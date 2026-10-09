"""
LLM Transit Gateway（双层架构 + Web 管理控制台）

架构：
  cc-switch / 客户端 → FastAPI(:4000) [Web UI / 管理 API / 参数清理] → LiteLLM(:4001) → 各厂商 API

Web 控制台：
  http://localhost:4000/

客户端 (cc-switch / Claude Code / VSCode / Cursor / OpenAI SDK) 配置：
  Base URL : http://localhost:4000/v1
  API Key  : 任意非空字符串 (例如 sk-123456)
  Model    : 对应厂商模型名 (如 gemini-2.5-flash / deepseek-chat / us.anthropic.claude-sonnet-4-6 等)

启动：
  /opt/miniconda3/envs/py311/bin/python proxy.py
"""

import asyncio
import base64
import hashlib
import ipaddress
import json
import logging
import os
import re
import subprocess
import sys
import threading
import time
from io import BytesIO
from pathlib import Path
from urllib.parse import urlsplit

import httpx
import uvicorn
from contextlib import asynccontextmanager
from fastapi import FastAPI, Request, HTTPException
from fastapi.responses import FileResponse, JSONResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles

import provider_manager

try:
    from PIL import Image
except Exception:
    Image = None

BASE_DIR = Path(__file__).parent.resolve()
STATIC_DIR = BASE_DIR / "static"

# ── 配置 ──────────────────────────────────────────────────────────────────────
# 端口从 providers_config.json 的 server.proxy_port / server.litellm_port 读取，
# 缺失时回退到 4000/4001（此前这两个字段只写在配置里，没有代码真正读取它们）。
_initial_config = provider_manager.load_config()
_server_cfg = _initial_config.get("server", {})
PROXY_HOST   = _server_cfg.get("proxy_host", "127.0.0.1")
PROXY_PORT   = _server_cfg.get("proxy_port", 4000)     # 客户端与 Web 访问端口
LITELLM_PORT = _server_cfg.get("litellm_port", 4001)   # LiteLLM 内部端口
_py_dir = os.path.dirname(sys.executable)
_scripts = os.path.join(_py_dir, "Scripts")
_bin_dir = _scripts if os.path.isdir(_scripts) else _py_dir
_ext = ".exe" if sys.platform == "win32" else ""
LITELLM_BIN  = os.path.join(_bin_dir, f"litellm{_ext}")
CONFIG_PATH  = str(BASE_DIR / "litellm_config.yaml")
LOG_DIR      = BASE_DIR / "logs"

# Bedrock 多图请求要求单图长边 ≤ 2000px（converse-stream 报错的硬限制）
_MAX_BEDROCK_IMAGE_DIM = 2000

_log_path: Path = None
_litellm_proc = None

_ANSI_STRIP_RE = re.compile(r'\x1b\[[0-9;]*m')
_TS_RE = re.compile(r'^\d{2}:\d{2}:\d{2}')
_GW_TS_RE = re.compile(r'^\[\d{2}:\d{2}:\d{2}\]')  # 网关自己打的 [HH:MM:SS] 行
_LEVEL_START_RE = re.compile(r'^(INFO|DEBUG|WARNING|ERROR|CRITICAL)')
_FILE_LEVELS = ("WARNING", "ERROR", "CRITICAL")
# 命中这些片段的行不写入日志文件（每个请求都会重复刷，且无需人工处理）
_LOG_NOISE_PATTERNS = (
    "not in built-in cost map",
    "Potential consecutive user/tool blocks",
    "does not support mixing function declarations with search tools",
    "Container ownership recording skipped",
    "GET /health/liveliness",
    "health/liveliness",
    "`temperature`, `top_p`, and `top_k` continue to function for Gemini 3+",
)

# 这些行连控制台都不打印，纯噪音
_CONSOLE_MUTE_PATTERNS = (
    "GET /health/liveliness",
    "health/liveliness",
    # LiteLLM 对 Gemini 3+ 每个带采样参数的请求都打一次弃用提示；参数目前仍然生效，
    # 由客户端（如 Codex）自带，网关无从改动，纯噪音
    "`temperature`, `top_p`, and `top_k` continue to function for Gemini 3+",
    "does not support mixing function declarations with search tools",
    "Container ownership recording skipped",
)

# 实时日志广播队列
_log_subscribers: set = set()
_recent_logs: list = []
_MAX_RECENT_LOGS = 200

# LiteLLM 已知的内部 bug 特征（屏蔽掉无害的异常日志）
_LITELLM_BUG_PATTERNS = (
    "AttributeError: 'dict' object has no attribute 'usage'",
    "Task exception was never retrieved",
    "_get_assembled_streaming_response",
)

# 上游/代理侧的网络故障：请求确实失败了（客户端会收到 500），不能像内部 bug 那样
# 直接丢掉，但 LiteLLM 为每次失败打的完整 traceback 有 40+ 行，一次抖动就刷满屏幕。
# 这里把整段折叠成一行摘要，保留「哪类故障」而不保留调用栈。
#
# 触发这些异常的典型链路：本地代理（http_proxy）与上游站点建 TLS 隧道失败、
# 流式响应中途被对端断开。故障原因在网关之外，调用栈对排查没有帮助。
_UPSTREAM_NET_ERROR_LABELS = (
    ("httpcore.ConnectError", "连接上游失败（代理/网络不可达）"),
    ("httpx.ConnectError", "连接上游失败（代理/网络不可达）"),
    ("httpx.ReadError", "读取上游响应失败（连接被断开）"),
    ("httpx.RemoteProtocolError", "上游提前关闭连接（响应体不完整）"),
    ("httpcore.RemoteProtocolError", "上游提前关闭连接（响应体不完整）"),
    ("httpx.ConnectTimeout", "连接上游超时"),
    ("httpx.ReadTimeout", "读取上游响应超时"),
    # LiteLLM 把上述底层异常包装后重抛的类型：重试耗尽后打的第二段栈同样无排查价值
    ("litellm.exceptions.InternalServerError", "上游请求失败（重试已耗尽）"),
    ("litellm.exceptions.APIConnectionError", "连接上游失败（重试已耗尽）"),
    ("litellm.exceptions.MidStreamFallbackError", "流式传输中断（兜底重试失败）"),
    ("litellm.exceptions.Timeout", "上游请求超时"),
)
# traceback 起始行。LiteLLM 会对超长输出做截断并插入
# "Traceback ... (litellm_truncated skipped N chars...)"，此时起始行不是标准形式，
# 所以只匹配 "Traceback" 前缀，把这两种都覆盖住。
_TRACEBACK_START = "Traceback (most recent call last):"
_TRACEBACK_START_RE = re.compile(r'^Traceback\b')


def _strip_ansi(s: str) -> str:
    return _ANSI_STRIP_RE.sub('', s)


def _broadcast_log(line: str):
    clean = _strip_ansi(line).strip()
    if not clean:
        return
    _recent_logs.append(clean)
    if len(_recent_logs) > _MAX_RECENT_LOGS:
        _recent_logs.pop(0)
    for q in list(_log_subscribers):
        try:
            q.put_nowait(clean)
        except Exception:
            pass


# 上游明确拒收请求内容（400）：不是网络问题，重试同样会失败。调用栈全在 LiteLLM
# 内部、对定位参数没有帮助，折叠成一行；原始请求体另存到 logs/failed_requests/。
_UPSTREAM_REJECT_LABELS = (
    ("INVALID_ARGUMENT", "400 INVALID_ARGUMENT（请求体已另存待排查）"),
    ("litellm.exceptions.BadRequestError", "400 BadRequest（请求体已另存待排查）"),
)
FAILED_REQ_DIR = LOG_DIR / "failed_requests"
_FAILED_REQ_KEEP = 20


