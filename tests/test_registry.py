"""检测引擎测试：引用解析 + 双模式分流 + 版本比较 + Registry 协议（Bearer/304/tags）。

协议测试用本地 ThreadingHTTPServer 起 mock registry，真实走 httpx HTTP 栈。
"""
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from backend.registry import (
    RegistryClient,
    detect_mode,
    newer_version_tags,
    parse_image_spec,
)
from backend.detect import StaticRegistrySource


# ---------- 纯逻辑 ----------

class TestParseImageSpec:
    def test_docker_hub_short_name(self):
        assert parse_image_spec("nginx") == ("registry-1.docker.io", "library/nginx", "latest")

    def test_docker_hub_with_tag(self):
        assert parse_image_spec("nginx:1.25") == ("registry-1.docker.io", "library/nginx", "1.25")

    def test_docker_io_alias(self):
        reg, repo, tag = parse_image_spec("docker.io/redis:7")
        assert reg == "registry-1.docker.io" and repo == "library/redis" and tag == "7"

    def test_custom_registry_with_port(self):
        assert parse_image_spec("registry.local:5000/app/web:v2") == ("registry.local:5000", "app/web", "v2")

    def test_ghcr(self):
        assert parse_image_spec("ghcr.io/quenary/tugtainer:1") == ("ghcr.io", "quenary/tugtainer", "1")

    def test_latest_default(self):
        reg, repo, tag = parse_image_spec("ghcr.io/a/b")
        assert tag == "latest"

    def test_digest_ref_rejected(self):
        with pytest.raises(ValueError):
            parse_image_spec("nginx@sha256:abc")

    def test_empty_rejected(self):
        with pytest.raises(ValueError):
            parse_image_spec("")


class TestDetectMode:
    def test_floating_tags_are_digest_only(self):
        for tag in ("latest", "nightly", "dev", "canary", "edge", "main"):
            assert detect_mode(tag) == "digest-only"

    def test_version_tags_are_pin_watch(self):
        for tag in ("8.4.5", "v3.9", "1.2.3-alpine", "2.0", "16.2"):
            assert detect_mode(tag) == "pin-watch"

    def test_override_wins(self):
        assert detect_mode("latest", "pin-watch") == "pin-watch"
        assert detect_mode("8.4.5", "digest-only") == "digest-only"

    def test_newer_version_tags(self):
        tags = ["8.4.5", "8.4.6", "9.0", "26", "latest", "8.4.4"]
        assert newer_version_tags("8.4.5", tags) == ["8.4.6", "9.0", "26"]

    def test_newer_version_tags_empty_when_current_is_max(self):
        assert newer_version_tags("9.0", ["8.0", "9.0", "latest"]) == []


# ---------- Static 源（demo/引擎联动用） ----------

class TestStaticRegistrySource:
    def test_check_returns_digest_and_newer_tags(self):
        src = StaticRegistrySource({"myapp:2.0": {"digest": "sha256:aa", "tags": ["2.0", "2.1", "3.0"]}})
        out = src.check("myapp:2.0", None, "pin-watch")
        assert out.digest == "sha256:aa"
        assert out.newer_tags == ["2.1", "3.0"]

    def test_unknown_spec_raises(self):
        src = StaticRegistrySource({})
        with pytest.raises(RuntimeError):
            src.check("nope:1", None, "digest-only")


# ---------- HTTP 协议层（mock registry server） ----------

DIGEST_V1 = "sha256:" + "1" * 64
DIGEST_V2 = "sha256:" + "2" * 64


class MockRegistryHandler(BaseHTTPRequestHandler):
    """协议剧本：先 401 要求 Bearer；带 token 后 200；
    If-None-Match 命中当前摘要 → 304；tags/list 返回固定列表。"""

    tokens_ok: set = set()

    def _auth_ok(self) -> bool:
        auth = self.headers.get("Authorization", "")
        return auth.startswith("Bearer ") and auth.split(" ", 1)[1] in MockRegistryHandler.tokens_ok

    def do_HEAD(self):
        # HEAD 响应一律不携带 body，避免污染 keep-alive 连接流
        path = self.path.split("?")[0]
        if path == "/token":
            return self._token()
        if not self._auth_ok():
            return self.send_response_and_headers(401, {"WWW-Authenticate": 'Bearer realm="http://127.0.0.1:{P}/token",service="mock"'.replace("{P}", str(self.server.server_address[1]))})
        if path == "/v2/app/web/tags/list":
            return self.send_response_and_headers(200, {"Content-Type": "application/json"})
        inm = self.headers.get("If-None-Match")
        if inm == DIGEST_V2:
            return self.send_response_and_headers(304, {})
        return self.send_response_and_headers(
            200, {"Docker-Content-Digest": DIGEST_V2}
        )

    def do_GET(self):
        path = self.path.split("?")[0]
        if path == "/token":
            return self._token()
        if not self._auth_ok():
            return self.send_response_and_headers(401, {"WWW-Authenticate": 'Bearer realm="http://127.0.0.1:{P}/token",service="mock"'.replace("{P}", str(self.server.server_address[1]))})
        if path == "/v2/app/web/tags/list":
            return self.send_response_and_headers(200, {}, body=json.dumps({"tags": ["1.0", "2.0", "2.5", "latest"]}).encode())
        return self.send_response_and_headers(200, {"Docker-Content-Digest": DIGEST_V2})

    def _token(self):
        MockRegistryHandler.tokens_ok.add("mock-token")
        body = json.dumps({"token": "mock-token"}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def send_response_and_headers(self, code, headers, body=b""):
        self.send_response(code)
        for k, v in headers.items():
            self.send_header(k, v)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):  # 静音
        pass


@pytest.fixture()
def mock_registry(monkeypatch):
    """启动本地 HTTP mock registry，并把 127.0.0.1 列入 insecure（走 http）。"""
    monkeypatch.setattr(
        "backend.config.CONFIG.INSECURE_REGISTRIES", "127.0.0.1"
    )
    server = ThreadingHTTPServer(("127.0.0.1", 0), MockRegistryHandler)
    t = threading.Thread(target=server.serve_forever, daemon=True)
    t.start()
    yield f"127.0.0.1:{server.server_address[1]}"
    server.shutdown()


class TestRegistryProtocol:
    def test_bearer_flow_returns_digest(self, mock_registry):
        client = RegistryClient()
        digest = client.remote_digest(mock_registry, "app/web", "latest", None)
        assert digest == DIGEST_V2

    def test_304_when_local_matches_remote(self, mock_registry):
        client = RegistryClient()
        digest = client.remote_digest(mock_registry, "app/web", "latest", DIGEST_V2)
        assert digest == DIGEST_V2  # 304 → 返回本地摘要（未变化）

    def test_list_tags(self, mock_registry):
        client = RegistryClient()
        tags = client.list_tags(mock_registry, "app/web")
        assert "2.5" in tags
