#!/usr/bin/env python3
"""
冒烟测试：验证 proxy.py 里几个已知修复点没有回归。
运行前需要 proxy.py 已经跑起来（先执行 ./restart.sh）。
纯标准库实现，不依赖 requirements.txt 之外的包。
"""

import json
import sys
import urllib.error
import urllib.request

BASE = "http://127.0.0.1:4000"
TIMEOUT = 60

results = []


def post(path: str, payload: dict) -> tuple[int, dict | str]:
    data = json.dumps(payload).encode()
    req = urllib.request.Request(
        f"{BASE}{path}",
        data=data,
        headers={"Content-Type": "application/json", "Authorization": "Bearer sk-test"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
            body = resp.read().decode()
            try:
                return resp.status, json.loads(body)
            except json.JSONDecodeError:
                return resp.status, body
    except urllib.error.HTTPError as e:
        body = e.read().decode()
        try:
            return e.code, json.loads(body)
        except json.JSONDecodeError:
            return e.code, body


def get(path: str) -> tuple[int, dict | str]:
    req = urllib.request.Request(
        f"{BASE}{path}",
        headers={"Authorization": "Bearer sk-test"},
    )
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
            body = resp.read().decode()
            return resp.status, json.loads(body)
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode()


def record(name: str, ok: bool, detail: str = ""):
    results.append((name, ok, detail))
    tag = "PASS" if ok else "FAIL"
    print(f"[{tag}] {name}" + (f" — {detail}" if detail and not ok else ""))


def is_bedrock_model(model_id: str) -> bool:
    return "us.anthropic." in model_id or "global.anthropic." in model_id


# 挑选一个"普通聊天模型"用于冒烟测试，避开已知需要特殊 API/权限的型号：
# antigravity/deep-research/computer-use 等只支持专用接口；global.anthropic 部分型号
# 的默认 data retention 模式不可用。优先选 us.anthropic.claude-sonnet-4* 与
# gemini-2.5-flash / gemini-3.5-flash 这类稳定型号。
def pick_model(models: list[str], preferred_substrings: list[str], avoid_substrings: list[str]) -> str | None:
    candidates = [m for m in models if not any(a in m for a in avoid_substrings)]
    for pref in preferred_substrings:
        for m in candidates:
            if pref in m:
                return m
    return candidates[0] if candidates else None


def main():
    status, body = get("/v1/models")
    if status != 200 or not isinstance(body, dict) or "data" not in body:
        record("拉取模型列表 (/v1/models)", False, f"status={status} body={body}")
        print("\n无法获取模型列表，后续测试跳过。请确认 proxy.py 已启动。")
        sys.exit(1)
    record("拉取模型列表 (/v1/models)", True)

    model_ids = [m["id"] for m in body["data"]]
    gemini_models = [m for m in model_ids if not is_bedrock_model(m)]
    bedrock_models = [m for m in model_ids if is_bedrock_model(m)]

    if not gemini_models:
        record("存在可用 Gemini 模型", False, "模型列表为空")
    if not bedrock_models:
        record("存在可用 Bedrock 模型", False, "模型列表为空（若未配置 AWS_BEARER_TOKEN_BEDROCK 属正常，跳过相关测试）")

    GEMINI_AVOID = ("antigravity", "deep-research", "computer-use", "image", "omni")
    # global.* 部分型号默认 data retention 不可用；us.anthropic.claude-*-4-20250514（无 -4-5
    # 后缀的旧版）已被 AWS 标记 legacy 禁用，避开这两类，优先选 4-5/haiku 系列。
    BEDROCK_AVOID = ("global.anthropic", "claude-3-", "-4-20250514")
    gemini_model = pick_model(gemini_models, ["gemini-2.5-flash", "gemini-3"], GEMINI_AVOID)
    bedrock_model = pick_model(bedrock_models, ["claude-sonnet-4-5", "claude-haiku-4-5", "claude-sonnet-4"], BEDROCK_AVOID)

    # 2. Gemini 基础调用
    if gemini_model:
        status, body = post("/v1/chat/completions", {
            "model": gemini_model,
            "messages": [{"role": "user", "content": "只回复一个字：ok"}],
            "stream": False,
        })
        ok = status == 200 and isinstance(body, dict) and body.get("choices")
        record(f"Gemini 基础调用 ({gemini_model})", ok, f"status={status} body={body}")

    # 3. Bedrock 基础调用
    if bedrock_model:
        status, body = post("/v1/chat/completions", {
            "model": bedrock_model,
            "messages": [{"role": "user", "content": "只回复一个字：ok"}],
            "stream": False,
        })
        ok = status == 200 and isinstance(body, dict) and body.get("choices")
        record(f"Bedrock 基础调用 ({bedrock_model})", ok, f"status={status} body={body}")

    # 4. Bedrock：缺失 tools 字段 + 以 tool 消息结尾，验证 toolConfig 重建与 tool_call_id 规范化
    if bedrock_model:
        long_tool_call_id = "call_" + "x" * 80  # 超过 64 字符，触发 id 规范化
        status, body = post("/v1/chat/completions", {
            "model": bedrock_model,
            "messages": [
                {"role": "user", "content": "现在几点？"},
                {
                    "role": "assistant",
                    "content": None,
                    "tool_calls": [{
                        "id": long_tool_call_id,
                        "type": "function",
                        "function": {"name": "get_time", "arguments": "{}"},
                    }],
                },
                {"role": "tool", "tool_call_id": long_tool_call_id, "content": "14:00"},
            ],
            "stream": False,
        })
        ok = status == 200 and isinstance(body, dict) and body.get("choices")
        record("Bedrock 缺失 tools + 超长 tool_call_id 重建", ok, f"status={status} body={body}")

    # 5. Gemini：以 assistant 消息结尾，验证自动裁剪逻辑
    if gemini_model:
        status, body = post("/v1/chat/completions", {
            "model": gemini_model,
            "messages": [
                {"role": "user", "content": "你好"},
                {"role": "assistant", "content": "你好，有什么可以帮你？"},
            ],
            "stream": False,
        })
        ok = status == 200 and isinstance(body, dict) and body.get("choices")
        record(f"Gemini 末尾 assistant 消息自动裁剪 ({gemini_model})", ok, f"status={status} body={body}")

    print()
    failed = [r for r in results if not r[1]]
    if failed:
        print(f"共 {len(failed)}/{len(results)} 项失败。")
        sys.exit(1)
    print(f"全部 {len(results)} 项通过。")


if __name__ == "__main__":
    main()