def _save_failed_request(body: bytes, _model_name: str, status: int) -> None:
    """上游返回 4xx 时保存网关实际转发出去的请求体，只保留最近 _FAILED_REQ_KEEP 份。

    Vertex 的 400 只回一句 "Request contains an invalid argument"，不说是哪个字段，
    没有原始请求就无法定位。内容含对话历史，放在已被 .gitignore 排除的 logs/ 下。
    """
    try:
        FAILED_REQ_DIR.mkdir(parents=True, exist_ok=True)
        # 文件名完全由服务端生成，避免把客户端可控的模型名带入路径。
        name = f"{time.strftime('%Y%m%d_%H%M%S')}_{time.time_ns()}.json"
        (FAILED_REQ_DIR / name).write_bytes(body)
        for old in sorted(FAILED_REQ_DIR.glob("*.json"))[:-_FAILED_REQ_KEEP]:
            old.unlink(missing_ok=True)
        print(f"[{time.strftime('%H:%M:%S')}] [WARN] 上游返回 {status}，请求体已保存：logs/failed_requests/{name}")
    except Exception:
        pass


# 流式请求最多等上游状态码这么久：参数错/429/模型名错一般 1~2 秒就返回，能透传真实
# 状态码；更慢的（排队等并发名额、带截图的长上下文首 token 慢）先回 200 开始流式，
# 避免客户端或代理等不到响应头就超时断开。
_STREAM_HEADER_WAIT = 3.0


def _sse_error_event(path: str, status: int, raw) -> bytes:
    """把上游错误包装成客户端能识别的 SSE 事件（响应头已发出、无法再改状态码时使用）。"""
    err = None
    try:
        parsed = json.loads(raw) if raw else None
        err = parsed.get("error") if isinstance(parsed, dict) else None
    except Exception:
        pass
    if not isinstance(err, dict):
        text = raw.decode("utf-8", "replace") if isinstance(raw, (bytes, bytearray)) else str(raw)
        err = {"message": text[:2000]}
    code = str(err.get("code") or status)
    message = str(err.get("message") or "")
    if path.rstrip("/").endswith("responses"):
        evt = {"type": "response.failed", "response": {
            "id": f"resp_gw_{int(time.time() * 1000)}", "object": "response", "status": "failed",
            "error": {"code": code, "message": message}, "output": []}}
        return f"event: response.failed\ndata: {json.dumps(evt, ensure_ascii=False)}\n\n".encode()
    return f"data: {json.dumps({'error': {**err, 'code': code}}, ensure_ascii=False)}\n\n".encode()


class _CleanupStreamingResponse(StreamingResponse):
    """响应结束（含客户端提前断开）后调用 on_finish。

    客户端在流开始前就断开时，生成器不会运行、它的 finally 也不会执行，上游连接和并发
    名额会泄漏；on_finish 用来兜底。ASGI 是按类型查找 __call__，所以必须用子类覆盖。
    """

    def __init__(self, *args, on_finish=None, **kwargs):
        super().__init__(*args, **kwargs)
        self._on_finish = on_finish

    async def __call__(self, scope, receive, send):
        try:
            await super().__call__(scope, receive, send)
        finally:
            if self._on_finish is not None:
                self._on_finish()


def _abandon_upstream(task: "asyncio.Task", sem: asyncio.Semaphore) -> None:
    """客户端在上游响应到达前就断开：上游一旦连上立即关闭并归还并发名额，避免名额泄漏。"""
    def _cleanup(t):
        if t.cancelled() or t.exception() is not None:
            return  # 失败路径的名额已在 open_upstream 内释放
        asyncio.ensure_future(t.result().aclose())
        sem.release()
    if task.done():
        _cleanup(task)
    else:
        task.add_done_callback(_cleanup)
        task.cancel()


def _net_error_label(text: str):
    """文本命中上游网络故障或拒收特征时返回中文摘要，否则返回 None。"""
    for marker, label in _UPSTREAM_REJECT_LABELS:
        if marker in text:
            return label
    for marker, label in _UPSTREAM_NET_ERROR_LABELS:
        if marker in text:
            return label
    return None


class TeeStream:
    """控制台：过滤掉纯噪音行；日志文件：只记录 WARNING/ERROR/CRITICAL 及其后续 traceback 行。

    额外把上游网络故障的长 traceback 折叠成一行摘要——这类失败是真实的（客户端
    会收到 500），但调用栈全在 httpx/httpcore 内部，对排查没有帮助，而每次抖动都
    刷 40+ 行会把有用的请求日志顶掉。
    """

    def __init__(self, original_stream, log_file):
        self.original_stream = original_stream
        self.log_file = log_file
        self._in_important = False
        self._in_litellm_bug = False  # 跟踪 LiteLLM 内部 bug 的多行 traceback
        self._tb_buffer: list = []    # 缓冲整条异常链，待读完再决定如何输出
        self._in_traceback = False
        self._tb_started = 0.0        # 缓冲起始时刻，用于兜底 flush

    def _emit_console(self, line: str, clean: str):
        self.original_stream.write(line)
        stripped = clean.strip()
        if stripped:
            _broadcast_log(stripped)

    def _flush_tb_buffer(self):
        """traceback 结束：命中网络故障则只输出一行摘要，否则原样输出。"""
        if not self._tb_buffer:
            return
        joined = "".join(clean for _, clean in self._tb_buffer)
        # LiteLLM 内部无害 bug 的栈：多 worker 下前导的 "Task exception was never
        # retrieved" 行不一定出现，不能只靠它识别，这里按栈内容整段丢弃
        if any(p in joined for p in _LITELLM_BUG_PATTERNS):
            self._tb_buffer = []
            self._in_traceback = False
            return
        label = _net_error_label(joined)
        if label:
            kind = "上游拒绝" if label.startswith("400") else "上游网络故障"
            summary = f"[{time.strftime('%H:%M:%S')}] [WARN] {kind}：{label}（已折叠 {len(self._tb_buffer)} 行调用栈）\n"
            self._emit_console(summary, summary)
        else:
            for raw, clean in self._tb_buffer:
                self._emit_console(raw, clean)
        self._tb_buffer = []
        self._in_traceback = False

    def write(self, buf):
        for line in buf.splitlines(keepends=True):
            clean = _strip_ansi(line)
            # 检测 LiteLLM 内部 bug 的开始
            if "Task exception was never retrieved" in clean:
                self._in_litellm_bug = True
                continue
            # 在 bug traceback 期间，检查是否包含特征字符串
            if self._in_litellm_bug:
                if any(p in clean for p in _LITELLM_BUG_PATTERNS):
                    continue
                # 如果是空行或缩进行（traceback 的一部分），继续跳过
                if not clean.strip() or clean.startswith("  ") or clean.startswith("\t"):
                    continue
                # 遇到新的非缩进行，说明 traceback 结束
                self._in_litellm_bug = False

            if any(p in clean for p in _CONSOLE_MUTE_PATTERNS):
                continue
            # 同一个无害 bug 的 "future: <Task finished ... AttributeError(...)>" 摘要行
            if clean.startswith("future: <Task finished") and "no attribute 'usage'" in clean:
                continue

            # traceback 折叠：异常类型出现在末尾，先缓冲整条异常链，读完再决定输出
            # 形式。异常链由多段 traceback 加 "The above exception..." 连接行组成，
            # 必须整条一起判断——底层的 ConnectError 在第一段，LiteLLM 包装后的类型
            # 在最后一段，分段判断会漏掉其中一部分。
            if _TRACEBACK_START_RE.match(clean):
                if not self._in_traceback:
                    self._tb_started = time.monotonic()
                self._in_traceback = True
                self._tb_buffer.append((line, clean))
                continue
            if self._in_traceback:
                # 出现新的日志行（带时间戳/日志级别/uvicorn 前缀）才算异常链结束
                if _TS_RE.match(clean) or _GW_TS_RE.match(clean) or _LEVEL_START_RE.match(clean) or clean.startswith("INFO:"):
                    self._flush_tb_buffer()
                    self._emit_console(line, clean)
                    continue
                self._tb_buffer.append((line, clean))
                if len(self._tb_buffer) > 400:   # 防御异常长的输出占满内存
                    self._flush_tb_buffer()
                continue

            self._emit_console(line, clean)
        # 不在 write() 末尾按「已出现异常行」收尾：异常链的第一段就带 ConnectError，
        # 那样会把后续 "The above exception..." 和第二段栈漏成原样输出。正常收尾靠
        # 下一条日志行触发；这里只做时间兜底，避免最后一段异常链一直卡在缓冲里。
        if self._tb_buffer and (time.monotonic() - self._tb_started) > 2.0:
            self._flush_tb_buffer()
        self.original_stream.flush()
        try:
            for line in buf.splitlines(keepends=True):
                clean = _strip_ansi(line)
                # 日志文件也要过滤 LiteLLM bug
                if any(p in clean for p in _LITELLM_BUG_PATTERNS):
                    continue
                if _TS_RE.match(clean) or _LEVEL_START_RE.match(clean):
                    is_important = any(lvl in clean for lvl in _FILE_LEVELS)
                    is_noise = any(p in clean for p in _LOG_NOISE_PATTERNS)
                    self._in_important = is_important and not is_noise
                if not self._in_important:
                    continue
                # 栈帧对排查上游网络故障没有价值，不落盘；末尾的异常行（非缩进，
                # 含 httpcore.ConnectError 这类类型名）保留，故障类型仍然可查。
                if _TRACEBACK_START in clean or clean.startswith(("  ", "\t")):
                    continue
                self.log_file.write(line)
            self.log_file.flush()
        except Exception:
            pass

    def flush(self):
        self.original_stream.flush()
        try:
            self.log_file.flush()
        except Exception:
            pass

    def isatty(self):
        return getattr(self.original_stream, "isatty", lambda: False)()


