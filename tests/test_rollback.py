"""手动版本回退测试：版本台账 / 回退执行 / 乒乓回退 / 互斥 / 自动回滚镜像 ID 修复 / API。"""
import pytest
from fastapi.testclient import TestClient

from backend import db, engine
from backend import detect as detect_mod
from backend.detect import StaticRegistrySource
from backend.docker import MockDockerClient, _fake_digest

SPEC = "nginx:1.25"
D_OLD = "sha256:" + "a" * 64
D_NEW = "sha256:" + "b" * 64
D_NEW2 = "sha256:" + "c" * 64


@pytest.fixture(autouse=True)
def _clean_lock():
    """用例间释放可能被异常持有的更新互斥锁。"""
    if engine._UPDATE_LOCK.locked():
        engine._UPDATE_LOCK.release()
    yield
    if engine._UPDATE_LOCK.locked():
        engine._UPDATE_LOCK.release()


@pytest.fixture()
def reg():
    return StaticRegistrySource({SPEC: {"digest": D_OLD, "tags": ["1.25"]}})


@pytest.fixture()
def seeded():
    d = MockDockerClient()
    d.seed_container("web", SPEC)
    return d


def _scan(d: MockDockerClient, reg: StaticRegistrySource, monkeypatch) -> None:
    monkeypatch.setattr(detect_mod, "make_registry_client", lambda: reg)
    detect_mod.scan(d)


def _update_to(seeded, reg, monkeypatch, digest):
    """驱动一次完整更新：改注册表摘要 + docker pull 结果 → 扫描 → 更新。"""
    reg.specs[SPEC]["digest"] = digest
    seeded.set_digest(SPEC, digest)
    _scan(seeded, reg, monkeypatch)
    out = engine.run_update(seeded, manual=False)
    assert out["containers"][0]["result"] == "updated"
    return out


class TestVersionLedger:
    def test_baseline_recorded_on_first_scan(self, seeded, reg, monkeypatch):
        _scan(seeded, reg, monkeypatch)
        led = db.query("SELECT * FROM container_versions WHERE name='web' ORDER BY id")
        assert [v["digest"] for v in led] == [D_OLD]
        assert led[0]["source"] == "baseline"

    def test_update_appends_new_version(self, seeded, reg, monkeypatch):
        _scan(seeded, reg, monkeypatch)
        _update_to(seeded, reg, monkeypatch, D_NEW)
        led = db.query("SELECT digest, source FROM container_versions WHERE name='web' ORDER BY id")
        assert [(v["digest"], v["source"]) for v in led] == [(D_OLD, "baseline"), (D_NEW, "update")]

    def test_ledger_dedup_and_prune(self, seeded, reg, monkeypatch):
        """重复摘要不重复记录；超出 10 条裁剪最旧。"""
        _scan(seeded, reg, monkeypatch)
        for d in [D_NEW, D_OLD, D_NEW, D_NEW2]:
            _update_to(seeded, reg, monkeypatch, d)
        led = db.query("SELECT digest FROM container_versions WHERE name='web' ORDER BY id")
        assert [v["digest"] for v in led] == [D_OLD, D_NEW, D_NEW2]
        # 压到 10+ 条验证裁剪
        for i in range(12):
            engine.record_version("web", f"sha256:{str(i).zfill(64)}", SPEC, "update")
        cnt = db.query_one("SELECT COUNT(*) AS n FROM container_versions WHERE name='web'")["n"]
        assert cnt == 10


