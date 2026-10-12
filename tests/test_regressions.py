import asyncio
import json
import stat
import tempfile
import time
import unittest
import urllib.error
from pathlib import Path
from unittest import mock

from fastapi import HTTPException
from starlette.requests import Request

import provider_manager
import proxy


def provider_config(*, selected_models, base_url="https://example.invalid"):
    return {
        "server": {
            "request_timeout": 600,
            "default_concurrency": 16,
            "provider_concurrency": {"demo": 4},
        },
        "providers": [{
            "id": "demo",
            "name": "Demo",
            "type": "openai_compatible",
            "enabled": True,
            "api_keys": ["fake-key"],
            "base_url": base_url,
            "models": ["model-a", "model-b"],
            "selected_models": selected_models,
            "extra": {},
        }],
    }


def json_request(payload, *, origin="http://127.0.0.1:4000"):
    body = json.dumps(payload).encode()
    sent = False

    async def receive():
        nonlocal sent
        if not sent:
            sent = True
            return {"type": "http.request", "body": body, "more_body": False}
        return {"type": "http.disconnect"}

    return Request({
        "type": "http",
        "method": "POST",
        "path": "/api/config",
        "headers": [
            (b"host", b"127.0.0.1:4000"),
            (b"origin", origin.encode()),
            (b"content-type", b"application/json"),
        ],
        "client": ("127.0.0.1", 1234),
        "server": ("127.0.0.1", 4000),
        "scheme": "http",
    }, receive)