def cleanup_old_logs(retention_days: int):
    if retention_days <= 0 or not LOG_DIR.exists():
        return
    cutoff = time.time() - retention_days * 86400
    removed = 0
    for f in LOG_DIR.glob("*.log"):
        try:
            if f.stat().st_mtime < cutoff:
                f.unlink()
                removed += 1
        except OSError:
            pass
    if removed:
        print(f"      日志清理：已删除 {removed} 个超过 {retention_days} 天的旧日志")


def setup_logging(retention_days: int = 7) -> Path:
    global _log_path
    LOG_DIR.mkdir(exist_ok=True)
    cleanup_old_logs(retention_days)
    today = time.strftime("%Y-%m-%d")
    n = 1
    while (LOG_DIR / f"{today}_{n}.log").exists():
        n += 1
    _log_path = LOG_DIR / f"{today}_{n}.log"

    log_file = open(_log_path, "a", encoding="utf-8")
    sys.stdout = TeeStream(sys.__stdout__, log_file)
    sys.stderr = TeeStream(sys.__stderr__, log_file)

    fmt = "%(asctime)s [%(levelname)s] %(name)s: %(message)s"
    logging.basicConfig(
        level=logging.INFO,
        format=fmt,
        handlers=[
            logging.StreamHandler(sys.stdout),
        ],
    )

    # 降噪：httpx 会为每一次转发/健康探测打一条 "HTTP Request: ... 200 OK"，
    # 与网关自己的请求行完全重复；uvicorn.access 的探测行同理。只保留告警及以上。
    for _noisy in ("httpx", "httpcore", "uvicorn.access"):
        logging.getLogger(_noisy).setLevel(logging.WARNING)

    return _log_path


# ── 并发转发：共享连接池 + 按厂商分组的并发上限 ──────────────────────────────
# 之前每个请求都现建现拆 httpx.AsyncClient，高并发下连接建立/断开开销明显，
# 表现上接近串行。这里改为模块级单例连接池。
#
# 并发上限按厂商（model 前缀，如 vertex_ai/xxx 中的 "vertex_ai"）分组：不同厂商
# 的配额互不挤占。之前是全局共享一个信号量——本意是压住 Vertex 项目在高并发下的
# 429，结果连没有这个限制的厂商（Bedrock/OpenAI 兼容线路等）也被一起卡住。
_DEFAULT_CONCURRENCY = _server_cfg.get("default_concurrency", 16)
_PROVIDER_CONCURRENCY = _server_cfg.get("provider_concurrency", {}) or {}
_provider_semaphores: dict = {}


def _build_bare_model_providers(config: dict) -> dict:
    return provider_manager.bare_model_providers(config)


_BARE_MODEL_PROVIDERS = _build_bare_model_providers(_initial_config)


def _semaphore_for_provider(pid: str) -> asyncio.Semaphore:
    limit = _PROVIDER_CONCURRENCY.get(pid, _DEFAULT_CONCURRENCY)
    sem = _provider_semaphores.get(pid)
    if sem is None:
        sem = asyncio.Semaphore(limit)
        _provider_semaphores[pid] = sem
    return sem


def _semaphore_for_model(model_name: str) -> asyncio.Semaphore:
    if model_name and "/" in model_name:
        pid = model_name.split("/", 1)[0]
    else:
        pid = _BARE_MODEL_PROVIDERS.get(model_name, "_default")
    return _semaphore_for_provider(pid)


def reload_concurrency_settings(server_cfg: dict):
    """Web 页面保存配置后调用：刷新并发限制而不需要重启整个 proxy 进程。
    已在途的请求仍持有旧信号量的许可，不受影响，正常释放即可；旧信号量本身
    随请求结束后被 GC，不会遗留问题。"""
    global _DEFAULT_CONCURRENCY, _PROVIDER_CONCURRENCY
    _DEFAULT_CONCURRENCY = server_cfg.get("default_concurrency", 16)
    _PROVIDER_CONCURRENCY = server_cfg.get("provider_concurrency", {}) or {}
    _provider_semaphores.clear()


def reload_runtime_settings(config: dict):
    """配置成功切换后刷新不需要重建 FastAPI 进程的运行时设置。"""
    global _BARE_MODEL_PROVIDERS, _NORMAL_TIMEOUT
    server_cfg = config.get("server", {})
    reload_concurrency_settings(server_cfg)
    _BARE_MODEL_PROVIDERS = _build_bare_model_providers(config)
    _NORMAL_TIMEOUT = httpx.Timeout(
        connect=10.0,
        read=float(server_cfg.get("request_timeout", 600)),
        write=30.0,
        pool=10.0,
    )
    _http_client.timeout = _NORMAL_TIMEOUT


# httpx 连接池上限固定给一个宽松值：并发信号量是实际限流手段，这里只要不成为
# 瓶颈即可（并发配置可在 Web 页面热更新，连接池不跟着动态调整，避免重建 client
# 打断正在途的连接）。
_STREAM_TIMEOUT = httpx.Timeout(connect=10.0, read=None, write=30.0, pool=10.0)
# 非流式请求的读超时跟随 request_timeout（默认 600s）。之前写死 120s：长任务的非流式
# 请求在上游还没返回时就被网关先掐断，客户端看到的就是「偶发超时」。
_NORMAL_TIMEOUT = httpx.Timeout(
    connect=10.0, read=float(_server_cfg.get("request_timeout", 600)), write=30.0, pool=10.0
)
_HTTP_LIMITS = httpx.Limits(max_connections=200, max_keepalive_connections=100)

_http_client: httpx.AsyncClient = httpx.AsyncClient(
    timeout=_NORMAL_TIMEOUT, limits=_HTTP_LIMITS, trust_env=False
)
_stream_http_client: httpx.AsyncClient = httpx.AsyncClient(
    timeout=_STREAM_TIMEOUT, limits=_HTTP_LIMITS, trust_env=False
)