class TestManualRollback:
    def _setup_updated(self, seeded, reg, monkeypatch):
        _scan(seeded, reg, monkeypatch)
        _update_to(seeded, reg, monkeypatch, D_NEW)

    def test_rollback_restores_previous(self, seeded, reg, monkeypatch):
        self._setup_updated(seeded, reg, monkeypatch)
        out = engine.run_rollback(seeded, "web")
        assert out["status"] == "ok"
        assert out["from_digest"] == D_NEW
        assert out["to_digest"] == D_OLD
        c = seeded.inspect("web")
        assert c["image_id"] == D_OLD
        assert c["running"] is True
        row = db.query_one("SELECT * FROM containers WHERE name='web'")
        assert row["local_digest"] == D_OLD
        assert row["update_available"] == 0
        # 台账保持两条（存在即去重）
        led = db.query("SELECT digest FROM container_versions WHERE name='web' ORDER BY id")
        assert [v["digest"] for v in led] == [D_OLD, D_NEW]

    def test_rollback_pingpong(self, seeded, reg, monkeypatch):
        """回退后再回退 → 回到新版本（乒乓切换）。"""
        self._setup_updated(seeded, reg, monkeypatch)
        assert engine.run_rollback(seeded, "web")["to_digest"] == D_OLD
        assert engine.run_rollback(seeded, "web")["to_digest"] == D_NEW

    def test_rollback_explicit_digest(self, seeded, reg, monkeypatch):
        """连升两版后显式回退到指定历史版本。"""
        _scan(seeded, reg, monkeypatch)
        _update_to(seeded, reg, monkeypatch, D_NEW)
        _update_to(seeded, reg, monkeypatch, D_NEW2)
        out = engine.run_rollback(seeded, "web", digest=D_OLD)
        assert out["to_digest"] == D_OLD
        assert seeded.inspect("web")["image_id"] == D_OLD

    def test_rollback_no_previous_version(self, seeded, monkeypatch):
        """只有当前版本（无历史）→ 明确报错，且当前版本已入台账。"""
        seeded.seed_container("web2", "x:y")
        with db.tx() as conn:
            conn.execute(
                "INSERT INTO containers(name, image_spec, local_digest) VALUES('web2','x:y',?)",
                ("sha256:" + "d" * 64,),
            )
        with pytest.raises(Exception, match="no previous version"):
            engine.run_rollback(seeded, "web2")
        # 当前版本已留存台账，为将来回退提供基点
        led = db.query("SELECT digest FROM container_versions WHERE name='web2'")
        assert [v["digest"] for v in led] == ["sha256:" + "d" * 64]

    def test_rollback_missing_container(self, seeded):
        with pytest.raises(Exception, match="no such container"):
            engine.run_rollback(seeded, "ghost")

    def test_rollback_disables_auto_update(self, seeded, reg, monkeypatch):
        """回退后自动更新开关自动关闭；重新打开开关后自动更新恢复。"""
        self._setup_updated(seeded, reg, monkeypatch)
        out = engine.run_rollback(seeded, "web")
        assert out["update_enabled"] is False
        row = db.query_one("SELECT * FROM containers WHERE name='web'")
        assert row["update_enabled"] == 0
        # 下轮扫描：远端仍更新 → 重新标记"有更新"（如实呈现），但自动更新已关
        _scan(seeded, reg, monkeypatch)
        row = db.query_one("SELECT * FROM containers WHERE name='web'")
        assert row["update_available"] == 1
        assert engine.auto_update_pending(seeded) is False
        out = engine.run_update(seeded, manual=False)
        assert out["plan"]["to_update"] == []
        assert out["plan"]["reasons"]["web"] == "no-update-or-disabled"
        # 用户重新打开自动开关 → 恢复自动更新
        with db.tx() as conn:
            conn.execute("UPDATE containers SET update_enabled=1 WHERE name='web'")
        assert engine.auto_update_pending(seeded) is True
        out2 = engine.run_update(seeded, manual=False)
        assert out2["containers"][0]["result"] == "updated"

    def test_rollback_mutex(self, seeded):
        """更新互斥：锁占用时回退立即让行。"""
        with engine._UPDATE_LOCK:
            out = engine.run_rollback(seeded, "web")
        assert out["status"] == "update already running"


class TestAutoRollbackImageId:
    def test_auto_rollback_rebuilds_with_old_image_id(self, seeded, reg, monkeypatch):
        """修复验证：自动回滚必须用旧镜像 ID 重建（真实 Docker 中 tag 已指向新镜像）。"""
        _scan(seeded, reg, monkeypatch)  # 基线：image_id = _fake_digest(SPEC)
        seeded.mark_unhealthy_once("web")
        reg.specs[SPEC]["digest"] = D_NEW
        seeded.set_digest(SPEC, D_NEW)
        _scan(seeded, reg, monkeypatch)
        out = engine.run_update(seeded, manual=False)
        r = out["containers"][0]
        assert r["result"] == "rolled_back"
        c = seeded.inspect("web")
        # 关键断言：回滚后的镜像 ID 是旧镜像，而非新摘要
        assert c["image_id"] == _fake_digest(SPEC)
        assert c["image_id"] != D_NEW
        assert c["running"] is True


class TestRollbackAPI:
    @pytest.fixture()
    def client(self, monkeypatch):
        import backend.app as app_module

        app_module.DOCKER = MockDockerClient()
        app_module.DOCKER.seed_container("web", SPEC)
        monkeypatch.setattr(
            "backend.detect.make_registry_client",
            lambda: StaticRegistrySource({SPEC: {"digest": D_OLD, "tags": ["1.25"]}}),
        )
        with TestClient(app_module.app) as tc:
            tc.post("/api/auth/setup", json={"username": "admin", "password": "admin123456"})
            yield tc, app_module.DOCKER

    def test_versions_and_rollback_api(self, client, monkeypatch):
        tc, d = client
        tc.post("/api/auth/login", json={"username": "admin", "password": "admin123456"})
        # 基线 + 更新
        reg = StaticRegistrySource({SPEC: {"digest": D_OLD, "tags": ["1.25"]}})
        monkeypatch.setattr(detect_mod, "make_registry_client", lambda: reg)
        detect_mod.scan(d)
        reg.specs[SPEC]["digest"] = D_NEW
        d.set_digest(SPEC, D_NEW)
        detect_mod.scan(d)
        engine.run_update(d, manual=False)
        # 版本列表
        r = tc.get("/api/containers/web/versions")
        assert r.status_code == 200
        body = r.json()
        assert body["current_digest"] == D_NEW
        assert body["rollback_target"]["digest"] == D_OLD
        assert [v["digest"] for v in body["versions"]] == [D_NEW, D_OLD]
        assert body["versions"][0]["is_current"] is True
        # 回退
        r = tc.post("/api/containers/web/rollback", json={})
        assert r.status_code == 200
        assert r.json()["to_digest"] == D_OLD
        assert d.inspect("web")["image_id"] == D_OLD
        # 显式指定不存在的 digest → 目标不可用报错
        r = tc.post("/api/containers/web/rollback", json={"digest": D_NEW2})
        assert r.status_code in (400, 500)
