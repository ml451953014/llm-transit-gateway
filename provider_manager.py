import json
import logging
import os
import re
import tempfile
import urllib.request
import urllib.error
from pathlib import Path
from typing import Dict, List, Any, Optional
from urllib.parse import urlsplit

logger = logging.getLogger("provider_manager")

BASE_DIR = Path(__file__).parent.resolve()
CONFIG_FILE = BASE_DIR / "providers_config.json"
LITELLM_YAML_PATH = BASE_DIR / "litellm_config.yaml"
ENV_FILE = BASE_DIR / ".env"

DEFAULT_PROVIDERS_CONFIG = {
    "server": {
        "proxy_host": "127.0.0.1",
        "proxy_port": 4000,
        "litellm_port": 4001,
        "log_retention_days": 7,
        "request_timeout": 600,
        "num_retries": 3,
        "retry_after": 1,
        "allowed_fails": 3,
        # 并发上限按厂商分组：未在 provider_concurrency 中单独配置的厂商统一走
        # default_concurrency。Vertex 项目在更高并发下会返回 429，4 是实测可稳定
        # 跑的上限；其余厂商通常没有这个限制，给了更宽松的默认值。
        "default_concurrency": 16,
        # LiteLLM worker 进程数，见 proxy.py start_litellm()
        "litellm_workers": 2,
        "provider_concurrency": {
            "vertex_ai": 4
        },
        # 同一模型出现在多个已启用实例时，必须显式指定裸模型名的归属。
        "bare_model_routes": {}
    },
    "providers": [
        {
            "id": "gemini",
            "name": "Google Gemini",
            "type": "gemini",
            "enabled": True,
            "api_keys": [],
            "base_url": "",
            "models": [],
            "selected_models": [],
            "extra": {}
        },
        {
            "id": "deepseek",
            "name": "DeepSeek",
            "type": "openai_compatible",
            "enabled": False,
            "api_keys": [],
            "base_url": "https://api.deepseek.com",
            "models": [],
            "selected_models": [],
            "extra": {}
        },
        {
            "id": "openai",
            "name": "OpenAI",
            "type": "openai",
            "enabled": False,
            "api_keys": [],
            "base_url": "https://api.openai.com/v1",
            "models": [],
            "selected_models": [],
            "extra": {}
        },
        {
            "id": "anthropic",
            "name": "Anthropic Claude (Direct)",
            "type": "anthropic",
            "enabled": False,
            "api_keys": [],
            "base_url": "https://api.anthropic.com",
            "models": [],
            "selected_models": [],
            "extra": {}
        },
        {
            "id": "bedrock",
            "name": "AWS Bedrock",
            "type": "bedrock",
            "enabled": False,
            "api_keys": [],
            "base_url": "",
            "models": [],
            "selected_models": [],
            "extra": {
                "bearer_token": "",
                "region": "us-west-2",
                "claude_only": True
            }
        },
        {
            "id": "vertex_ai",
            "name": "Google Vertex AI",
            "type": "vertex_ai",
            "enabled": False,
            "api_keys": [],
            "base_url": "",
            "models": [],
            "selected_models": [],
            "extra": {
                "project": "",
                "client_email": "",
                "private_key": "",
                "location": "us-central1"
            }
        }
    ]
}

_SUPPORTED_PROVIDER_TYPES = {
    "gemini", "openai", "openai_compatible", "anthropic", "bedrock", "vertex_ai"
}


def selected_models_for_provider(provider: Dict[str, Any]) -> List[str]:
    """返回明确选中的模型；仅旧配置缺少该字段时才回退到完整模型列表。"""
    selected = provider.get("selected_models")
    if selected is None:
        return provider.get("models") or []
    return selected