@asynccontextmanager
async def _lifespan(app: FastAPI):
    yield
    await _http_client.aclose()
    await _stream_http_client.aclose()


app = FastAPI(title="LLM Transit Gateway", lifespan=_lifespan)
if STATIC_DIR.exists():
    app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")

_admin_lock = asyncio.Lock()


def _require_client_authorization(request: Request) -> None:
    auth = request.headers.get("authorization", "")
    scheme, _, token = auth.partition(" ")
    if scheme.lower() != "bearer" or not token.strip():
        raise HTTPException(
            status_code=401,
            detail="需要非空 Bearer API Key",
            headers={"WWW-Authenticate": "Bearer"},
        )


def _require_admin_request(request: Request) -> None:
    """允许同源浏览器请求；无 Origin 的脚本调用仅允许来自本机。"""
    origin = request.headers.get("origin")
    if origin:
        parsed = urlsplit(origin)
        host = request.headers.get("host", "").lower()
        if parsed.scheme in ("http", "https") and parsed.netloc.lower() == host:
            return
        raise HTTPException(status_code=403, detail="管理接口只接受同源请求")

    client_host = request.client.host if request.client else ""
    try:
        if ipaddress.ip_address(client_host).is_loopback:
            return
    except ValueError:
        pass
    raise HTTPException(status_code=403, detail="管理接口只允许本机访问")

_BEDROCK_ID_RE = re.compile(r'[^a-zA-Z0-9_.:-]')


def _sanitize_bedrock_id(raw: str, id_map: dict) -> str:
    """将 tool call ID 规范化为 Bedrock 兼容格式（≤64字符，仅 [a-zA-Z0-9_.:-]）。"""
    if raw not in id_map:
        s = _BEDROCK_ID_RE.sub('_', raw)
        if len(s) > 64:
            h = hashlib.md5(raw.encode()).hexdigest()[:8]
            s = s[:55] + '_' + h
        id_map[raw] = s
    return id_map[raw]


# OpenAI Responses API 规范里 input 项的 id 上限是 64 字符（超了上游直接 400
# ApiIdParam / string_above_max_length）。
#
# LiteLLM 为了 encrypted-content affinity routing，会把 model_id 编码进 item id：
#   encitem_{base64("litellm:model_id:<id>;item_id:<原 id>")}
# 客户端把上一轮的 id 原样带回来后，LiteLLM 会对「已经编码过的 id」再编码一次，
# 于是每轮对话 id 长度按 ~1.4 倍指数增长（144 → 284 → 472 → … → 3928 → 5328），
# 长会话跑十来轮就超过 64 字符，请求被上游拒绝，且 fallback 重试的是同一条线路，
# 重试同样失败，表现为 Agent 直接中断。
#
# 解法是把套娃的 id 递归解包回最内层的原始 id（rs_<uuid> 这种正常长度）。副作用是
# 丢掉 affinity routing 携带的 model_id，也就是不再强制把请求路由回产出该内容的
# 那个模型——对「同一段历史中途换模型」的用法来说这正是期望行为。
#
# 另一条独立的约束是字符集：id 只允许字母、数字、下划线、连字符。LiteLLM 会把
# Gemini 的思维签名拼在 tool call id 后面（call_<uuid>__thought__<base64 签名>，
# 见 litellm_core_utils/prompt_templates/factory.py），base64 含 "/" 和 "+"，
# 转发给 OpenAI 兼容线路会被判 invalid_value。LiteLLM 只为 Anthropic 做了这层
# 清理（normalize_anthropic_tool_use_id），OpenAI 兼容线路是漏的，这里补上：
# 签名对非 Gemini 模型没有意义，直接连后缀一起丢掉。
_RESPONSES_ID_MAX_LEN = 64
_ENCITEM_PREFIX = "encitem_"
_THOUGHT_SIG_SEPARATOR = "__thought__"
_INVALID_ID_CHARS_RE = re.compile(r"[^a-zA-Z0-9_-]")


def _unwrap_encoded_item_id(raw: str) -> str:
    """把 LiteLLM 嵌套编码的 encitem_ id 递归解包成最内层的原始 id。"""
    seen = 0
    while raw.startswith(_ENCITEM_PREFIX) and seen < 32:  # 上限防御畸形输入导致死循环
        cleaned = raw[len(_ENCITEM_PREFIX):]
        missing = len(cleaned) % 4
        if missing:
            cleaned += "=" * (4 - missing)
        try:
            decoded = base64.b64decode(cleaned.encode()).decode("utf-8")
        except Exception:
            break
        parts = decoded.split(";", 1)   # item_id 内部可能含分号，只切第一个
        if len(parts) < 2:
            break
        raw = parts[1].replace("item_id:", "", 1)
        seen += 1
    return raw


def _normalize_responses_ids(data: dict):
    """规范化 Responses API input 项的 id，使其满足上游的长度与字符集约束。

    依次处理：解包 LiteLLM 的嵌套 encitem_ 编码 → 去掉 Gemini 思维签名后缀 →
    替换非法字符 → 仍超长则哈希截断保底。同一个原始 id 可能在多处被引用
    （item.id 与后续项的 call_id 等），用映射表保证改写后仍指向同一个值，
    不破坏引用关系。
    """
    items = data.get("input")
    if not isinstance(items, list):
        return

    id_map: dict = {}

    def normalize(raw):
        if not isinstance(raw, str) or not raw:
            return raw
        if raw in id_map:
            return id_map[raw]

        fixed = raw
        if fixed.startswith(_ENCITEM_PREFIX):
            fixed = _unwrap_encoded_item_id(fixed)
        # 思维签名只对产出它的 Gemini 模型有意义，跨模型转发时连后缀一起丢掉
        if _THOUGHT_SIG_SEPARATOR in fixed:
            fixed = fixed.split(_THOUGHT_SIG_SEPARATOR, 1)[0]
        fixed = _INVALID_ID_CHARS_RE.sub("_", fixed)
        if len(fixed) > _RESPONSES_ID_MAX_LEN:
            h = hashlib.md5(raw.encode()).hexdigest()[:12]
            fixed = f"{fixed[:_RESPONSES_ID_MAX_LEN - len(h) - 1]}_{h}"

        if fixed != raw:
            id_map[raw] = fixed
        return fixed

    for item in items:
        if not isinstance(item, dict):
            continue
        for key in ("id", "call_id", "tool_use_id", "item_id"):
            if key in item:
                item[key] = normalize(item[key])
        for block in item.get("content") or []:
            if isinstance(block, dict):
                for key in ("id", "tool_use_id", "item_id"):
                    if key in block:
                        block[key] = normalize(block[key])

    if id_map:
        print(f"[{time.strftime('%H:%M:%S')}] [WARN] 规范化了 {len(id_map)} 个不合规 input id（长度/字符集不满足上游约束）")


def _ensure_bedrock_tool_config(data: dict):
    """Bedrock 要求消息含 toolUse/toolResult 时必须提供 toolConfig；若 tools 缺失则从历史中重建。"""
    if data.get("tools"):
        return

    tool_names: set = set()

    for msg in data.get("messages") or []:
        if not isinstance(msg, dict):
            continue
        for tc in msg.get("tool_calls") or []:
            if isinstance(tc, dict):
                fn = tc.get("function", {})
                if isinstance(fn, dict) and fn.get("name"):
                    tool_names.add(fn["name"])
        for block in msg.get("content") or []:
            if isinstance(block, dict) and block.get("type") == "tool_use" and block.get("name"):
                tool_names.add(block["name"])

    for item in data.get("input") or []:
        if isinstance(item, dict) and item.get("type") == "function_call" and item.get("name"):
            tool_names.add(item["name"])

    if tool_names:
        data["tools"] = [
            {
                "type": "function",
                "function": {
                    "name": name,
                    "description": name,
                    "parameters": {"type": "object", "properties": {}},
                },
            }
            for name in sorted(tool_names)
        ]


