# LLM Transit Gateway

[English](README_EN.md) | **简体中文**

让 Gemini、Vertex AI、Claude、OpenAI 等模型通过统一接口接入常用 AI 工具。

LLM Transit Gateway 是一个本地优先的多厂商兼容网关。它向客户端统一提供 OpenAI 兼容的 `/v1/chat/completions` 与 `/v1/responses` 接口，后端可同时连接 Gemini API、Google Vertex AI、AWS Bedrock、OpenAI、Anthropic 和自定义 OpenAI 兼容线路。

## 核心亮点

- **让 Gemini 用在更多 AI 工具中**：把 Gemini API 和 Vertex AI 模型统一转换为常见客户端能够识别的接口，可供 cc-switch、Cursor、VS Code 插件、OpenAI SDK 等工具使用。
- **不是简单转发，而是兼容层**：修复不同客户端、LiteLLM 和上游厂商之间的请求差异，包括 Gemini 思维参数、tool call、结构化输出、消息尾部角色和无效字段。
- **Gemini 思维签名与长会话兼容**：识别并清理跨模型历史中无意义的 `__thought__` 签名后缀，递归解包 LiteLLM 的 `encitem_` ID，并保持 tool call 引用关系一致，避免 Responses API 长会话因 ID 超长或字符非法而中断。
- **多 Vertex 项目独立管理**：一个服务账号 JSON 对应一个独立网关实例，各自拥有凭证、模型权限、调用前缀和并发限制；同名模型可以显式指定默认路由，不会误入错误项目。
- **针对 Agent 长会话优化**：自动修复损坏的工具参数、重建 Bedrock `toolConfig`、规范化 tool call ID，并处理流式首包超时、真实状态码透传和中途断流。
- **可视化配置和排障**：通过 Web 控制台添加、删除和配置厂商，一键导入 Vertex 服务账号 JSON、拉取模型、测试全部对外接口并查看实时日志。

## 架构

```
客户端 (Claude Code / cc-switch / OpenAI SDK / VSCode / Cursor 等)
        │  Base URL: http://localhost:4000/v1
        ▼
┌─────────────────────────────┐
│  proxy.py (FastAPI, :4000)  │  · Web 管理控制台
│  - 请求体清理/修复          │  · 管理 API（增删厂商、拉取模型、重启）
│  - 按厂商分组的并发限流     │  · 实时日志推送（SSE）
│  - 转发到 LiteLLM           │
└──────────────┬──────────────┘
               ▼
┌─────────────────────────────┐
│  LiteLLM Proxy (:4001)      │  · 统一路由到各厂商 SDK
│  - 重试 / mid-stream 兜底   │
└──────────────┬──────────────┘
               ▼
     Gemini / Vertex AI / Bedrock / OpenAI / Anthropic / 自定义 OpenAI 兼容线路
```

两层网关的分工：`proxy.py` 负责客户端请求进来之后的清理和管理面（Web UI、配置持久化、并发控制），`LiteLLM` 负责真正对接各厂商 SDK、做重试和 fallback。

## 功能详情

- **多厂商聚合**：Gemini、Vertex AI（Service Account）、AWS Bedrock（Bearer Token）、OpenAI、Anthropic Direct，以及任意数量的自定义 OpenAI 兼容线路（中转站、SiliconFlow 等）。
- **Web 管理控制台**（`http://localhost:4000/`）：
  - 按厂商配置 API Key / Base URL / 专属凭证字段
  - 可创建多个 Vertex AI 项目实例：每个服务账号 JSON 对应独立凭证、模型集合和调用前缀
  - 一键拉取厂商模型列表，勾选启用哪些模型
  - 全局设置：默认并发上限、按厂商单独覆盖并发上限、重试次数/间隔、请求超时
  - 内置连通性测试面板
  - 实时日志流（SSE），只展示 WARNING/ERROR 及关键事件
- **请求体自动修复**（`proxy.py` 里的 `_clean_body`）：
  - 规范化 Gemini 的 `reasoning_effort` / `thinking_level` 别名（如 `xhigh` → `high`）
  - 处理 Gemini 思维签名与 LiteLLM 嵌套编码 ID，保证跨模型历史满足 Responses API 的长度和字符集约束
  - 修复历史消息中损坏的 tool call JSON 参数
  - 清理空/非法的 `tools` 字段
  - Gemini 要求对话以 user 消息结尾，自动裁剪末尾的 assistant 消息
  - Bedrock 专项修复：tool call ID 规范化（≤64 字符）、缺失 toolConfig 自动重建、超限图片自动缩放（多图请求单图长边 ≤2000px）、剔除 Bedrock 不支持的参数
- **按厂商分组的并发限流**：不同厂商的并发配额互不挤占（例如 Vertex 项目在高并发下容易 429，可以单独限制，不会拖累其他厂商）。在 Web 页面调整后无需重启整个网关进程即可生效。
- **自动重试与 mid-stream 兜底**：所有厂商模型都配置了 LiteLLM 的 fallback（重试同一个模型），网络抖动导致的连接中断能自动恢复。
- **实时日志广播 + 落盘**：控制台过滤纯噪音行，日志文件只记录 WARNING/ERROR/CRITICAL 及其 traceback，按天分文件，支持保留天数自动清理。

## 快速开始

### 依赖

