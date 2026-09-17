"""扫描编排测试：首检基线 / 防抖 / 两级通知 / 项目聚合 / 自动已读 / 扫描互斥。"""
import threading

import pytest

from backend import db, detect, notify
from backend.detect import StaticRegistrySource, scan
from backend.docker import MockDockerClient, _fake_digest


def _seed_three_containers(client: MockDockerClient):
    client.seed_container(
        "demo-web", "nginx:1.25-alpine",
        labels={"com.docker.compose.project": "demo-app", "com.docker.compose.service": "web"},
    )
    client.seed_container(
        "demo-api", "myapp:2.0",
        labels={"com.docker.compose.project": "demo-app", "com.docker.compose.service": "api"},
    )
    client.seed_container("solo-cache", "redis:7-alpine")  # 独立容器（无 compose）


REGISTRY = StaticRegistrySource({
    "nginx:1.25-alpine": {"digest": "sha256:" + "f" * 64, "tags": ["1.25-alpine"]},
    "myapp:2.0": {"digest": "sha256:" + "e" * 64, "tags": ["2.0", "2.1", "3.0"]},
    "redis:7-alpine": {"digest": "sha256:" + "d" * 64, "tags": ["7-alpine"]},
})


@pytest.fixture(autouse=True)
def _reset_registry():
    """防止用例间的 mock registry 状态污染。"""
    REGISTRY.specs["nginx:1.25-alpine"]["digest"] = "sha256:" + "f" * 64
    REGISTRY.specs["myapp:2.0"]["tags"] = ["2.0", "2.1", "3.0"]
    REGISTRY.specs["redis:7-alpine"]["digest"] = "sha256:" + "d" * 64
    yield


@pytest.fixture()
def seeded_client():
    c = MockDockerClient()
    _seed_three_containers(c)
    return c


class TestFirstScanBaseline:
    def test_first_scan_no_alert(self, seeded_client, monkeypatch):
        """首检基线（PRD 4.1）：首次扫描建基线不告警，且 local 与远端摘要对齐。"""
        monkeypatch.setattr(detect, "make_registry_client", lambda: REGISTRY)
        out = scan(seeded_client)
        assert out["status"] == "ok"
        assert out["events"] == 0
        rows = {r["name"]: r for r in db.query("SELECT * FROM containers")}
        expected = REGISTRY.specs["nginx:1.25-alpine"]["digest"]
        assert rows["demo-web"]["local_digest"] == expected
        assert rows["demo-web"]["remote_digest"] == expected
        assert rows["demo-web"]["update_available"] == 0