def _fix_bedrock_tool_ids(data: dict):
    """遍历请求体，将所有 tool call ID 规范化以满足 Bedrock 约束。"""
    id_map: dict = {}

    def fix(v: str) -> str:
        return _sanitize_bedrock_id(v, id_map)

    # Chat Completions 格式：messages
    for msg in data.get("messages") or []:
        if not isinstance(msg, dict):
            continue
        for tc in msg.get("tool_calls") or []:
            if isinstance(tc, dict) and "id" in tc:
                tc["id"] = fix(tc["id"])
        if msg.get("role") == "tool" and "tool_call_id" in msg:
            msg["tool_call_id"] = fix(msg["tool_call_id"])
        for block in msg.get("content") or []:
            if not isinstance(block, dict):
                continue
            if block.get("type") == "tool_use" and "id" in block:
                block["id"] = fix(block["id"])
            if block.get("type") == "tool_result" and "tool_use_id" in block:
                block["tool_use_id"] = fix(block["tool_use_id"])

    # Responses API 格式：input
    for item in data.get("input") or []:
        if not isinstance(item, dict):
            continue
        if "call_id" in item:
            item["call_id"] = fix(item["call_id"])
        for block in item.get("content") or []:
            if not isinstance(block, dict):
                continue
            if block.get("type") == "tool_use" and "id" in block:
                block["id"] = fix(block["id"])
            if block.get("type") == "tool_result" and "tool_use_id" in block:
                block["tool_use_id"] = fix(block["tool_use_id"])


def _resize_base64_image(b64_data: str, max_dim: int = _MAX_BEDROCK_IMAGE_DIM) -> str:
    """若图片长边超过 max_dim，按比例缩小并重新编码为 base64（保持原格式，失败则原样返回）。"""
    if Image is None or not b64_data:
        return b64_data
    try:
        raw = base64.b64decode(b64_data)
        img = Image.open(BytesIO(raw))
        w, h = img.size
        if max(w, h) <= max_dim:
            return b64_data
        scale = max_dim / float(max(w, h))
        new_size = (max(1, int(w * scale)), max(1, int(h * scale)))
        fmt = img.format or "PNG"
        if img.mode in ("RGBA", "P") and fmt.upper() in ("JPEG", "JPG"):
            img = img.convert("RGB")
        resized = img.resize(new_size, Image.LANCZOS)
        buf = BytesIO()
        resized.save(buf, format=fmt)
        return base64.b64encode(buf.getvalue()).decode()
    except Exception:
        return b64_data


def _shrink_oversized_images(data: dict):
    """遍历请求体中的所有 base64 图片，缩小超过 Bedrock 限制尺寸的图片。"""
    if Image is None:
        return

    def visit_content_list(content):
        if not isinstance(content, list):
            return
        for block in content:
            if not isinstance(block, dict):
                continue
            if block.get("type") == "image":
                source = block.get("source")
                if isinstance(source, dict) and source.get("type") == "base64" and source.get("data"):
                    source["data"] = _resize_base64_image(source["data"])
            elif block.get("type") == "image_url":
                url_obj = block.get("image_url")
                url = url_obj.get("url") if isinstance(url_obj, dict) else url_obj
                if isinstance(url, str) and url.startswith("data:") and ";base64," in url:
                    header, b64 = url.split(";base64,", 1)
                    new_b64 = _resize_base64_image(b64)
                    new_url = f"{header};base64,{new_b64}"
                    if isinstance(url_obj, dict):
                        url_obj["url"] = new_url
                    else:
                        block["image_url"] = new_url

    for msg in data.get("messages") or []:
        if isinstance(msg, dict):
            visit_content_list(msg.get("content"))

    for item in data.get("input") or []:
        if isinstance(item, dict):
            visit_content_list(item.get("content"))


# Gemini/Vertex 的 thinkingLevel 只认这几个值；xhigh/max 等是 Claude 系专用别名，
# 传给 Gemini 会被 litellm 直接 ValueError 拒绝（Anthropic/Bedrock 原生支持
# xhigh，所以这个归一化只能按目标模型判断，不能对所有厂商一刀切）。
_GEMINI_VALID_EFFORT = {"minimal", "low", "medium", "high", "disable", "none"}
_GEMINI_EFFORT_ALIAS = {
    "xhigh": "high",
    "x-high": "high",
    "extra-high": "high",
    "max": "high",
    "auto": "medium",
    "default": "medium",
    "off": "none",
    "disabled": "disable",
}


# 实测 89 个函数 + json_schema 通过，131 个不通过；取保守值
_GEMINI_SCHEMA_TOOLS_LIMIT = 80


def _is_gemini_model(model_name: str) -> bool:
    m = (model_name or "").lower()
    return "gemini" in m or m.startswith("vertex_ai/") or m.startswith("vertex/")


def _fix_gemini_effort(value):
    """归一化 Gemini 的 reasoning effort 别名。返回 None 表示无法识别，调用方应删掉该字段。"""
    if not isinstance(value, str):
        return None
    v = value.strip().lower()
    if v in _GEMINI_VALID_EFFORT:
        return v
    return _GEMINI_EFFORT_ALIAS.get(v)


