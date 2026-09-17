"""API 端到端测试：登录 → 扫描 → 两级/聚合通知 → 策略 → 更新 → 自动回滚 全链路。

这是 PRD 5.2 场景（compose A/B/C，A、B 有更新）与验收标准的完整复现剧本。
"""
import pytest
from fastapi.testclient import TestClient

from backend import db
from backend.detect import StaticRegistrySource
from backend.docker import MockDockerClient


REGISTRY = StaticRegistrySource({
    "nginx:1.25-alpine": {"digest": "sha256:" + "f" * 64, "tags": ["1.25-alpine", "1.26-alpine"]},
    "myapp:2.0": {"digest": "sha256:" + "e" * 64, "tags": ["2.0", "2.1", "3.0"]},
    "postgres:16.2": {"digest": "sha256:" + "c" * 64, "tags": ["16.2"]},
})


@pytest.fixture(autouse=True)
def _reset_registry():
    """防止用例间的 mock registry 状态污染。"""
    REGISTRY.specs["nginx:1.25-alpine"]["digest"] = "sha256:" + "f" * 64
    REGISTRY.specs["nginx:1.25-alpine"]["tags"] = ["1.25-alpine", "1.26-alpine"]
    REGISTRY.specs["myapp:2.0"]["digest"] = "sha256:" + "e" * 64
    REGISTRY.specs["myapp:2.0"]["tags"] = ["2.0", "2.1", "3.0"]
    yield


def _seed_stack(client: MockDockerClient):
    client.seed_container(
        "demo-web", "nginx:1.25-alpine",
        labels={
            "com.docker.compose.project": "demo-app",
            "com.docker.compose.service": "web",
            "dev.quenary.tugtainer.depends_on": "demo-api",
        },
    )
    client.seed_container(
        "demo-api", "myapp:2.0",
        labels={
            "com.docker.compose.project": "demo-app",
            "com.docker.compose.service": "api",
            "com.docker.compose.depends_on": "db:condition:service_healthy",
            "dev.quenary.tugtainer.depends_on": "demo-db",
        },
    )
    client.seed_container(
        "demo-db", "postgres:16.2",
        labels={"com.docker.compose.project": "demo-app", "com.docker.compose.service": "db"},
    )


@pytest.fixture()
def client(monkeypatch):
    import backend.app as app_module

    app_module.DOCKER = MockDockerClient()
    _seed_stack(app_module.DOCKER)
    monkeypatch.setattr(
        "backend.detect.make_registry_client", lambda: REGISTRY, raising=True
    )
    with TestClient(app_module.app) as tc:
        yield tc, app_module.DOCKER


class TestAuthFlow:
    def test_public_health_open(self, client):
        tc, _ = client
        r = tc.get("/api/public/health")
        assert r.status_code == 200
        assert r.json()["status"] == "ok"

    def test_protected_requires_login(self, client):
        tc, _ = client
        assert tc.get("/api/summary").status_code == 401
        assert tc.get("/api/containers").status_code == 401

    def test_setup_then_login_flow(self, client):
        tc, _ = client
        assert tc.get("/api/auth/check").json()["need_setup"] is True
        # 弱密码拒绝
        assert tc.post("/api/auth/setup", json={"username": "admin", "password": "123"}).status_code == 422
        r = tc.post("/api/auth/setup", json={"username": "admin", "password": "admin123456"})
        assert r.status_code == 200
        assert tc.get("/api/auth/check").json()["authenticated"] is True
        assert tc.get("/api/summary").status_code == 200
        # 重复 setup 被拒
        assert tc.post("/api/auth/setup", json={"username": "x", "password": "xxxxxxx"}).status_code == 409

    def test_login_lockout(self, client):
        tc, _ = client
        tc.post("/api/auth/setup", json={"username": "admin", "password": "admin123456"})
        tc.post("/api/auth/logout")
        for _ in range(5):
            r = tc.post("/api/auth/login", json={"username": "admin", "password": "wrong-pwd"})
            assert r.status_code == 401  # 第 5 次失败本身仍 401（锁定在其后生效）
        r6 = tc.post("/api/auth/login", json={"username": "admin", "password": "wrong-pwd"})
        assert r6.status_code == 429  # 第 6 次被限流拦截
        locked = tc.get("/api/auth/check").json()
        assert locked["locked_sec"] > 0


