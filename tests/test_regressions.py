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