def _clean_body(raw: bytes):
    """移除会导致 LiteLLM / Gemini / Bedrock 报错的冲突参数。

    返回 (body, model_name, is_stream)。请求体只在这里解析一次，避免转发路径上
    对同一份 JSON（可能内含大体积 base64 图片）重复 json.loads 浪费 CPU。
    """
    if not raw:
        return raw, "", False
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return raw, "", False

    # 仅对路由到 Gemini/Vertex 的请求归一化 reasoning effort 别名
    if _is_gemini_model(data.get("model", "")):
        if "reasoning_effort" in data:
            fixed = _fix_gemini_effort(data["reasoning_effort"])
            if fixed is None:
                data.pop("reasoning_effort", None)
            elif fixed != data["reasoning_effort"]:
                data["reasoning_effort"] = fixed

        # /v1/responses 风格：reasoning: {"effort": "xhigh"}
        reasoning = data.get("reasoning")
        if isinstance(reasoning, dict) and "effort" in reasoning:
            fixed = _fix_gemini_effort(reasoning["effort"])
            if fixed is None:
                reasoning.pop("effort", None)
                if not reasoning:
                    data.pop("reasoning", None)
            elif fixed != reasoning["effort"]:
                reasoning["effort"] = fixed

    # Vertex Gemini 不接受「json_schema 结构化输出 + 大量函数声明」同时出现：实测同一请求
    # 去掉任一侧都 200，github(89)+figma(42) 两个 namespace 单独各自 200、合起来 400
    # INVALID_ARGUMENT（无更具体报错）。Codex 的后台建议请求会带上全部 MCP 工具并要求
    # 严格 JSON 输出，它本身不需要调用工具，所以这种组合下丢掉 tools 保住结构化输出。
    if _is_gemini_model(data.get("model", "")) and data.get("tools"):
        fmt = (data.get("text") or {}).get("format") or {}
        if fmt.get("type") == "json_schema":
            n_funcs = sum(
                len(t.get("tools") or []) if t.get("type") == "namespace" else 1
                for t in data["tools"] if isinstance(t, dict)
            )
            if n_funcs > _GEMINI_SCHEMA_TOOLS_LIMIT:
                data.pop("tools", None)
                data.pop("tool_choice", None)
                data.pop("parallel_tool_calls", None)
                print(f"[{time.strftime('%H:%M:%S')}] [WARN] Gemini 结构化输出请求带 {n_funcs} 个工具，超过 {_GEMINI_SCHEMA_TOOLS_LIMIT}，已去掉 tools 避免 400")

    # Gemini 3+ 只接受 thinking_level，不接受同时存在的 thinking/thinking_budget
    if "thinking_level" in data:
        data.pop("thinking", None)
        data.pop("thinking_budget", None)

    # 修复历史消息中 tool call arguments 损坏的 JSON（上游模型截断/拼接错误产生）
    for msg in data.get("messages") or []:
        if not isinstance(msg, dict):
            continue
        for tc in msg.get("tool_calls") or []:
            if not isinstance(tc, dict):
                continue
            fn = tc.get("function")
            if not isinstance(fn, dict):
                continue
            args = fn.get("arguments", "")
            if not isinstance(args, str):
                continue
            try:
                json.loads(args)
            except (json.JSONDecodeError, ValueError):
                fn["arguments"] = "{}"

    # 清理 invalid / empty tools (例如 [{}] 或包含空字典的 tool)
    if "tools" in data:
        if isinstance(data["tools"], list):
            cleaned_tools = [
                t for t in data["tools"]
                if isinstance(t, dict) and t
            ]
            if cleaned_tools:
                data["tools"] = cleaned_tools
            else:
                data.pop("tools", None)
        elif data["tools"] is None:
            data.pop("tools", None)

    # Gemini 要求对话必须以 user 消息结尾；其他厂商可能把末尾 assistant 当作续写前缀。
    if _is_gemini_model(data.get("model", "")):
        messages = data.get("messages", [])
        while messages and isinstance(messages[-1], dict) and messages[-1].get("role") in ("assistant", "model"):
            messages.pop()
        if messages:
            data["messages"] = messages

    # Responses API：禁用有状态存储，避免 LiteLLM 重建历史时把末尾 assistant 消息带给 Gemini
    if "input" in data:
        data["store"] = False
        # 客户端在多模型会话里回传的历史可能带超长 id，转发给 OpenAI 兼容线路会
        # 400。所有厂商都做这个归一化：切模型时历史会跨厂商流动，只修出错的那条
        # 线路挡不住问题。
        _normalize_responses_ids(data)

    # Bedrock 专项修复
    model = data.get("model", "")
    if "us.anthropic." in model or "global.anthropic." in model or "bedrock" in model:
        _fix_bedrock_tool_ids(data)
        _ensure_bedrock_tool_config(data)
        _shrink_started = time.monotonic()
        _shrink_oversized_images(data)
        _shrink_elapsed = time.monotonic() - _shrink_started
        if _shrink_elapsed > 0.05:
            # 图片缩放是同步 PIL 操作，会阻塞事件循环；耗时明显时打印出来，
            # 方便判断"请求变慢像串行"是否是这里造成的。
            print(f"[{time.strftime('%H:%M:%S')}] [WARN] 图片缩放耗时 {_shrink_elapsed*1000:.0f}ms（阻塞事件循环）")
        data.pop("tool_choice", None)
        _BEDROCK_ALLOWED = {
            "model", "messages", "tools", "stream",
            "max_tokens", "max_completion_tokens", "temperature", "top_p", "top_k",
            "stop", "n", "user", "response_format", "seed",
            "thinking", "reasoning_effort",
            "presence_penalty", "frequency_penalty",
            "input", "store", "previous_response_id",
        }
        for _k in list(data.keys()):
            if _k not in _BEDROCK_ALLOWED:
                data.pop(_k)
        thinking = data.get("thinking")
        if isinstance(thinking, dict) and thinking.get("type") == "enabled":
            data["thinking"] = {"type": "adaptive"}

    return (
        json.dumps(data).encode(),
        data.get("model") or "",
        bool(data.get("stream", False)),
    )


# ── Web UI 首页 ─────────────────────────────────────────────────────────────
@app.get("/")
async def index():
    html_file = STATIC_DIR / "index.html"
    if html_file.exists():
        return FileResponse(html_file)
    return Response("<h1>LLM Transit Gateway 正在运行</h1><p>未找到 static/index.html</p>", media_type="text/html")


# ── 管理 API 接口 ─────────────────────────────────────────────────────────────
@app.get("/api/config")
async def get_config(request: Request):
    _require_admin_request(request)
    config = provider_manager.load_config()
    return JSONResponse(config)


@app.post("/api/config")
async def update_config(request: Request):
    _require_admin_request(request)
    try:
        new_config = await request.json()
        provider_manager.validate_config(new_config)
    except (ValueError, json.JSONDecodeError) as e:
        raise HTTPException(status_code=400, detail=str(e))

    async with _admin_lock:
        old_config = provider_manager.load_config()
        old_server = old_config.get("server", {})
        new_server = new_config.get("server", {})
        immutable_defaults = {
            "proxy_host": "127.0.0.1",
            "proxy_port": 4000,
            "litellm_port": 4001,
        }
        changed_immutable = [
            name for name, default in immutable_defaults.items()
            if old_server.get(name, default) != new_server.get(name, default)
        ]
        if changed_immutable:
            raise HTTPException(
                status_code=409,
                detail=f"{', '.join(changed_immutable)} 需要完整重启 proxy.py，不能热更新",
            )
        try:
            await asyncio.to_thread(provider_manager.generate_litellm_yaml, new_config)
            ready = await asyncio.to_thread(restart_litellm_subproc, new_config)
            if not ready:
                raise RuntimeError("新 LiteLLM 进程未能就绪")
            await asyncio.to_thread(provider_manager.save_config, new_config)
            await asyncio.to_thread(provider_manager.prune_vertex_credential_files, new_config)
            reload_runtime_settings(new_config)
            return JSONResponse({"status": "ok", "message": "配置已保存并重载生效"})
        except Exception as e:
            # 新配置应用失败时恢复旧 YAML、旧进程与旧运行时设置，避免保存操作造成停服。
            try:
                await asyncio.to_thread(provider_manager.generate_litellm_yaml, old_config)
                restored = await asyncio.to_thread(restart_litellm_subproc, old_config)
                await asyncio.to_thread(provider_manager.save_config, old_config)
                await asyncio.to_thread(provider_manager.prune_vertex_credential_files, old_config)
                reload_runtime_settings(old_config)
            except Exception as rollback_error:
                raise HTTPException(
                    status_code=503,
                    detail=f"应用配置失败且回滚失败: {e}; {rollback_error}",
                )
            if not restored:
                raise HTTPException(status_code=503, detail=f"应用配置失败，旧配置也未能恢复: {e}")
            raise HTTPException(status_code=503, detail=f"应用配置失败，已恢复旧配置: {e}")


@app.post("/api/restart")
async def restart_service_api(request: Request):
    _require_admin_request(request)
    async with _admin_lock:
        try:
            ready = await asyncio.to_thread(restart_litellm_subproc)
            if not ready:
                raise HTTPException(status_code=503, detail="LiteLLM 重启后未能就绪")
            return JSONResponse({"status": "ok", "message": "LiteLLM 网关核心已成功重启"})
        except HTTPException:
            raise
        except Exception as e:
            raise HTTPException(status_code=500, detail=str(e))


@app.get("/api/health")
async def health_check():
    cfg = provider_manager.load_config()
    total_models = 0
    for p in cfg.get("providers", []):
        if p.get("enabled"):
            models = provider_manager.selected_models_for_provider(p)
            total_models += len(models)
    
    litellm_ok = False
    try:
        async with httpx.AsyncClient(timeout=2.0, trust_env=False) as client:
            res = await client.get(f"http://127.0.0.1:{LITELLM_PORT}/health/liveliness")
            litellm_ok = (res.status_code == 200)
    except Exception:
        litellm_ok = False

    return JSONResponse({
        "status": "ok" if litellm_ok else "warning",
        "litellm_ready": litellm_ok,
        "active_models_count": total_models
    })