def bare_model_providers(config: Dict[str, Any]) -> Dict[str, str]:
    """计算裸模型名的唯一归属；重名模型没有显式选择时不暴露裸名。"""
    owners: Dict[str, List[str]] = {}
    for provider in config.get("providers", []):
        if not provider.get("enabled"):
            continue
        pid = provider.get("id")
        for model in selected_models_for_provider(provider):
            model_owners = owners.setdefault(model, [])
            if pid not in model_owners:
                model_owners.append(pid)

    explicit = config.get("server", {}).get("bare_model_routes", {}) or {}
    resolved = {}
    for model, provider_ids in owners.items():
        if len(provider_ids) == 1:
            resolved[model] = provider_ids[0]
        elif explicit.get(model) in provider_ids:
            resolved[model] = explicit[model]
    return resolved


def validate_config(config: Dict[str, Any]) -> None:
    """在写盘或生成 LiteLLM 配置前验证会影响运行的最小结构。"""
    if not isinstance(config, dict):
        raise ValueError("配置根节点必须是对象")

    server = config.get("server", {})
    if not isinstance(server, dict):
        raise ValueError("server 必须是对象")

    def require_int(name: str, minimum: int, maximum: Optional[int] = None):
        if name not in server:
            return
        value = server[name]
        if isinstance(value, bool) or not isinstance(value, int):
            raise ValueError(f"server.{name} 必须是整数")
        if value < minimum or (maximum is not None and value > maximum):
            suffix = f"~{maximum}" if maximum is not None else "以上"
            raise ValueError(f"server.{name} 必须在 {minimum}{suffix}")

    require_int("proxy_port", 1, 65535)
    require_int("litellm_port", 1, 65535)
    require_int("num_retries", 0)
    require_int("allowed_fails", 0)
    require_int("default_concurrency", 1)
    require_int("litellm_workers", 1)

    for name in ("request_timeout", "retry_after"):
        if name in server:
            value = server[name]
            minimum = 0 if name == "retry_after" else 0.000001
            if isinstance(value, bool) or not isinstance(value, (int, float)) or value < minimum:
                raise ValueError(f"server.{name} 必须是有效的非负数")

    proxy_host = server.get("proxy_host", "127.0.0.1")
    if not isinstance(proxy_host, str) or not proxy_host.strip():
        raise ValueError("server.proxy_host 必须是非空字符串")

    overrides = server.get("provider_concurrency", {}) or {}
    if not isinstance(overrides, dict):
        raise ValueError("server.provider_concurrency 必须是对象")
    for pid, value in overrides.items():
        if not isinstance(pid, str) or isinstance(value, bool) or not isinstance(value, int) or value < 1:
            raise ValueError("provider_concurrency 的厂商名必须是字符串，值必须是正整数")

    bare_routes = server.get("bare_model_routes", {}) or {}
    if not isinstance(bare_routes, dict):
        raise ValueError("server.bare_model_routes 必须是对象")
    for model, pid in bare_routes.items():
        if not isinstance(model, str) or not model.strip() or not isinstance(pid, str) or not pid.strip():
            raise ValueError("bare_model_routes 的模型名和厂商 id 必须是非空字符串")

    providers = config.get("providers", [])
    if not isinstance(providers, list):
        raise ValueError("providers 必须是数组")
    seen_ids = set()
    for index, provider in enumerate(providers):
        if not isinstance(provider, dict):
            raise ValueError(f"providers[{index}] 必须是对象")
        pid = provider.get("id")
        if not isinstance(pid, str) or not re.fullmatch(r"[a-z0-9_-]+", pid):
            raise ValueError(f"providers[{index}].id 只能包含小写字母、数字、下划线和连字符")
        if pid in seen_ids:
            raise ValueError(f"厂商 id 重复: {pid}")
        seen_ids.add(pid)
        if provider.get("type") not in _SUPPORTED_PROVIDER_TYPES:
            raise ValueError(f"不支持的厂商类型: {provider.get('type')}")
        if "enabled" in provider and not isinstance(provider["enabled"], bool):
            raise ValueError(f"providers[{index}].enabled 必须是布尔值")
        for field in ("name", "base_url"):
            if field in provider and not isinstance(provider[field], str):
                raise ValueError(f"providers[{index}].{field} 必须是字符串")
        for field in ("api_keys", "models", "selected_models"):
            value = provider.get(field)
            if value is None:
                continue
            if not isinstance(value, list) or any(not isinstance(item, str) for item in value):
                raise ValueError(f"providers[{index}].{field} 必须是字符串数组")
        if not isinstance(provider.get("extra", {}), dict):
            raise ValueError(f"providers[{index}].extra 必须是对象")