class ProviderManagerRegressionTests(unittest.TestCase):
    def render(self, config):
        with tempfile.TemporaryDirectory() as tmp_dir:
            old_path = provider_manager.LITELLM_YAML_PATH
            provider_manager.LITELLM_YAML_PATH = Path(tmp_dir) / "litellm.yaml"
            try:
                return provider_manager.generate_litellm_yaml(config)
            finally:
                provider_manager.LITELLM_YAML_PATH = old_path

    def test_empty_selection_stays_empty(self):
        rendered = self.render(provider_config(selected_models=[]))
        self.assertNotIn("model-a", rendered)
        self.assertNotIn("model-b", rendered)
        self.assertIn("model_list: []", rendered)

    def test_bare_alias_gets_fallback(self):
        rendered = self.render(provider_config(selected_models=["model-a"]))
        self.assertIn('"model-a": ["model-a"]', rendered)
        self.assertIn("model_name: model-a", rendered)

    def test_duplicate_model_has_no_implicit_bare_route(self):
        config = provider_config(selected_models=["model-a"])
        config["providers"].append({
            **config["providers"][0],
            "id": "demo-2",
            "name": "Demo 2",
            "base_url": "https://second.invalid",
        })
        rendered = self.render(config)
        self.assertIn("model_name: demo/model-a", rendered)
        self.assertIn("model_name: demo-2/model-a", rendered)
        self.assertNotIn("\n  - model_name: model-a\n", rendered)

    def test_duplicate_model_uses_explicit_bare_route(self):
        config = provider_config(selected_models=["model-a"])
        config["providers"].append({
            **config["providers"][0],
            "id": "demo-2",
            "name": "Demo 2",
            "base_url": "https://second.invalid",
        })
        config["server"]["bare_model_routes"] = {"model-a": "demo-2"}
        rendered = self.render(config)
        self.assertEqual(rendered.count("\n  - model_name: model-a\n"), 1)
        bare_block = rendered.split("\n  - model_name: model-a\n", 1)[1]
        self.assertIn("api_base: 'https://second.invalid/v1'", bare_block)

    def test_versioned_api_base_is_not_modified(self):
        rendered = self.render(provider_config(
            selected_models=["model-a"],
            base_url="https://example.invalid/api/v3",
        ))
        self.assertIn("api_base: 'https://example.invalid/api/v3'", rendered)
        self.assertNotIn("/api/v3/v1", rendered)

    def test_deepseek_url_detection_uses_hostname_boundary(self):
        response = mock.MagicMock()
        response.__enter__.return_value.read.return_value = b'{"data": []}'
        with mock.patch("provider_manager.urllib.request.urlopen", return_value=response) as opened:
            provider_manager.fetch_openai_compatible_models(
                "https://deepseek.com.evil.example", "fake-key"
            )
        self.assertEqual(
            opened.call_args.args[0].full_url,
            "https://deepseek.com.evil.example/v1/models",
        )

        with mock.patch("provider_manager.urllib.request.urlopen", return_value=response) as opened:
            provider_manager.fetch_openai_compatible_models(
                "https://api.deepseek.com", "fake-key"
            )
        self.assertEqual(opened.call_args.args[0].full_url, "https://api.deepseek.com/models")

    def test_vertex_model_report_keeps_partial_results_and_source_error(self):
        credentials = mock.Mock(token="access-token")
        google_response = mock.MagicMock()
        google_response.__enter__.return_value.read.return_value = json.dumps({
            "publisherModels": [{"name": "publishers/google/models/gemini-test"}],
        }).encode()
        forbidden = urllib.error.HTTPError(
            "https://example.invalid", 403, "Forbidden", {}, None
        )

        with (
            mock.patch(
                "google.oauth2.service_account.Credentials.from_service_account_info",
                return_value=credentials,
            ),
            mock.patch(
                "provider_manager.urllib.request.urlopen",
                side_effect=[google_response, forbidden],
            ),
        ):
            report = provider_manager.fetch_vertex_models_report(
                "demo-project", "demo@example.invalid", "private-key", "global"
            )

        self.assertEqual(report["models"], ["gemini-test"])
        self.assertEqual(report["sources"][0]["status"], "ok")
        self.assertEqual(report["sources"][1]["status"], "error")
        self.assertEqual(report["sources"][1]["error"], "HTTP 403")

    def test_config_write_is_private_and_atomic(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            old_path = provider_manager.CONFIG_FILE
            target = Path(tmp_dir) / "providers.json"
            provider_manager.CONFIG_FILE = target
            try:
                provider_manager.save_config(provider_config(selected_models=[]))
                self.assertEqual(stat.S_IMODE(target.stat().st_mode), 0o600)
                self.assertEqual(json.loads(target.read_text()), provider_config(selected_models=[]))
            finally:
                provider_manager.CONFIG_FILE = old_path

    def test_prune_vertex_credentials_only_removes_deleted_instances(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            old_base = provider_manager.BASE_DIR
            provider_manager.BASE_DIR = Path(tmp_dir)
            try:
                keep = Path(tmp_dir) / "vertex_sa_keep.json"
                remove = Path(tmp_dir) / "vertex_sa_removed.json"
                unrelated = Path(tmp_dir) / "other.json"
                keep.write_text("keep")
                remove.write_text("remove")
                unrelated.write_text("unrelated")
                config = provider_config(selected_models=[])
                config["providers"].append({
                    "id": "keep", "name": "Keep", "type": "vertex_ai", "enabled": False,
                    "api_keys": [], "base_url": "", "models": [], "selected_models": [], "extra": {},
                })

                removed = provider_manager.prune_vertex_credential_files(config)

                self.assertEqual(removed, ["vertex_sa_removed.json"])
                self.assertTrue(keep.exists())
                self.assertFalse(remove.exists())
                self.assertTrue(unrelated.exists())
            finally:
                provider_manager.BASE_DIR = old_base


class ProxyRegressionTests(unittest.TestCase):
    @classmethod
    def tearDownClass(cls):
        async def close_clients():
            await proxy._http_client.aclose()
            await proxy._stream_http_client.aclose()

        asyncio.run(close_clients())

    @staticmethod
    def clean(payload):
        body, _, _ = proxy._clean_body(json.dumps(payload).encode())
        return json.loads(body)

    def test_assistant_tail_is_only_trimmed_for_gemini(self):
        messages = [
            {"role": "user", "content": "hello"},
            {"role": "assistant", "content": "prefix"},
        ]
        openai = self.clean({"model": "openai/gpt-test", "messages": messages})
        gemini = self.clean({"model": "gemini/gemini-test", "messages": messages})
        self.assertEqual([m["role"] for m in openai["messages"]], ["user", "assistant"])
        self.assertEqual([m["role"] for m in gemini["messages"]], ["user"])

    def test_trailing_reasoning_items_are_trimmed_only_for_gemini(self):
        def history():
            return [
                {"type": "message", "role": "user", "content": [{"type": "input_text", "text": "hi"}]},
                {"type": "function_call", "id": "fc_1", "call_id": "call_1", "name": "x", "arguments": "{}"},
                {"type": "function_call_output", "call_id": "call_1", "output": "ok"},
                {"type": "reasoning", "id": "rs_1", "summary": [{"type": "summary_text", "text": "think"}]},
                {"type": "reasoning", "id": "rs_2", "summary": []},
            ]
        gemini = self.clean({"model": "vertex_ai/gemini-3.8-flash", "input": history()})
        self.assertEqual([i["type"] for i in gemini["input"]], ["message", "function_call", "function_call_output"])
        other = self.clean({"model": "aicodewith/gpt-test", "input": history()})
        self.assertEqual(len(other["input"]), 5)

    def test_reasoning_in_the_middle_and_user_tail_are_kept_for_gemini(self):
        items = [
            {"type": "message", "role": "user", "content": [{"type": "input_text", "text": "a"}]},
            {"type": "reasoning", "id": "rs_1", "summary": []},
            {"type": "message", "role": "assistant", "content": [{"type": "output_text", "text": "b"}]},
            {"type": "message", "role": "user", "content": [{"type": "input_text", "text": "c"}]},
        ]
        out = self.clean({"model": "vertex_ai/gemini-3.8-flash", "input": items})
        self.assertEqual([i["type"] for i in out["input"]], ["message", "reasoning", "message", "message"])

    def test_string_input_and_all_reasoning_input_do_not_crash(self):
        self.assertEqual(self.clean({"model": "vertex_ai/gemini-3.8-flash", "input": "hi"})["input"], "hi")
        out = self.clean({"model": "vertex_ai/gemini-3.8-flash", "input": [{"type": "reasoning", "id": "rs_1", "summary": []}]})
        self.assertEqual(out["input"], [])

    def test_bare_model_uses_provider_limit(self):
        old_owners = proxy._BARE_MODEL_PROVIDERS
        old_default = proxy._DEFAULT_CONCURRENCY
        old_overrides = proxy._PROVIDER_CONCURRENCY
        try:
            proxy._BARE_MODEL_PROVIDERS = {"model-a": "demo"}
            proxy._DEFAULT_CONCURRENCY = 16
            proxy._PROVIDER_CONCURRENCY = {"demo": 4}
            proxy._provider_semaphores.clear()
            self.assertEqual(proxy._semaphore_for_model("model-a")._value, 4)
        finally:
            proxy._BARE_MODEL_PROVIDERS = old_owners
            proxy._DEFAULT_CONCURRENCY = old_default
            proxy._PROVIDER_CONCURRENCY = old_overrides
            proxy._provider_semaphores.clear()

    def test_request_timeout_hot_reload(self):
        config = provider_config(selected_models=["model-a"])
        config["server"]["request_timeout"] = 7
        proxy.reload_runtime_settings(config)
        self.assertEqual(proxy._http_client.timeout.read, 7)
        proxy.reload_runtime_settings(proxy._initial_config)

    def test_client_requires_nonempty_bearer(self):
        request = Request({
            "type": "http",
            "headers": [],
            "client": ("127.0.0.1", 1234),
            "server": ("127.0.0.1", 4000),
            "scheme": "http",
        })
        with self.assertRaises(HTTPException) as raised:
            proxy._require_client_authorization(request)
        self.assertEqual(raised.exception.status_code, 401)

    def test_admin_rejects_cross_origin(self):
        request = Request({
            "type": "http",
            "headers": [
                (b"host", b"127.0.0.1:4000"),
                (b"origin", b"https://evil.example"),
            ],
            "client": ("127.0.0.1", 1234),
            "server": ("127.0.0.1", 4000),
            "scheme": "http",
        })
        with self.assertRaises(HTTPException) as raised:
            proxy._require_admin_request(request)
        self.assertEqual(raised.exception.status_code, 403)

    def test_failed_request_filename_does_not_use_model_name(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            old_dir = proxy.FAILED_REQ_DIR
            proxy.FAILED_REQ_DIR = Path(tmp_dir)
            try:
                proxy._save_failed_request(b"{}", "../../secret-model", 400)
                saved = list(Path(tmp_dir).glob("*.json"))
                self.assertEqual(len(saved), 1)
                self.assertNotIn("secret-model", saved[0].name)
                self.assertEqual(saved[0].parent, Path(tmp_dir))
            finally:
                proxy.FAILED_REQ_DIR = old_dir

    def test_public_model_errors_do_not_expose_exception_details(self):
        request = Request({
            "type": "http",
            "method": "GET",
            "path": "/v1/models",
            "headers": [(b"authorization", b"Bearer test")],
            "client": ("127.0.0.1", 1234),
            "server": ("127.0.0.1", 4000),
            "scheme": "http",
        })
        with mock.patch("proxy.httpx.AsyncClient", side_effect=RuntimeError("sensitive detail")):
            response = asyncio.run(proxy.list_models(request))
        payload = json.loads(response.body)
        self.assertEqual(response.status_code, 502)
        self.assertEqual(payload["error"]["message"], "内部模型服务暂时不可用")
        self.assertNotIn("sensitive detail", response.body.decode())

    def test_public_proxy_errors_do_not_expose_exception_details(self):
        body = json.dumps({
            "model": "demo/model-a",
            "messages": [{"role": "user", "content": "hello"}],
            "stream": False,
        }).encode()
        sent = False

        async def receive():
            nonlocal sent
            if not sent:
                sent = True
                return {"type": "http.request", "body": body, "more_body": False}
            return {"type": "http.disconnect"}

        request = Request({
            "type": "http",
            "method": "POST",
            "path": "/v1/chat/completions",
            "query_string": b"",
            "headers": [(b"authorization", b"Bearer test")],
            "client": ("127.0.0.1", 1234),
            "server": ("127.0.0.1", 4000),
            "scheme": "http",
        }, receive)
        upstream_error = mock.AsyncMock(side_effect=RuntimeError("sensitive detail"))
        with mock.patch.object(proxy._http_client, "request", new=upstream_error):
            response = asyncio.run(proxy.proxy(request, "v1/chat/completions"))
        payload = json.loads(response.body)
        self.assertEqual(response.status_code, 502)
        self.assertEqual(payload["error"]["message"], "内部模型服务暂时不可用")
        self.assertNotIn("sensitive detail", response.body.decode())


SIGNED_ID = "call_363124_" + "A1/b+C2d" * 12 + "__thought__" + "Zm9v/YmFy+" * 60   # 约 750 字符，含 / 和 +
GEMINI = "vertex_ai/gemini-3.8-flash"


class SignedIdRegressionTests(unittest.TestCase):
    """Gemini 思维签名拼在工具调用 id 里（见 proxy.py 注释）：出站换短 id，入站按需回填。"""

    def setUp(self):
        proxy._SIGNED_ID_CACHE.clear()

    @staticmethod
    def function_call(call_id):
        return {"type": "function_call", "id": call_id, "call_id": call_id, "name": "exec_command", "arguments": "{}"}

    def sse(self, event, payload):
        return f"event: {event}\ndata: {json.dumps(payload)}\n\n".encode()

    def stream_events(self, call_id):
        item = self.function_call(call_id)
        return [
            self.sse("response.created", {"type": "response.created", "response": {"output": []}}),
            self.sse("response.output_item.added", {"type": "response.output_item.added", "item": item}),
            self.sse("response.function_call_arguments.delta",
                     {"type": "response.function_call_arguments.delta", "item_id": call_id, "delta": "{}"}),
            self.sse("response.output_item.done", {"type": "response.output_item.done", "item": item}),
            self.sse("response.completed",
                     {"type": "response.completed", "response": {"output": [item]}}),
        ]

    def test_non_stream_response_gets_short_valid_ids(self):
        raw = json.dumps({"output": [self.function_call(SIGNED_ID)]}).encode()
        fixed = json.loads(proxy._rewrite_signed_ids_json(raw, GEMINI))
        call = fixed["output"][0]
        self.assertLessEqual(len(call["call_id"]), 64)
        self.assertRegex(call["call_id"], r"^[A-Za-z0-9_-]+$")
        self.assertRegex(call["id"], r"^[A-Za-z0-9_-]+$")
        self.assertLessEqual(len(call["id"]), 64)
        self.assertTrue(call["call_id"].startswith("call_363124_"))
        self.assertTrue(call["id"].startswith("fc_363124_"), "function_call 项的 id 必须以 fc_ 开头")

    def test_unsigned_function_call_gets_fc_item_id_and_keeps_call_id(self):
        raw = json.dumps({"output": [self.function_call("call_abc123")]}).encode()
        call = json.loads(proxy._rewrite_signed_ids_json(raw, GEMINI))["output"][0]
        self.assertEqual(call["call_id"], "call_abc123")
        self.assertEqual(call["id"], "fc_abc123")

    def test_response_without_any_call_id_is_returned_untouched(self):
        raw = json.dumps({"output": [{"type": "message", "id": "msg_1", "content": []}]}).encode()
        self.assertIs(proxy._rewrite_signed_ids_json(raw, GEMINI), raw)

    def test_stream_rewrites_all_positions_consistently_for_every_chunk_size(self):
        data = b"".join(self.stream_events(SIGNED_ID))
        for size in (1, 7, 64, 513, len(data)):
            rewriter = proxy._SignedIdStreamRewriter(GEMINI)
            out = b""
            for i in range(0, len(data), size):
                out += rewriter.feed(data[i:i + size])
            out += rewriter.flush()
            self.assertNotIn(b"__thought__", out, f"chunk={size}")
            item_ids, call_ids = set(), set()
            for line in out.decode().splitlines():
                if not line.startswith("data:"):
                    continue
                obj = json.loads(line[5:])
                for node in (obj, obj.get("item") or {}, *((obj.get("response") or {}).get("output") or [])):
                    for key in ("id", "item_id"):
                        if isinstance(node.get(key), str) and node[key].startswith("fc_"):
                            item_ids.add(node[key])
                    if isinstance(node.get("call_id"), str):
                        call_ids.add(node["call_id"])
            self.assertEqual(len(item_ids), 1, f"id/item_id 必须改写成同一个 fc_ 值, chunk={size}: {item_ids}")
            self.assertEqual(len(call_ids), 1, f"call_id 必须改写成同一个值, chunk={size}: {call_ids}")
            item_id, call_id = next(iter(item_ids)), next(iter(call_ids))
            self.assertTrue(call_id.startswith("call_"))
            self.assertLessEqual(max(len(item_id), len(call_id)), 64)
            for value in (item_id, call_id):
                self.assertRegex(value, r"^[A-Za-z0-9_-]+$")
                self.assertEqual(proxy._restore_signed_id(value, GEMINI), SIGNED_ID)

    def test_signed_item_id_and_call_id_share_one_full_id(self):
        raw = json.dumps({"output": [self.function_call(SIGNED_ID)]}).encode()
        call = json.loads(proxy._rewrite_signed_ids_json(raw, GEMINI))["output"][0]
        self.assertTrue(call["id"].startswith("fc_"))
        self.assertTrue(call["call_id"].startswith("call_"))
        data = {"model": GEMINI, "input": [call, {"type": "function_call_output", "call_id": call["call_id"], "output": "ok"}]}
        proxy._normalize_responses_ids(data)
        self.assertEqual(data["input"][0]["id"], SIGNED_ID)
        self.assertEqual(data["input"][0]["call_id"], SIGNED_ID)
        self.assertEqual(data["input"][1]["call_id"], SIGNED_ID)

    def test_stream_without_call_ids_is_forwarded_byte_for_byte(self):
        data = self.sse("response.output_text.delta", {"type": "response.output_text.delta", "delta": "hi"}) * 3
        rewriter = proxy._SignedIdStreamRewriter(GEMINI)
        out = rewriter.feed(data[:50]) + rewriter.feed(data[50:]) + rewriter.flush()
        self.assertEqual(out, data)

    def test_unsigned_stream_only_changes_item_ids(self):
        data = b"".join(self.stream_events("call_plain"))
        rewriter = proxy._SignedIdStreamRewriter(GEMINI)
        out = (rewriter.feed(data[:50]) + rewriter.feed(data[50:]) + rewriter.flush()).decode()
        seen = {"id": set(), "item_id": set(), "call_id": set()}
        for line in out.splitlines():
            if not line.startswith("data:"):
                continue
            obj = json.loads(line[5:])
            for node in (obj, obj.get("item") or {}, *((obj.get("response") or {}).get("output") or [])):
                for key in seen:
                    if isinstance(node.get(key), str):
                        seen[key].add(node[key])
        self.assertEqual(seen["call_id"], {"call_plain"})
        self.assertEqual(seen["id"], {"fc_plain"})
        self.assertEqual(seen["item_id"], {"fc_plain"})

    def test_short_id_is_restored_for_the_same_gemini_model(self):
        raw = json.dumps({"output": [self.function_call(SIGNED_ID)]}).encode()
        short = json.loads(proxy._rewrite_signed_ids_json(raw, GEMINI))["output"][0]["call_id"]
        data = {"model": GEMINI, "input": [self.function_call(short),
                {"type": "function_call_output", "call_id": short, "output": "ok"}]}
        proxy._normalize_responses_ids(data)
        self.assertEqual(data["input"][0]["call_id"], SIGNED_ID)
        self.assertEqual(data["input"][1]["call_id"], SIGNED_ID)

    def test_short_id_is_not_restored_for_another_model(self):
        raw = json.dumps({"output": [self.function_call(SIGNED_ID)]}).encode()
        short = json.loads(proxy._rewrite_signed_ids_json(raw, GEMINI))["output"][0]["call_id"]
        data = {"model": "aicodewith/gpt-test", "input": [self.function_call(short)]}
        proxy._normalize_responses_ids(data)
        self.assertEqual(data["input"][0]["call_id"], short)
        self.assertNotIn("__thought__", data["input"][0]["call_id"])

    def test_unknown_short_id_after_restart_falls_back_to_unsigned(self):
        data = {"model": GEMINI, "input": [self.function_call("call_1_abcdef0123")]}
        proxy._normalize_responses_ids(data)
        self.assertEqual(data["input"][0]["call_id"], "call_1_abcdef0123")

    def test_cache_is_bounded(self):
        old = proxy._SIGNED_ID_CACHE_MAX
        proxy._SIGNED_ID_CACHE_MAX = 5
        try:
            for i in range(20):
                proxy._shorten_signed_id(f"call_{i}__thought__sig{i}", GEMINI)
            self.assertEqual(len(proxy._SIGNED_ID_CACHE), 5)
        finally:
            proxy._SIGNED_ID_CACHE_MAX = old

    def test_only_responses_path_of_gemini_models_is_rewritten(self):
        self.assertTrue(proxy._is_responses_path("v1/responses"))
        self.assertFalse(proxy._is_responses_path("v1/messages"))
        self.assertFalse(proxy._is_responses_path("v1/chat/completions"))
        self.assertTrue(proxy._is_gemini_model(GEMINI))
        self.assertFalse(proxy._is_gemini_model("aicodewith/claude-haiku-4-5"))


class UpdateConfigRegressionTests(unittest.IsolatedAsyncioTestCase):
    async def test_restart_does_not_block_loop_and_save_happens_after_ready(self):
        old_config = provider_config(selected_models=["model-a"])
        new_config = provider_config(selected_models=["model-b"])
        events = []

        def restart(_config):
            events.append("restart-start")
            time.sleep(0.1)
            events.append("restart-ready")
            return True

        def save(_config):
            events.append("save")

        def prune(_config):
            events.append("prune")

        with (
            mock.patch.object(proxy.provider_manager, "load_config", return_value=old_config),
            mock.patch.object(proxy.provider_manager, "generate_litellm_yaml", return_value=""),
            mock.patch.object(proxy.provider_manager, "save_config", side_effect=save),
            mock.patch.object(proxy.provider_manager, "prune_vertex_credential_files", side_effect=prune),
            mock.patch.object(proxy, "restart_litellm_subproc", side_effect=restart),
        ):
            task = asyncio.create_task(proxy.update_config(json_request(new_config)))
            await asyncio.sleep(0.02)
            self.assertFalse(task.done(), "同步重启阻塞了事件循环")
            response = await task

        self.assertEqual(response.status_code, 200)
        self.assertEqual(events, ["restart-start", "restart-ready", "save", "prune"])
        proxy.reload_runtime_settings(proxy._initial_config)


class WebUiRegressionTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.html = (Path(__file__).parents[1] / "static" / "index.html").read_text()

    def test_custom_provider_uses_complete_modal_form(self):
        self.assertNotIn("prompt('厂商名称", self.html)
        self.assertIn('id="providerCatalogTitle"', self.html)
        self.assertIn('id="customProviderForm"', self.html)
        for field_id in (
            "customProviderName",
            "customProviderId",
            "customProviderBaseUrl",
            "customProviderKeys",
        ):
            self.assertIn(f'id="{field_id}"', self.html)

    def test_provider_catalog_is_grouped_by_user_task(self):
        for category in ("云平台", "官方模型 API", "自定义接入"):
            self.assertIn(f"<strong>{category}</strong>", self.html)
        for provider in ("Google Vertex AI", "AWS Bedrock", "Google Gemini", "OpenAI", "Anthropic Claude", "DeepSeek"):
            self.assertIn(f"name: '{provider}'", self.html)

    def test_sidebar_does_not_group_unrelated_brands_by_adapter_type(self):
        self.assertIn("function providerFamilyKey(provider)", self.html)
        self.assertIn("return `custom:${provider.id}`", self.html)
        self.assertNotIn("if (!grouped.has(p.type))", self.html)
        self.assertIn("<span class=\"ni-badge\">自定义</span>", self.html)

    def test_provider_state_and_kind_are_human_readable(self):
        self.assertIn("function providerKindLabel(provider)", self.html)
        self.assertIn("自定义接口", self.html)
        self.assertIn("${p.enabled ? '已启用' : '未启用'}", self.html)

    def test_provider_delete_requires_confirmation_and_cleans_references(self):
        self.assertIn("function confirmDeleteProvider(idx)", self.html)
        self.assertIn('role="alertdialog"', self.html)
        self.assertIn("function deleteProvider(idx)", self.html)
        self.assertIn("delete server.provider_concurrency[providerId]", self.html)
        self.assertIn("if (owner === providerId) delete server.bare_model_routes[model]", self.html)
        self.assertIn("点击「保存并应用」后生效", self.html)

    def test_api_panel_lists_and_tests_all_supported_public_endpoints(self):
        self.assertIn('<span class="ni-name">API 接口</span>', self.html)
        for path in ("/api/health", "/v1/models", "/v1/chat/completions", "/v1/responses"):
            self.assertIn(f"path: '{path}'", self.html)
        self.assertIn("function selectApiEndpoint(key)", self.html)
        self.assertIn("async function runApiTest()", self.html)
        self.assertIn("{ model, input: prompt, stream: false }", self.html)
        self.assertNotIn("网关连通性测试", self.html)

    def test_each_api_endpoint_has_full_url_copy_action(self):
        self.assertIn("async function copyApiEndpointUrl(key, button)", self.html)
        self.assertIn("`${window.location.origin}${endpoint.path}`", self.html)
        self.assertIn("copyApiEndpointUrl('${key}', this)", self.html)
        self.assertIn('aria-label="复制 ${endpoint.method} ${endpoint.path} 完整 URL"', self.html)
        self.assertNotIn('aria-label="复制 API Base URL"', self.html)
        self.assertIn("navigator.clipboard?.writeText", self.html)
        self.assertIn("document.execCommand('copy')", self.html)
        self.assertIn("<span>复制</span>", self.html)

    def test_custom_provider_modal_has_accessible_validation(self):
        self.assertIn('aria-labelledby="customProviderTitle"', self.html)
        self.assertIn('onkeydown="handleModalKeydown(event)"', self.html)
        self.assertIn('aria-live="polite"', self.html)

    def test_vertex_service_account_json_import_is_local_and_complete(self):
        self.assertIn('accept=".json,application/json"', self.html)
        self.assertIn("function parseVertexServiceAccountJson(text)", self.html)
        self.assertIn("function importVertexServiceAccount(idx, input)", self.html)
        for source_field, target_field in (
            ("project_id", "project"),
            ("client_email", "client_email"),
            ("private_key", "private_key"),
        ):
            self.assertIn(f"{target_field}: data.{source_field}.trim()", self.html)
        self.assertIn("文件只在浏览器中解析", self.html)

    def test_vertex_project_can_be_created_as_an_independent_instance(self):
        self.assertIn('id="vertexProviderForm"', self.html)
        self.assertIn("type: 'vertex_ai'", self.html)
        self.assertIn("managed_instance: true", self.html)
        self.assertIn("一个 JSON 对应一个独立网关实例", self.html)

    def test_model_source_status_and_duplicate_route_controls_are_visible(self):
        self.assertIn("p.model_sources = data.sources || []", self.html)
        self.assertIn("function updateBareModelRoute(model, providerId)", self.html)
        self.assertIn("重名模型需要指定裸名路由", self.html)
        self.assertIn("不提供裸模型名", self.html)


if __name__ == "__main__":
    unittest.main()