class TestScanNotifyFlow:
    """PRD 4.1 检测→通知链路 + PRD 5.4 聚合。"""

    def _login(self, tc):
        tc.post("/api/auth/setup", json={"username": "admin", "password": "admin123456"})

    def test_baseline_then_remote_release_triggers_alerts(self, client):
        tc, docker = client
        self._login(tc)
        # ① 首扫：建基线，无告警（首巡现存 tag 入基线不通知）
        out = tc.post("/api/scan").json()
        assert out["status"] == "ok" and out["events"] == 0
        # 容器已入库
        rows = {r["name"]: r for r in tc.get("/api/containers").json()}
        assert set(rows) == {"demo-web", "demo-api", "demo-db"}
        assert rows["demo-db"]["compose_id"] == "demo-app"

        # ② 远端发布新版本：web 摘要变化 + myapp 仓库新出 3.5
        REGISTRY.specs["nginx:1.25-alpine"]["digest"] = "sha256:" + "9" * 64
        REGISTRY.specs["myapp:2.0"]["tags"].append("3.5")
        out2 = tc.post("/api/scan").json()
        # 聚合分级：demo-app|update 一条 + demo-app|new-tag 一条 = 2
        assert out2["events"] == 2

        # ③ 通知中心：项目聚合通知存在且含 web
        notifs = tc.get("/api/notifications").json()
        assert any(n["target"] == "demo-app" for n in notifs)
        # ④ web 现在标记为有更新
        rows = {r["name"]: r for r in tc.get("/api/containers").json()}
        assert rows["demo-web"]["update_available"] == 1

    def test_read_flow(self, client):
        tc, _ = client
        self._login(tc)
        tc.post("/api/scan")  # 基线
        REGISTRY.specs["myapp:2.0"]["tags"].append("3.5")
        tc.post("/api/scan")  # 产生 new-tag 通知
        unread = tc.get("/api/notifications?unread=true").json()
        assert unread, "new-tag 通知应出现"
        r = tc.post(f"/api/notifications/{unread[0]['id']}/read")
        assert r.status_code == 200
        tc.post("/api/notifications/read-all")
        assert tc.get("/api/notifications?unread=true").json() == []
        tc.post("/api/notifications/clear-read")
        assert tc.get("/api/notifications").json() == []