@app.post("/api/providers/{provider_id}/fetch_models")
async def fetch_models_endpoint(provider_id: str, request: Request):
    _require_admin_request(request)
    try:
        body = await request.json()
        if body.get("id") != provider_id:
            raise ValueError("请求路径与厂商 id 不一致")
        report = await asyncio.to_thread(provider_manager.fetch_provider_models_report, body)
        return JSONResponse({"status": "ok", **report})
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"拉取失败: {str(e)}")


@app.get("/api/logs/stream")
async def stream_logs(request: Request):
    _require_admin_request(request)
    q = asyncio.Queue()
    _log_subscribers.add(q)

    async def log_generator():
        try:
            # 先回放最近日志
            for item in _recent_logs[-30:]:
                yield f"data: {item}\n\n"
            while True:
                msg = await q.get()
                yield f"data: {msg}\n\n"
        except asyncio.CancelledError:
            _log_subscribers.discard(q)

    return StreamingResponse(log_generator(), media_type="text/event-stream")


# ── 模型列表：拦截并回填真实厂商 ────────────────────────────────────────────────
# 厂商 id → 对外分组显示名（不影响路由，路由仍用 id 前缀）
_OWNER_LABELS = {
    "gemini": "google",
    "bedrock": "aws",
    "vertex_ai": "vertex",
}


@app.get("/v1/models")
@app.get("/models")
async def list_models(request: Request):
    """LiteLLM 的 /v1/models 把所有模型的 owned_by 都写死成 openai，导致 cc-switch
    把全部模型归到一个组。这里取 LiteLLM 实际加载的模型，再按 `厂商id/模型名` 前缀
    把 owned_by 改回真实厂商，让客户端按厂商分组。"""
    _require_client_authorization(request)
    try:
        async with httpx.AsyncClient(timeout=10.0, trust_env=False) as client:
            resp = await client.get(f"http://localhost:{LITELLM_PORT}/v1/models")
        data = resp.json()
    except Exception as e:
        print(f"[ERROR] 获取模型列表失败: {type(e).__name__}: {e}")
        return JSONResponse(
            {"error": {"message": "内部模型服务暂时不可用", "type": "proxy_error"}},
            status_code=502,
        )

    for m in data.get("data", []):
        mid = m.get("id", "")
        if "/" in mid:
            pid = mid.split("/", 1)[0]
            m["owned_by"] = _OWNER_LABELS.get(pid, pid)
    return JSONResponse(data)


# ── 代理转发路由 ─────────────────────────────────────────────────────────────
@app.api_route("/{path:path}", methods=["GET", "POST", "PUT", "DELETE", "PATCH", "OPTIONS"])
async def proxy(request: Request, path: str):
    # 处理 CORS 预检
    if request.method == "OPTIONS":
        return Response(
            content="",
            status_code=200,
            headers={
                "Access-Control-Allow-Origin": "*",
                "Access-Control-Allow-Methods": "*",
                "Access-Control-Allow-Headers": "*",
            }
        )

    _require_client_authorization(request)

    raw_body = await request.body()
    body, model_name, is_stream = _clean_body(raw_body)

    print(f"[{time.strftime('%H:%M:%S')}] {request.method} /{path} -> {model_name or '-'}")

    headers = {
        k: v for k, v in request.headers.items()
        if k.lower() not in ("host", "content-length")
    }

    upstream = f"http://localhost:{LITELLM_PORT}/{path}"
    semaphore = _semaphore_for_model(model_name)

    async def acquire_slot():
        # 并发满时请求会在这里静默排队，客户端只感知到「变慢」。排队超过 1 秒就打一行，
        # 用来区分慢在网关排队还是慢在上游。
        t0 = time.monotonic()
        await semaphore.acquire()
        waited = time.monotonic() - t0
        if waited > 1.0:
            pid = model_name.split("/", 1)[0] if "/" in model_name else _BARE_MODEL_PROVIDERS.get(model_name, "_default")
            limit = _PROVIDER_CONCURRENCY.get(pid, _DEFAULT_CONCURRENCY)
            print(f"[{time.strftime('%H:%M:%S')}] [排队] {model_name} 等待并发名额 {waited:.1f}s（{pid} 上限 {limit}）")

    try:
        if is_stream:
            # 上游报错要让客户端看见：之前一律回 200 + 错误 JSON，客户端以为成功却收不到
            # 合法事件就断开。但一直等上游响应头也不行——排队等名额、或带截图的长上下文
            # 首 token 很慢时，客户端/代理等不到响应头就超时断开，带图请求失败率因此升高。
            # 折中：最多等 _STREAM_HEADER_WAIT 秒，期间上游报错就透传真实状态码；超时就先回
            # 200 开流，之后的错误以 SSE 错误事件发出。信号量持有到流结束。
            cors = {"Access-Control-Allow-Origin": "*", "Access-Control-Allow-Headers": "*"}

            async def open_upstream():
                await acquire_slot()
                try:
                    req = _stream_http_client.build_request(
                        method=request.method, url=upstream, headers=headers,
                        content=body, params=request.query_params,
                    )
                    return await _stream_http_client.send(req, stream=True)
                except BaseException:
                    semaphore.release()
                    raise

            upstream_task = asyncio.ensure_future(open_upstream())
            done, _ = await asyncio.wait({upstream_task}, timeout=_STREAM_HEADER_WAIT)
            if upstream_task in done and not upstream_task.cancelled() and upstream_task.exception() is None:
                early = upstream_task.result()
                if early.status_code >= 400:
                    try:
                        err_body = await early.aread()
                    finally:
                        await early.aclose()
                        semaphore.release()
                    if early.status_code != 429 and early.status_code < 500:
                        _save_failed_request(body, model_name, early.status_code)
                    return Response(
                        content=err_body,
                        status_code=early.status_code,
                        media_type=early.headers.get("content-type", "application/json"),
                        headers=cors,
                    )

            if upstream_task.done() and upstream_task.exception() is not None:
                raise upstream_task.exception()  # 3 秒内就连不上：交给外层返回 502

            state = {"started": False}

            async def event_stream():
                state["started"] = True
                try:
                    resp = await upstream_task
                except httpx.HTTPError as e:
                    # 响应头已发出，只能用错误事件告知；名额已在 open_upstream 里释放
                    print(f"[{time.strftime('%H:%M:%S')}] [WARN] 连接 LiteLLM 失败（{type(e).__name__}）：{model_name}")
                    yield _sse_error_event(path, 502, f"转发至 LiteLLM 失败: {e}")
                    return
                try:
                    if resp.status_code >= 400:
                        err_body = await resp.aread()
                        if resp.status_code != 429 and resp.status_code < 500:
                            _save_failed_request(body, model_name, resp.status_code)
                            yield _sse_error_event(path, resp.status_code, err_body)
                        else:
                            # 429/5xx 属于可重试错误：直接结束流，客户端会自行重发
                            print(f"[{time.strftime('%H:%M:%S')}] [WARN] 上游返回 {resp.status_code}（已开流，结束流让客户端重试）：{model_name}")
                        return
                    async for chunk in resp.aiter_raw():
                        yield chunk
                except httpx.HTTPError as e:
                    # 上游流中途断开：不让异常冒到 uvicorn 刷整段栈；直接结束流，客户端
                    # （Codex）会把缺少 response.completed 当作可重试错误自行重发。
                    print(f"[{time.strftime('%H:%M:%S')}] [WARN] 上游流中途断开（{type(e).__name__}）：{model_name}")
                finally:
                    await resp.aclose()
                    semaphore.release()

            return _CleanupStreamingResponse(
                event_stream(),
                media_type="text/event-stream",
                headers=cors,
                on_finish=lambda: None if state["started"] else _abandon_upstream(upstream_task, semaphore),
            )

        else:
            await acquire_slot()
            try:
                resp = await _http_client.request(
                    method=request.method,
                    url=upstream,
                    headers=headers,
                    content=body,
                    params=request.query_params,
                )
            finally:
                semaphore.release()
            if 400 <= resp.status_code < 500 and resp.status_code != 429:
                _save_failed_request(body, model_name, resp.status_code)
            resp_headers = {
                k: v for k, v in resp.headers.items()
                if k.lower() not in ("transfer-encoding", "content-length")
            }
            resp_headers["Access-Control-Allow-Origin"] = "*"
            resp_headers["Access-Control-Allow-Headers"] = "*"
            return Response(content=resp.content, status_code=resp.status_code, headers=resp_headers)

    except Exception as e:
        print(f"[ERROR] 转发至 LiteLLM 失败: {type(e).__name__}: {e}")
        return JSONResponse(
            {"error": {"message": "内部模型服务暂时不可用", "type": "proxy_error"}},
            status_code=502,
        )