def _atomic_write_text(path: Path, content: str, mode: int = 0o600) -> None:
    """在同目录写临时文件并原子替换，避免崩溃留下半份配置。"""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(content)
            f.flush()
            os.fsync(f.fileno())
        os.chmod(tmp_name, mode)
        os.replace(tmp_name, path)
    finally:
        try:
            os.unlink(tmp_name)
        except FileNotFoundError:
            pass


def normalize_openai_api_base(base_url: str) -> str:
    """补齐缺失的版本路径，但保留已经带 /vN 或 /api/vN 的地址。"""
    clean = base_url.rstrip("/")
    if re.search(r"/(?:api/)?v\d+$", clean):
        return clean
    return f"{clean}/v1"


def _restrict_file_permissions(path: Path) -> None:
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass


def load_env_vars() -> Dict[str, str]:
    env = {}
    if ENV_FILE.exists():
        _restrict_file_permissions(ENV_FILE)
        with open(ENV_FILE, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#"):
                    continue
                if "=" in line:
                    k, v = line.split("=", 1)
                    env[k.strip()] = v.strip()
    return env


def _migrate_concurrency_settings(config: Dict[str, Any]):
    """旧版配置用单一的 max_concurrent_requests 限制所有厂商；迁移成
    default_concurrency + provider_concurrency（按厂商分组），仅在旧字段
    存在且新字段缺失时执行一次，并落盘避免每次启动都重复迁移。"""
    server = config.setdefault("server", {})
    old_val = server.pop("max_concurrent_requests", None)
    changed = old_val is not None
    if "default_concurrency" not in server:
        server["default_concurrency"] = old_val if old_val is not None else 16
        changed = True
    if "provider_concurrency" not in server:
        server["provider_concurrency"] = {"vertex_ai": old_val if old_val is not None else 4}
        changed = True
    if changed:
        save_config(config)


def load_config() -> Dict[str, Any]:
    if not CONFIG_FILE.exists():
        # 尝试从现有的 .env 导入初始配置
        config = json.loads(json.dumps(DEFAULT_PROVIDERS_CONFIG))
        env = load_env_vars()
        gemini_key = env.get("GEMINI_API_KEY") or os.environ.get("GEMINI_API_KEY", "")
        bedrock_token = env.get("AWS_BEARER_TOKEN_BEDROCK") or os.environ.get("AWS_BEARER_TOKEN_BEDROCK", "")
        bedrock_region = env.get("AWS_REGION") or os.environ.get("AWS_REGION", "us-west-2")

        for p in config["providers"]:
            if p["id"] == "gemini" and gemini_key:
                p["api_keys"] = [gemini_key]
                p["enabled"] = True
            elif p["id"] == "bedrock" and bedrock_token:
                p["extra"]["bearer_token"] = bedrock_token
                p["extra"]["region"] = bedrock_region
                p["enabled"] = True

        save_config(config)
        return config

    try:
        _restrict_file_permissions(CONFIG_FILE)
        with open(CONFIG_FILE, "r", encoding="utf-8") as f:
            config = json.load(f)
        _migrate_concurrency_settings(config)
        validate_config(config)
        return config
    except Exception as e:
        logger.error(f"Error loading providers_config.json: {e}")
        return json.loads(json.dumps(DEFAULT_PROVIDERS_CONFIG))


def save_config(config: Dict[str, Any]):
    validate_config(config)
    content = json.dumps(config, indent=2, ensure_ascii=False) + "\n"
    _atomic_write_text(CONFIG_FILE, content)


def prune_vertex_credential_files(config: Dict[str, Any]) -> List[str]:
    """删除已从配置移除的 Vertex 凭证文件，只处理本应用生成的固定文件名。"""
    keep = {
        f"vertex_sa_{provider['id']}.json"
        for provider in config.get("providers", [])
        if provider.get("type") == "vertex_ai" and provider.get("id")
    }
    removed = []
    for path in BASE_DIR.glob("vertex_sa_*.json"):
        if path.name in keep or not path.is_file():
            continue
        try:
            path.unlink()
            removed.append(path.name)
        except OSError as e:
            logger.warning(f"无法清理已删除 Vertex 网关的凭证文件 {path.name}: {e}")
    return removed


SKIP_GEMINI_KEYWORDS = ("tts", "lyria", "clip", "embed", "aqa", "robotics")


def fetch_gemini_models_api(api_key: str) -> List[str]:
    url = f"https://generativelanguage.googleapis.com/v1beta/models?key={api_key}&pageSize=200"
    req = urllib.request.Request(url, headers={"User-Agent": "Transit-Service/1.0"})
    with urllib.request.urlopen(req, timeout=10) as resp:
        data = json.loads(resp.read().decode("utf-8"))
    models = []
    for m in data.get("models", []):
        name = m.get("name", "")
        short = name.removeprefix("models/")
        methods = m.get("supportedGenerationMethods", [])
        if "generateContent" not in methods:
            continue
        if any(kw in short.lower() for kw in SKIP_GEMINI_KEYWORDS):
            continue
        models.append(short)
    return sorted(models)


def fetch_openai_compatible_models(base_url: str, api_key: str) -> List[str]:
    clean_base = base_url.rstrip("/")
    hostname = (urlsplit(clean_base).hostname or "").lower().rstrip(".")
    is_deepseek = hostname == "deepseek.com" or hostname.endswith(".deepseek.com")
    if not re.search(r"/(?:api/)?v\d+$", clean_base):
        if is_deepseek:
            url = f"{clean_base}/models"
        else:
            url = f"{clean_base}/v1/models"
    else:
        url = f"{clean_base}/models"

    headers = {
        "Authorization": f"Bearer {api_key}",
        "User-Agent": "Transit-Service/1.0",
        "Accept": "application/json"
    }
    req = urllib.request.Request(url, headers=headers)
    with urllib.request.urlopen(req, timeout=10) as resp:
        data = json.loads(resp.read().decode("utf-8"))
    
    models = []
    if isinstance(data, dict):
        items = data.get("data") or data.get("models") or []
        for item in items:
            if isinstance(item, dict) and "id" in item:
                models.append(item["id"])
            elif isinstance(item, str):
                models.append(item)
    elif isinstance(data, list):
        for item in data:
            if isinstance(item, dict) and "id" in item:
                models.append(item["id"])
            elif isinstance(item, str):
                models.append(item)
    return sorted(models)


def fetch_bedrock_models_api(bearer_token: str, region: str, claude_only: bool = True) -> List[str]:
    url = f"https://bedrock.{region}.amazonaws.com/inference-profiles"
    headers = {
        "Authorization": f"Bearer {bearer_token}",
        "User-Agent": "Transit-Service/1.0"
    }
    req = urllib.request.Request(url, headers=headers)
    with urllib.request.urlopen(req, timeout=10) as resp:
        data = json.loads(resp.read().decode("utf-8"))
    models = []
    for p in data.get("inferenceProfileSummaries", []):
        if p.get("status") != "ACTIVE":
            continue
        profile_id = p.get("inferenceProfileId", "")
        if not profile_id:
            continue
        if claude_only and "claude" not in profile_id.lower():
            continue
        models.append(profile_id)
    return sorted(models)


def fetch_anthropic_models(base_url: str, api_key: str) -> List[str]:
    url = f"{base_url}/v1/models?limit=1000"
    headers = {
        "x-api-key": api_key,
        "anthropic-version": "2023-06-01",
        "User-Agent": "Transit-Service/1.0",
    }
    req = urllib.request.Request(url, headers=headers)
    with urllib.request.urlopen(req, timeout=10) as resp:
        data = json.loads(resp.read().decode("utf-8"))
    models = [m["id"] for m in data.get("data", []) if isinstance(m, dict) and m.get("id")]
    return sorted(models)


def fetch_vertex_models_report(
    project: str, client_email: str, private_key: str, location: str
) -> Dict[str, Any]:
    """列出各 publisher 的候选模型，并保留每一组的成功或失败状态。"""
    from google.oauth2 import service_account
    import google.auth.transport.requests

    info = {
        "type": "service_account",
        "project_id": project,
        "client_email": client_email,
        "private_key": private_key.replace("\\n", "\n"),
        "token_uri": "https://oauth2.googleapis.com/token",
    }
    creds = service_account.Credentials.from_service_account_info(
        info, scopes=["https://www.googleapis.com/auth/cloud-platform"]
    )
    creds.refresh(google.auth.transport.requests.Request())
    token = creds.token

    host = "aiplatform.googleapis.com" if location == "global" else f"{location}-aiplatform.googleapis.com"
    models: List[str] = []
    sources = []
    for publisher in ("google", "anthropic"):
        page_token = ""
        publisher_models: List[str] = []
        error = ""
        while True:
            url = f"https://{host}/v1beta1/publishers/{publisher}/models?pageSize=200"
            if page_token:
                url += f"&pageToken={page_token}"
            req = urllib.request.Request(url, headers={"Authorization": f"Bearer {token}"})
            try:
                with urllib.request.urlopen(req, timeout=15) as resp:
                    data = json.loads(resp.read().decode("utf-8"))
            except Exception as e:
                error = f"HTTP {e.code}" if isinstance(e, urllib.error.HTTPError) else str(e)
                logger.warning(f"Vertex publisher [{publisher}] 列表拉取失败: {error}")
                break
            for m in data.get("publisherModels", []):
                name = m.get("name", "")  # publishers/google/models/gemini-2.5-pro
                short = name.split("/models/")[-1]
                if short:
                    publisher_models.append(short)
            page_token = data.get("nextPageToken", "")
            if not page_token:
                break
        publisher_models = sorted(set(publisher_models))
        models.extend(publisher_models)
        sources.append({
            "id": publisher,
            "name": "Google" if publisher == "google" else "Anthropic Claude",
            "status": "error" if error else "ok",
            "count": len(publisher_models),
            "error": error,
        })
    return {"models": sorted(set(models)), "sources": sources}


def fetch_vertex_models_api(project: str, client_email: str, private_key: str, location: str) -> List[str]:
    """兼容原调用方，只返回 Vertex publisher 候选模型列表。"""
    return fetch_vertex_models_report(project, client_email, private_key, location)["models"]


def fetch_provider_models(provider: Dict[str, Any]) -> List[str]:
    p_type = provider.get("type")
    keys = provider.get("api_keys", [])
    key = keys[0] if keys else ""

    if p_type == "gemini":
        if not key:
            raise ValueError("Gemini API Key 不能为空")
        return fetch_gemini_models_api(key)

    elif p_type in ("openai", "openai_compatible"):
        base_url = provider.get("base_url") or "https://api.openai.com/v1"
        if not key:
            raise ValueError("API Key 不能为空")
        return fetch_openai_compatible_models(base_url, key)

    elif p_type == "anthropic":
        if not key:
            raise ValueError("Anthropic API Key 不能为空")
        base_url = (provider.get("base_url") or "https://api.anthropic.com").rstrip("/")
        return fetch_anthropic_models(base_url, key)

    elif p_type == "bedrock":
        extra = provider.get("extra", {})
        token = extra.get("bearer_token") or key
        region = extra.get("region", "us-west-2")
        claude_only = extra.get("claude_only", True)
        if not token:
            raise ValueError("AWS Bearer Token 不能为空")
        return fetch_bedrock_models_api(token, region, claude_only)

    elif p_type == "vertex_ai":
        extra = provider.get("extra", {})
        project = (extra.get("project") or "").strip()
        client_email = (extra.get("client_email") or "").strip()
        private_key = (extra.get("private_key") or "").strip()
        location = extra.get("location", "us-central1") or "us-central1"
        if not (project and client_email and private_key):
            raise ValueError("请先填写 项目ID / 客户端邮箱 / 私钥 后再拉取模型")
        return fetch_vertex_models_api(project, client_email, private_key, location)

    else:
        return provider.get("models", [])


def fetch_provider_models_report(provider: Dict[str, Any]) -> Dict[str, Any]:
    """返回模型列表及可选的来源状态，供管理界面展示。"""
    if provider.get("type") != "vertex_ai":
        return {"models": fetch_provider_models(provider)}

    extra = provider.get("extra", {})
    project = (extra.get("project") or "").strip()
    client_email = (extra.get("client_email") or "").strip()
    private_key = (extra.get("private_key") or "").strip()
    location = extra.get("location", "us-central1") or "us-central1"
    if not (project and client_email and private_key):
        raise ValueError("请先填写 项目ID / 客户端邮箱 / 私钥 后再拉取模型")
    return fetch_vertex_models_report(project, client_email, private_key, location)


def generate_litellm_yaml(config: Dict[str, Any]) -> str:
    validate_config(config)
    server = config.get("server", {})
    num_retries = server.get("num_retries", 3)
    retry_after = server.get("retry_after", 1)
    allowed_fails = server.get("allowed_fails", 3)
    request_timeout = server.get("request_timeout", 600)

    # 统一计算实际启用模型与裸名归属，模型路由和 fallback 共用同一语义。
    enabled_models = []  # (provider, model)
    bare_owners = bare_model_providers(config)
    for p in config.get("providers", []):
        if not p.get("enabled"):
            continue
        for m in selected_models_for_provider(p):
            enabled_models.append((p, m))

    bedrock_items = [
        (p.get("id"), m) for p, m in enabled_models if p.get("type") == "bedrock"
    ]

    # 每个 bedrock 模型兜底到 *其他* bedrock 模型（不能指回自己，否则 503 时无处可逃）。
    # 优先把同名的 global.anthropic 区域变体排在最前，再接其余模型。
    bedrock_pubs = [f"{pid}/{m}" for pid, m in bedrock_items]
    fallback_pairs = []
    for p, m in enabled_models:
        pid = p.get("id")
        pub = f"{pid}/{m}"
        aliases = [pub]
        if bare_owners.get(m) == pid:
            aliases.append(m)

        if p.get("type") == "bedrock":
            alt_first = []
            if m.startswith("us.anthropic."):
                alt = f"{pid}/" + m.replace("us.anthropic.", "global.anthropic.", 1)
                if alt in bedrock_pubs:
                    alt_first.append(alt)
            others = alt_first + [x for x in bedrock_pubs if x != pub and x not in alt_first]
            for alias in aliases:
                if others:
                    fallback_pairs.append({alias: others})
        else:
            # 自兜底表示网络中断时重试同一个模型组；裸名也必须有同样条目。
            for alias in aliases:
                fallback_pairs.append({alias: [alias]})

    self_fallbacks = json.dumps(fallback_pairs) if fallback_pairs else "[]"

    # 预处理 Vertex AI：用 project_id/client_email/private_key 三项重建服务账号凭证文件
    vertex_creds = {}  # provider_id -> {"path": str, "project": str}
    for p in config.get("providers", []):
        if not p.get("enabled") or p.get("type") != "vertex_ai":
            continue
        extra = p.get("extra", {})
        project = (extra.get("project") or "").strip()
        client_email = (extra.get("client_email") or "").strip()
        private_key = (extra.get("private_key") or "").strip()
        if not (project and client_email and private_key):
            continue
        # 从 JSON 复制粘贴过来的私钥常带字面 \n，需还原成真实换行
        private_key = private_key.replace("\\n", "\n")
        sa = {
            "type": "service_account",
            "project_id": project,
            "client_email": client_email,
            "private_key": private_key,
            "token_uri": "https://oauth2.googleapis.com/token",
        }
        sa_path = BASE_DIR / f"vertex_sa_{p['id']}.json"
        _atomic_write_text(sa_path, json.dumps(sa))
        vertex_creds[p["id"]] = {"path": str(sa_path), "project": project}

    lines = [
        "router_settings:",
        f"  num_retries: {num_retries}",
        f"  retry_after: {retry_after}",
        f"  allowed_fails: {allowed_fails}",
        f"  fallbacks: {self_fallbacks}",
        "",
        "litellm_settings:",
        "  drop_params: true",
        f"  request_timeout: {request_timeout}",
        "  assistant_continue_message: '...'",
        "",
        "model_list:" if enabled_models else "model_list: []",
    ]

    for p in config.get("providers", []):
        if not p.get("enabled"):
            continue

        pid = p.get("id")
        p_type = p.get("type")
        keys = p.get("api_keys", [])
        models = selected_models_for_provider(p)
        base_url = p.get("base_url", "").rstrip("/")
        extra = p.get("extra", {})

        # 如果没有配置 key 但有 extra token
        if not keys and extra.get("bearer_token"):
            keys = [extra.get("bearer_token")]

        if not keys:
            keys = ["dummy_key"]

        for m in models:
            # cc-switch 等客户端往往只发裸模型名（不带厂商前缀），LiteLLM 的
            # model_name 若只注册 "厂商id/模型名" 会导致这类请求 400。
            bare_available = bare_owners.get(m) == pid

            for key in keys:
                param_lines = []

                if p_type == "gemini":
                    param_lines.append(f"      model: gemini/{m}")
                    param_lines.append(f"      api_key: '{key}'")

                elif p_type == "openai":
                    param_lines.append(f"      model: openai/{m}")
                    param_lines.append(f"      api_key: '{key}'")
                    if base_url:
                        param_lines.append(f"      api_base: '{base_url}'")

                elif p_type == "openai_compatible":
                    param_lines.append(f"      model: openai/{m}")
                    param_lines.append(f"      api_key: '{key}'")
                    if base_url:
                        clean_api_base = normalize_openai_api_base(base_url)
                        param_lines.append(f"      api_base: '{clean_api_base}'")

                elif p_type == "anthropic":
                    param_lines.append(f"      model: anthropic/{m}")
                    param_lines.append(f"      api_key: '{key}'")
                    if base_url:
                        param_lines.append(f"      api_base: '{base_url}'")

                elif p_type == "bedrock":
                    region = extra.get("region", "us-west-2")
                    param_lines.append(f"      model: bedrock/{m}")
                    param_lines.append(f"      aws_region_name: '{region}'")

                elif p_type == "vertex_ai":
                    location = extra.get("location", "us-central1")
                    creds = vertex_creds.get(p.get("id"))
                    project = (creds or {}).get("project") or extra.get("project", "")
                    param_lines.append(f"      model: vertex_ai/{m}")
                    if creds:
                        param_lines.append(f"      vertex_credentials: '{creds['path']}'")
                    if project:
                        param_lines.append(f"      vertex_project: '{project}'")
                    if location:
                        param_lines.append(f"      vertex_location: '{location}'")

                model_names = [f"{pid}/{m}"]
                if bare_available:
                    model_names.append(m)

                for name in model_names:
                    lines.append(f"  - model_name: {name}")
                    lines.append("    litellm_params:")
                    lines.extend(param_lines)
                    lines.append("")

    yaml_content = "\n".join(lines)
    _atomic_write_text(LITELLM_YAML_PATH, yaml_content)

    return yaml_content