- Python 3.11（`start.sh` 会自动探测 conda 环境）
- 见 [`requirements.txt`](requirements.txt)：`litellm[proxy]`、`python-dotenv`、`Pillow`

### 安装

```bash
./install.sh
# 等价于：conda run -n py311 pip install -r requirements.txt
```

### 启动

```bash
./start.sh
```

`start.sh` 会自动探测同时满足「Python 3.11」和「同级目录装有 litellm」的 conda 环境；也可以用 `PYTHON_BIN=/path/to/python ./start.sh` 手动指定。首次启动会在当前目录生成 `providers_config.json`（默认全部厂商禁用），需要去 Web 控制台或直接编辑该文件填入凭证。

启动后：

- Web 控制台：`http://localhost:4000/`
- API Base URL：`http://localhost:4000/v1`

### 配置厂商

打开 `http://localhost:4000/`，在左侧选择厂商 → 填入 API Key（或 Vertex/Bedrock 的专属凭证字段）→ 点击「拉取模型」→ 勾选需要启用的模型 → 打开厂商启用开关 → 「保存并应用」。保存后会自动重新生成 `litellm_config.yaml` 并重启 LiteLLM 子进程。

需要接入多个 Vertex 项目时，点击「添加网关」→「Google Vertex AI」，为每个项目分别导入服务账号 JSON。每个实例必须使用唯一调用前缀，例如 `vertex_google/`、`vertex_claude/`。拉取结果来自各 publisher 的 Model Garden 候选列表，并不等于账号一定有调用权限；请保存后用「API 接口」逐个确认。

也可以直接编辑 `providers_config.json`（参考 [`env_demo`](env_demo) 了解可迁移的环境变量），字段结构：

```jsonc
{
  "server": {
    "proxy_host": "127.0.0.1",            // 默认仅本机可访问；确需局域网访问时再显式修改
    "proxy_port": 4000,
    "litellm_port": 4001,
    "log_retention_days": 7,
    "request_timeout": 600,
    "num_retries": 3,
    "retry_after": 1,
    "allowed_fails": 3,
    "default_concurrency": 16,          // 未单独配置的厂商统一用这个并发上限
    "provider_concurrency": { "vertex_google": 4 }, // 按厂商实例 id 覆盖
    "bare_model_routes": { "gemini-2.5-pro": "vertex_google" } // 重名模型的裸名默认实例
  },
  "providers": [ /* 每个厂商的 id/type/enabled/api_keys/base_url/models/selected_models/extra */ ]
}
```

### 客户端接入

任意支持 OpenAI 兼容 API 的客户端（Claude Code、cc-switch、Cursor、VSCode 插件、OpenAI SDK 等）：

- Base URL：`http://localhost:4000/v1`
- API Key：任意非空 Bearer 字符串（网关只检查非空，鉴权交由上游厂商凭证）
- Model：优先使用 `厂商id/模型名`（如 `vertex_google/gemini-2.5-pro`）。裸模型名只在全局唯一时自动提供；同一模型存在于多个实例时，必须在控制台显式指定默认实例，否则不会注册该裸名，避免请求误入错误项目。

## 运维

| 脚本 | 作用 |
|---|---|
| `./start.sh` | 探测 Python 环境并启动网关（前台运行，Ctrl+C 退出） |
| `./stop.sh` | 停止配置文件中 proxy_port/litellm_port 对应的进程 |
| `./restart.sh` | 等价于重新执行 `start.sh` |
| `python smoketest.py` | 冒烟测试（需要网关已在运行），验证 Gemini/Bedrock 基础调用、tool call 修复、消息裁剪等已知修复点没有回归 |

日志文件位于 `logs/`，按天分文件（同一天多次启动会追加序号），仅记录 WARNING 及以上级别；控制台输出可通过 Web 控制台的「实时日志」面板查看。

## 已知行为 / 排错

- **LiteLLM 内部 `Task exception was never retrieved` + `AttributeError: 'dict' object has no attribute 'usage'`**：LiteLLM 自身在组装流式响应统计信息时的已知内部问题，不影响请求结果（请求仍返回 200），`proxy.py` 的日志过滤器（`TeeStream`）会自动屏蔽，不会写入日志文件或打印到控制台。
- **`MidStreamFallbackError` / `Vertex_ai_betaException`**：这是上游连接在流式传输中途真实中断（多为 Vertex 网络抖动），不是无害噪音。LiteLLM 的 fallback 机制会自动重试同一个模型，客户端通常感知不到（网关日志会看到一次 500 紧跟一次 200）。如果频率明显升高，考虑调低该厂商的并发上限。
- **并发限制按厂商分组**：如果发现某个厂商的请求排队明显，去 Web 控制台「全局设置」调整该厂商的并发上限或默认并发上限，保存后立即生效，无需重启进程。

## 安全注意事项

- `providers_config.json`、`vertex_sa_*.json`、`.env`、`litellm_config.yaml`、`logs/` 均包含真实凭证或已被 `.gitignore` 排除，**不会**被提交到仓库。首次部署需要自行创建这些文件或通过 Web 控制台填入。
- 网关默认只监听 `127.0.0.1`。推理接口只检查 Bearer Token 非空，不校验身份；管理接口额外限制为同源浏览器或本机脚本调用，但仍不等同于用户认证。如需把 `proxy_host` 改为 `0.0.0.0`，必须在前面增加带认证的反向代理，不要直接暴露到公网。