class TestUpdateFlow:
    """PRD 5.2 场景复现：更新 → 拓扑顺序 → 回滚 → 任务通知。"""

    def _prepare(self, tc):
        tc.post("/api/auth/setup", json={"username": "admin", "password": "admin123456"})
        tc.post("/api/scan")  # 基线 + new-tag 通知
        REGISTRY.specs["nginx:1.25-alpine"]["digest"] = "sha256:" + "9" * 64
        REGISTRY.specs["myapp:2.0"]["digest"] = "sha256:" + "8" * 64
        tc.post("/api/scan")  # web、api 有更新
        rows = {r["name"]: r for r in tc.get("/api/containers").json()}
        assert rows["demo-web"]["update_available"] == 1
        assert rows["demo-api"]["update_available"] == 1
        assert rows["demo-db"]["update_available"] == 0

    def test_update_all_respects_topology_and_updates(self, client):
        tc, docker = client
        self._prepare(tc)
        out = tc.post("/api/update?manual=true").json()
        results = {c["name"]: c["result"] for c in out["containers"]}
        assert results["demo-web"] == "updated"
        assert results["demo-api"] == "updated"
        # db 不换镜像
        assert docker.inspect("demo-db")["image"] == "postgres:16.2"
        # 拓扑序：api（被 web 依赖）先重建
        log = docker.event_log
        assert log.index("create:demo-api") < log.index("create:demo-web")
        # 成功后清 update_available
        rows = {r["name"]: r for r in tc.get("/api/containers").json()}
        assert rows["demo-web"]["update_available"] == 0
        # 任务结果通知
        notifs = [n for n in tc.get("/api/notifications").json() if n["type"] == "job"]
        assert notifs and any("成功 2" in str(n["payload"]) or len(n["payload"].get("updated", [])) == 2 for n in notifs)

    def test_single_update_with_rollback(self, client):
        """模拟失败 → 自动回滚 → 回滚单列通知（PRD 5.4）。"""
        tc, docker = client
        self._prepare(tc)
        # 预设 web 重建后 unhealthy
        r = tc.post("/api/demo/simulate-failure", json={"name": "demo-web"})
        assert r.status_code == 200
        out = tc.post("/api/containers/demo-web/update").json()
        web_result = [c for c in out["containers"] if c["name"] == "demo-web"][0]
        assert web_result["result"] == "rolled_back"
        assert docker.inspect("demo-web")["health"] == "healthy"  # 回滚后恢复
        assert "unhealthy:demo-web" in docker.event_log
        # 回滚单列通知
        notifs = tc.get("/api/notifications").json()
        rollback = [n for n in notifs if n["target"] == "demo-web" and n["payload"].get("event") == "rolled_back"]
        assert rollback

    def test_policy_toggles(self, client):
        tc, _ = client
        self._prepare(tc)
        assert tc.put("/api/containers/demo-db/mode", json={"mode": "pin-watch"}).json()["mode"] == "pin-watch"
        assert tc.put("/api/containers/demo-db/ignored", json={"value": True}).json()["ignored"] is True
        assert tc.put("/api/containers/demo-db/update_enabled", json={"value": False}).status_code == 200
        row = [r for r in tc.get("/api/containers").json() if r["name"] == "demo-db"][0]
        assert row["mode"] == "pin-watch" and row["ignored"] == 1 and row["update_enabled"] == 0

    def test_protected_container_not_updatable(self, client):
        """protected 容器（如 socket-proxy）无法手动更新（PRD 边界约束）。"""
        tc, docker = client
        self._prepare(tc)
        docker.seed_container(
            "socket-proxy", "socket:1",
            labels={"dev.quenary.tugtainer.protected": "true"},
        )
        with db.tx() as conn:
            conn.execute(
                "INSERT INTO containers(name, image_spec, update_available, update_enabled) "
                "VALUES('socket-proxy','socket:1',1,1)"
            )
        out = tc.post("/api/update?manual=true").json()
        results = {c["name"]: c["result"] for c in out["containers"]}
        assert "socket-proxy" not in results  # 被 protected 过滤


class TestWatchesAndSettings:
    def test_watch_crud(self, client):
        tc, _ = client
        tc.post("/api/auth/setup", json={"username": "admin", "password": "admin123456"})
        assert tc.post("/api/watches", json={"reference": "nginx:1.25-alpine"}).status_code == 200
        assert tc.post("/api/watches", json={"reference": "bad@@ref"}).status_code == 400
        rows = tc.get("/api/watches").json()
        assert len(rows) == 1
        tc.post("/api/scan")
        w = tc.get("/api/watches").json()[0]
        assert w["baseline_digest"]  # 扫描后记录基线
        assert tc.delete(f"/api/watches/{w['id']}").status_code == 200
        assert tc.get("/api/watches").json() == []

    def test_settings_roundtrip(self, client):
        tc, _ = client
        tc.post("/api/auth/setup", json={"username": "admin", "password": "admin123456"})
        r = tc.put("/api/settings", json={"settings": {"delay_update_sec": "300"}})
        assert r.status_code == 200
        assert tc.get("/api/settings").json()["delay_update_sec"] == "300"
        assert tc.put("/api/settings", json={"settings": {"evil_key": "1"}}).status_code == 400

    def test_channel_crud(self, client):
        tc, _ = client
        tc.post("/api/auth/setup", json={"username": "admin", "password": "admin123456"})
        assert tc.post("/api/channels", json={"kind": "webhook", "name": "n1", "url": "http://127.0.0.1:9/x"}).status_code == 200
        assert tc.post("/api/channels", json={"kind": "wecom", "name": "企微", "url": "https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=x"}).status_code == 200
        assert tc.post("/api/channels", json={"kind": "bogus", "name": "n2", "url": "http://x"}).status_code == 422
        assert len(tc.get("/api/channels").json()) == 2
        assert tc.delete("/api/channels/1").status_code == 200