def kill_port(port: int):
    """仅杀死监听该端口的进程。"""
    try:
        if sys.platform == "win32":
            result = subprocess.run(
                f'netstat -ano | findstr ":{port} "',
                capture_output=True, text=True, shell=True
            )
            pids = set()
            for line in result.stdout.splitlines():
                parts = line.strip().split()
                if len(parts) >= 5 and f":{port}" in parts[1]:
                    pids.add(parts[-1])
            for pid in pids:
                subprocess.run(f"taskkill /F /PID {pid}", shell=True, capture_output=True)
        else:
            subprocess.run(
                f"lsof -ti tcp:{port} -s TCP:LISTEN | xargs kill -9 2>/dev/null; true",
                shell=True
            )
    except Exception:
        pass


# LiteLLM 子进程启动时会无条件 print ASCII banner 和逐条模型清单（proxy_server.py
# 里没有开关，LITELLM_DONT_SHOW_FEEDBACK_BOX 只管反馈方框、NO_DOCS 只管 /docs），
# 只能在这里按模式丢弃。
_LITELLM_BANNER_CHARS = ("██", "╚═", "╔═", "║", "╗", "╝")
_MODEL_LIST_HEADER = "Proxy initialized with Config, Set models:"
_MODEL_LIST_LINE_RE = re.compile(r"^\s{2,}[\w./:\-]+\s*$")

# 子进程输出静默这么久，就认为启动刷屏结束（供主进程延后打启动摘要用）
_LITELLM_QUIET_SECONDS = 1.5
_litellm_last_output_at = 0.0


def _tee_stream(stream, out_stream):
    def _run():
        global _litellm_last_output_at
        in_model_list = False
        for line in stream:
            _litellm_last_output_at = time.time()
            clean = _strip_ansi(line).rstrip("\n")

            # banner 的 ASCII 艺术行：整行只由方块/制表字符与空格组成
            stripped = clean.strip()
            if stripped and not stripped.strip("".join(_LITELLM_BANNER_CHARS) + " "):
                continue

            # 模型清单：表头之后的缩进模型名行，直到出现非模型名行为止
            if _MODEL_LIST_HEADER in clean:
                in_model_list = True
                continue
            if in_model_list:
                if not clean.strip() or _MODEL_LIST_LINE_RE.match(clean):
                    continue
                in_model_list = False

            out_stream.write(line)
            out_stream.flush()
    threading.Thread(target=_run, daemon=True).start()


def wait_for_litellm_quiet(timeout: float = 20.0):
    """等 LiteLLM 子进程的启动输出安静下来，避免主进程摘要被夹在它的刷屏中间。"""
    deadline = time.time() + timeout
    while time.time() < deadline:
        if _litellm_last_output_at and time.time() - _litellm_last_output_at >= _LITELLM_QUIET_SECONDS:
            return
        time.sleep(0.1)


def start_litellm(config_override: dict = None):
    global _litellm_proc
    kill_port(LITELLM_PORT)
    time.sleep(0.3)
    
    env = os.environ.copy()
    env_file_vars = provider_manager.load_env_vars()
    env.update(env_file_vars)
    
    # 动态载入配置里的凭证
    cfg = config_override if config_override is not None else provider_manager.load_config()
    for p in cfg.get("providers", []):
        if p.get("id") == "gemini" and p.get("api_keys"):
            env["GEMINI_API_KEY"] = p["api_keys"][0]
        elif p.get("id") == "bedrock" and p.get("extra", {}).get("bearer_token"):
            env["AWS_BEARER_TOKEN_BEDROCK"] = p["extra"]["bearer_token"]
            env["AWS_REGION"] = p["extra"].get("region", "us-west-2")

    env["DISABLE_AIOHTTP_TRANSPORT"] = "True"
    # 降噪：不打 LiteLLM 的 ASCII banner 和启动时逐条刷出的模型清单
    env["LITELLM_DONT_SHOW_FEEDBACK_BOX"] = "True"
    env["NO_DOCS"] = "True"
    cmd = [LITELLM_BIN, "--config", CONFIG_PATH, "--port", str(LITELLM_PORT), "--host", "127.0.0.1"]
    # LiteLLM 默认单进程：每个请求的 Responses↔Chat 格式转换、日志回调都在同一个事件
    # 循环里做，Codex 这类每轮都回传完整历史（上百 KB～MB 级）的客户端并发时会互相
    # 阻塞，表现为「多开一个窗口就变慢」。多 worker 让这部分 CPU 工作并行。
    workers = int(cfg.get("server", {}).get("litellm_workers", 2))
    if workers > 1:
        # uvicorn 多进程默认只给新 worker 5 秒完成启动，超时就杀掉重开。LiteLLM 单个
        # worker 冷启动实测约 60 秒（导入 + 加载配置），于是 worker 永远起不来、每几秒
        # 被杀一次，日志刷 "Child process [...] died"。放宽到 180 秒。
        cmd += ["--num_workers", str(workers), "--timeout_worker_healthcheck", "180"]
    _litellm_proc = subprocess.Popen(cmd, env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    _tee_stream(_litellm_proc.stdout, sys.stdout)


def restart_litellm_subproc(config_override: dict = None, timeout: float = 180.0) -> bool:
    global _litellm_proc
    print(f"[{time.strftime('%H:%M:%S')}] 重载 LiteLLM 路由配置...")
    if _litellm_proc:
        try:
            _litellm_proc.terminate()
        except Exception:
            pass
    kill_port(LITELLM_PORT)
    start_litellm(config_override)
    return wait_for_litellm(timeout=timeout)


def wait_for_litellm(timeout=180):
    import urllib.request
    deadline = time.time() + timeout
    while time.time() < deadline:
        if _litellm_proc is not None and _litellm_proc.poll() is not None:
            return False
        try:
            urllib.request.urlopen(f"http://127.0.0.1:{LITELLM_PORT}/health/liveliness", timeout=2)
            return True
        except Exception:
            time.sleep(0.5)
    return False


if __name__ == "__main__":
    kill_port(PROXY_PORT)
    log_path = setup_logging()

    cfg = provider_manager.load_config()
    provider_manager.generate_litellm_yaml(cfg)

    start_litellm()
    ready = wait_for_litellm()
    # 子进程的启动输出是异步刷出来的，等它安静再打摘要，否则会被夹在中间
    wait_for_litellm_quiet()

    _concurrency_desc = f"默认 {_DEFAULT_CONCURRENCY}" + (
        "，" + "、".join(f"{k}={v}" for k, v in _PROVIDER_CONCURRENCY.items()) if _PROVIDER_CONCURRENCY else ""
    )
    print(f"控制台   http://localhost:{PROXY_PORT}/")
    print(f"API      http://localhost:{PROXY_PORT}/v1")
    print(f"并发上限 {_concurrency_desc}")
    print(f"日志     {log_path}")
    if not ready:
        print("警告     LiteLLM 未在 180 秒内就绪，仍继续启动")
    print()

    uvicorn.run(app, host=PROXY_HOST, port=PROXY_PORT, log_level="warning", access_log=False)