class TestDetectAndNotify:
    def test_update_alert_once_and_dedup(self, seeded_client, monkeypatch):
        """摘要变化 → update 通知一次；重复扫描同摘要不再通知（防抖）。"""
        monkeypatch.setattr(detect, "make_registry_client", lambda: REGISTRY)
        scan(seeded_client)  # 基线
        # 模拟远端摘要变化：改变本地摘要记录（等同于"远端更新了"）
        seeded_client.remove("demo-web")
        seeded_client.seed_container(
            "demo-web", "nginx:1.25-alpine",
            labels={"com.docker.compose.project": "demo-app", "com.docker.compose.service": "web"},
        )
        # 强制 local_digest 与远端不同：直接篡改 DB 行的 local_digest
        with db.tx() as conn:
            conn.execute("UPDATE containers SET local_digest='sha256:stale' WHERE name='demo-web'")
        out1 = scan(seeded_client)
        assert out1["events"] == 1
        out2 = scan(seeded_client)  # 同摘要重复扫描
        assert out2["events"] == 0  # 防抖生效

    def test_new_tag_alert_per_tag(self, seeded_client, monkeypatch):
        """Pin-Watch：首巡基线吞掉现存更高 tag（不通知）；
        仓库后续新出现 3.5 → new-tag 通知一次，重复扫描不重发。"""
        monkeypatch.setattr(detect, "make_registry_client", lambda: REGISTRY)
        out0 = scan(seeded_client)  # 首巡：2.1/3.0 入基线，无事件
        assert out0["events"] == 0
        # 仓库发布新大版本 3.5
        REGISTRY.specs["myapp:2.0"]["tags"].append("3.5")
        with db.tx() as conn:
            conn.execute("UPDATE containers SET local_digest='sha256:stale2' WHERE name='demo-api'")
        out = scan(seeded_client)
        assert out["events"] == 2  # 1 update 聚合 + 1 new-tag 聚合（3.5）
        new_tags = []
        for r in db.query("SELECT * FROM notifications"):
            if r["type"] == "new-tag":
                p = db.jload(r["payload"])
                new_tags += [s.get("tag") for s in p.get("services", [])]
        assert new_tags == ["3.5"]
        out2 = scan(seeded_client)
        assert out2["events"] == 0  # 每 tag 仅一次（防抖）
        # 基线状态可在容器行看到（UI 展示口径）
        api_row = db.query_one("SELECT * FROM containers WHERE name='demo-api'")
        assert "3.5" in db.jload(api_row["newer_tags"])

    def test_compose_project_aggregation(self, seeded_client, monkeypatch):
        """同项目同批次发现 → 聚合为一条项目级通知（PRD 5.4）。"""
        monkeypatch.setattr(detect, "make_registry_client", lambda: REGISTRY)
        scan(seeded_client)
        with db.tx() as conn:
            conn.execute("UPDATE containers SET local_digest='sha256:stale' WHERE name IN ('demo-web','demo-api')")
        out = scan(seeded_client)
        # demo-web + demo-api 两条差异 → 聚合为 1 条项目级新通知
        assert out["events"] == 1
        project_notifs = [r for r in db.query("SELECT * FROM notifications") if r["target"] == "demo-app"]
        assert len(project_notifs) == 1  # 聚合为一条
        payload = db.jload(project_notifs[0]["payload"])
        assert payload["project"] == "demo-app"
        assert len(payload["services"]) == 2

    def test_force_scan_rebroadcasts(self, seeded_client, monkeypatch):
        """强制扫描：无视防抖与已读，重播所有差异（PRD 3.4）。"""
        monkeypatch.setattr(detect, "make_registry_client", lambda: REGISTRY)
        scan(seeded_client)
        with db.tx() as conn:
            conn.execute("UPDATE containers SET local_digest='sha256:stale' WHERE name='demo-web'")
        scan(seeded_client)
        before = db.query("SELECT COUNT(*) AS n FROM notifications")[0]["n"]
        scan(seeded_client, force=True)
        after = db.query("SELECT COUNT(*) AS n FROM notifications")[0]["n"]
        assert after > before

    def test_auto_mark_read_when_resolved(self, seeded_client, monkeypatch):
        """目标达成自动已读：本地摘要同步到通知记录的新摘要 → 未读转已读。"""
        monkeypatch.setattr(detect, "make_registry_client", lambda: REGISTRY)
        scan(seeded_client)
        with db.tx() as conn:
            conn.execute("UPDATE containers SET local_digest='sha256:stale' WHERE name='demo-web'")
        scan(seeded_client)
        unread_before = db.query("SELECT COUNT(*) AS n FROM notifications WHERE read_at IS NULL")[0]["n"]
        assert unread_before >= 1
        # 容器"已更新到新摘要"：local_digest == remote_digest
        with db.tx() as conn:
            conn.execute(
                "UPDATE containers SET local_digest=remote_digest WHERE name='demo-web'"
            )
        marked = notify.auto_mark_read({r["name"]: r for r in db.query("SELECT * FROM containers")})
        assert marked >= 1

    def test_scan_mutex(self, seeded_client, monkeypatch):
        """扫描互斥：并发第二次进入返回 scan already running（继承 Vigil）。"""
        monkeypatch.setattr(detect, "make_registry_client", lambda: REGISTRY)

        result = {}
        def slow_scan():
            with detect._SCAN_LOCK:  # 模拟慢扫描持锁
                import time as _t
                _t.sleep(0.4)

        t = threading.Thread(target=slow_scan)
        t.start()
        import time as _t
        _t.sleep(0.1)
        out = scan(seeded_client)
        assert out["status"] == "scan already running"
        t.join()


