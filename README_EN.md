# LLM Transit Gateway

**English** | [简体中文](README.md)

[![CI](https://github.com/ml451953014/llm-transit-gateway/actions/workflows/ci.yml/badge.svg)](https://github.com/ml451953014/llm-transit-gateway/actions/workflows/ci.yml)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)
![Python 3.11](https://img.shields.io/badge/Python-3.11-blue.svg)

Bring Gemini, Vertex AI, Claude, OpenAI, and other models into the AI tools you already use through one unified API.

LLM Transit Gateway is a local-first, multi-provider compatibility gateway. It exposes OpenAI-compatible `/v1/chat/completions` and `/v1/responses` endpoints while connecting to the Gemini API, Google Vertex AI, AWS Bedrock, OpenAI, Anthropic, and custom OpenAI-compatible services.

## Highlights

- **Use Gemini in more AI tools**: expose Gemini API and Vertex AI models through endpoints understood by clients such as cc-switch, Cursor, VS Code extensions, and the OpenAI SDK.
- **A compatibility layer, not just a proxy**: normalize differences between clients, LiteLLM, and upstream providers, including Gemini thinking parameters, tool calls, structured output, trailing message roles, and unsupported fields.
- **Gemini thought-signature and long-session compatibility**: remove irrelevant `__thought__` signature suffixes from cross-model history, recursively unwrap LiteLLM `encitem_` IDs, and preserve tool-call references so long Responses API sessions do not fail because of invalid or oversized IDs.
- **Independent management for multiple Vertex projects**: each service-account JSON file becomes a separate gateway instance with its own credentials, model permissions, routing prefix, and concurrency limit. Duplicate model names can be assigned an explicit default route.
- **Optimized for long-running agent sessions**: repair malformed tool arguments, rebuild Bedrock `toolConfig`, normalize tool-call IDs, and handle slow streaming headers, real upstream status codes, and mid-stream disconnects.
- **Visual configuration and troubleshooting**: add, remove, and configure providers from the Web console; import Vertex service-account JSON files, fetch models, test every public endpoint, and inspect live logs.

## Architecture

```text
Clients (Claude Code / cc-switch / OpenAI SDK / VS Code / Cursor / ...)
        │  Base URL: http://localhost:4000/v1
        ▼
┌─────────────────────────────┐
│  proxy.py (FastAPI, :4000)  │  · Web management console
│  - Request normalization    │  · Management API (providers, models, restart)
│  - Per-provider concurrency │  · Live log streaming (SSE)
│  - Forwarding to LiteLLM    │
└──────────────┬──────────────┘
               ▼
┌─────────────────────────────┐
│  LiteLLM Proxy (:4001)      │  · Unified routing to provider SDKs
│  - Retry / stream fallback  │
└──────────────┬──────────────┘
               ▼
     Gemini / Vertex AI / Bedrock / OpenAI / Anthropic / custom endpoints
```

The two gateway layers have separate responsibilities: `proxy.py` handles request normalization and the management plane (Web UI, configuration persistence, and concurrency control), while LiteLLM connects to provider SDKs and performs retries and fallbacks.

## Features

- **Multi-provider aggregation**: Gemini, Vertex AI with service accounts, AWS Bedrock with bearer tokens, OpenAI, direct Anthropic access, and any number of custom OpenAI-compatible services.
- **Web management console** at `http://localhost:4000/`:
  - Configure API keys, base URLs, and provider-specific credentials.
  - Create multiple Vertex AI project instances, each with separate credentials, model sets, and routing prefixes.
  - Fetch provider model lists and choose which models to enable.
  - Configure global and per-provider concurrency limits, retries, retry intervals, and request timeouts.
  - Test all public API endpoints.
  - Stream important WARNING/ERROR events through SSE.
- **Automatic request repair** in `_clean_body`:
  - Normalize Gemini `reasoning_effort` and `thinking_level` aliases such as `xhigh` → `high`.
  - Process Gemini thought signatures and nested LiteLLM IDs so cross-model history satisfies Responses API length and character constraints.
  - Repair malformed JSON arguments in historical tool calls.
  - Remove empty or invalid `tools` fields.
  - Trim trailing assistant/model messages when Gemini requires the conversation to end with a user message.
  - Apply Bedrock-specific fixes: normalize tool-call IDs to ≤64 characters, rebuild missing `toolConfig`, resize images beyond the 2000px limit, and remove unsupported parameters.
- **Per-provider concurrency limits**: one provider's quota does not block the others. Limits can be changed in the Web console without restarting the gateway process.
- **Automatic retries and mid-stream fallback**: LiteLLM retries the same model when upstream network connections fail.
- **Live and persisted logs**: the console filters noise, while WARNING/ERROR/CRITICAL events and tracebacks are written to daily log files with configurable retention.

## Quick Start

### Requirements

- Python 3.11 (`start.sh` automatically searches for a compatible Conda environment)
- Dependencies from [`requirements.txt`](requirements.txt): `litellm[proxy]`, `python-dotenv`, and `Pillow`

### Install

```bash
./install.sh
# Equivalent to: conda run -n py311 pip install -r requirements.txt
```

### Start

```bash
./start.sh
```

`start.sh` looks for a Python 3.11 environment that also has LiteLLM installed. You can select one explicitly with `PYTHON_BIN=/path/to/python ./start.sh`. On first launch, the gateway creates `providers_config.json` with all providers disabled. Add credentials through the Web console or edit the file locally.

After startup:

- Web console: `http://localhost:4000/`
- API Base URL: `http://localhost:4000/v1`

### Configure Providers

Open `http://localhost:4000/`, select a provider, enter its API key or provider-specific credentials, fetch its models, select the models to enable, turn on the provider, and click **Save & Apply**. The gateway regenerates `litellm_config.yaml` and restarts the LiteLLM child process automatically.

To connect multiple Vertex projects, choose **Add Gateway → Google Vertex AI** and import one service-account JSON file for each project. Every instance must have a unique routing prefix, such as `vertex_google/` or `vertex_claude/`. The fetched publisher catalog is a candidate list, not proof that the account can call every model; verify access with the built-in API tests after saving.

You can also edit `providers_config.json` directly. See [`env_demo`](env_demo) for supported environment-variable migration. The configuration shape is:

```jsonc
{
  "server": {
    "proxy_host": "127.0.0.1",            // Localhost only by default
    "proxy_port": 4000,
    "litellm_port": 4001,
    "log_retention_days": 7,
    "request_timeout": 600,
    "num_retries": 3,
    "retry_after": 1,
    "allowed_fails": 3,
    "default_concurrency": 16,
    "provider_concurrency": { "vertex_google": 4 },
    "bare_model_routes": { "gemini-2.5-pro": "vertex_google" }
  },
  "providers": [ /* id/type/enabled/api_keys/base_url/models/selected_models/extra */ ]
}
```

### Connect a Client

For any client that accepts an OpenAI-compatible API, including Claude Code, cc-switch, Cursor, VS Code extensions, and the OpenAI SDK:

- Base URL: `http://localhost:4000/v1`
- API Key: any non-empty Bearer string. The gateway only checks that it is present; upstream authentication uses the provider credentials.
- Model: prefer `provider-id/model-name`, for example `vertex_google/gemini-2.5-pro`. A bare model name is registered automatically only when it is globally unique. If multiple instances expose the same model, choose the default instance explicitly in the console.

## Open-source Ecosystem and Compatible Clients

This project uses [LiteLLM](https://github.com/BerriAI/litellm) as its upstream provider-routing layer and exposes standard model-list, Chat Completions, and Responses endpoints. Open-source clients that accept a custom OpenAI Base URL can usually connect directly:

| Open-source project | Connection |
|---|---|
| [CC Switch](https://github.com/farion1231/cc-switch) | Add the gateway as a custom provider for Claude Code, Codex, Gemini CLI, and other tools. |
| [Open WebUI](https://github.com/open-webui/open-webui) | Add an OpenAI-compatible connection with `http://localhost:4000/v1` as the Base URL. |
| [Cline](https://github.com/cline/cline) | Select **OpenAI Compatible**, then enter the Base URL, any non-empty API key, and a model ID. |
| [Continue](https://github.com/continuedev/continue) | Use the `openai` provider and point `apiBase` at this gateway. |
| [Cherry Studio](https://github.com/CherryHQ/cherry-studio) | Add a custom OpenAI-compatible provider with the gateway URL. |

Compatibility here covers model discovery and text/tool-calling endpoints. The gateway does not currently expose embeddings, speech, or image-generation APIs. Clients may send different extension fields; please report reproducible incompatibilities with all sensitive data removed.

## Operations

| Command | Purpose |
|---|---|
| `./start.sh` | Detect a Python environment and run the gateway in the foreground. |
| `./stop.sh` | Stop processes using the configured `proxy_port` and `litellm_port`. |
| `./restart.sh` | Start the gateway again. |
| `python smoketest.py` | With the gateway running, verify Gemini/Bedrock calls, tool-call repairs, message trimming, and other regressions. |

Logs are stored in `logs/` as daily files. The Web console also exposes a live-log panel.

## Known Behavior and Troubleshooting

- **LiteLLM `Task exception was never retrieved` with `AttributeError: 'dict' object has no attribute 'usage'`**: this is an internal LiteLLM error while assembling streaming usage statistics. It does not affect a successful response. `TeeStream` filters this noise from the console and log files.
- **`MidStreamFallbackError` / `Vertex_ai_betaException`**: this indicates a real upstream connection interruption, often caused by Vertex network instability. LiteLLM retries the same model. If it happens frequently, reduce that provider's concurrency limit.
- **Per-provider queueing**: if one provider appears slow, adjust its concurrency limit under **Global Settings**. Changes take effect immediately without restarting the gateway process.

## Security Notes

- `providers_config.json`, `vertex_sa_*.json`, `.env`, `litellm_config.yaml`, and `logs/` can contain real credentials and are excluded by `.gitignore`. Do not force-add them to Git.
- The gateway listens on `127.0.0.1` by default. Inference endpoints only require a non-empty Bearer token, and management endpoints are restricted to same-origin browser requests or local scripts; this is not full user authentication. If you change `proxy_host` to `0.0.0.0`, place an authenticated reverse proxy in front of the gateway and never expose port 4000 directly to the public Internet.

## Contributing and License

Read [`CONTRIBUTING.md`](CONTRIBUTING.md) before opening an issue or pull request. Report security issues privately by following [`SECURITY.md`](SECURITY.md), not through a public issue.

This project is licensed under the [MIT License](LICENSE).