class TestWatches:
    def test_watch_baseline_recorded(self, seeded_client, monkeypatch):
        """纯远端监控：记录基线摘要（PRD 3.6）。"""
        monkeypatch.setattr(detect, "make_registry_client", lambda: REGISTRY)
        with db.tx() as conn:
            conn.execute("INSERT INTO watches(reference) VALUES('redis:7-alpine')")
        scan(seeded_client)
        w = db.query_one("SELECT * FROM watches WHERE reference='redis:7-alpine'")
        assert w["baseline_digest"] == "sha256:" + "d" * 64
        assert w["last_checked_at"] is not None

    def test_watch_change_notifies_once(self, seeded_client, monkeypatch):
        """哨兵通知：首检基线不告警 → 上游摘要变化 → update 通知一次（同摘要去重）。"""
        monkeypatch.setattr(detect, "make_registry_client", lambda: REGISTRY)
        with db.tx() as conn:
            conn.execute("INSERT INTO watches(reference) VALUES('redis:7-alpine')")
        scan(seeded_client)  # 首检：建基线
        assert db.query_one("SELECT * FROM notifications WHERE type='update'") is None

        REGISTRY.specs["redis:7-alpine"]["digest"] = "sha256:" + "9" * 64
        scan(seeded_client)  # 变化：通知
        n = db.query_one(
            "SELECT * FROM notifications WHERE type='update' AND target='redis:7-alpine'"
        )
        assert n is not None
        payload = db.jload(n["payload"], {})
        assert payload["watch"] is True
        assert payload["new_digest"] == "sha256:" + "9" * 64

        scan(seeded_client)  # 同摘要：去重不重发
        cnt = len(db.query(
            "SELECT * FROM notifications WHERE dedup_key='watch-update|redis:7-alpine|sha256:" + "9" * 64 + "'"
        ))
        assert cnt == 1

    def test_watch_newtag_after_baseline(self, seeded_client, monkeypatch):
        """pin-watch：首检把现存更高 tag 记为已见；之后新出现的 tag 才提醒。"""
        monkeypatch.setattr(detect, "make_registry_client", lambda: REGISTRY)
        with db.tx() as conn:
            conn.execute("INSERT INTO watches(reference) VALUES('myapp:2.0')")
        scan(seeded_client)  # 首检：2.1/3.0 记为已见基线
        w = db.query_one("SELECT * FROM watches WHERE reference='myapp:2.0'")
        assert db.jload(w["newer_tags"], []) == ["2.1", "3.0"]
        assert db.query_one("SELECT * FROM notifications WHERE type='new-tag'") is None

        REGISTRY.specs["myapp:2.0"]["tags"] = ["2.0", "2.1", "3.0", "3.1"]
        scan(seeded_client)
        n = db.query_one(
            "SELECT * FROM notifications WHERE type='new-tag' AND target='myapp:2.0'"
        )
        assert n is not None
        assert db.jload(n["payload"], {})["tag"] == "3.1"

    def test_watch_force_replays_newtags(self, seeded_client, monkeypatch):
        """强制扫描：重播基线外全部新版本 tag（无视已见集合）。"""
        monkeypatch.setattr(detect, "make_registry_client", lambda: REGISTRY)
        with db.tx() as conn:
            conn.execute("INSERT INTO watches(reference) VALUES('myapp:2.0')")
        scan(seeded_client)
        scan(seeded_client, force=True)
        tags = {db.jload(n["payload"], {}).get("tag")
                for n in db.query(
                    "SELECT * FROM notifications WHERE type='new-tag' AND target='myapp:2.0'"
                )}
        assert tags == {"2.1", "3.0"}
