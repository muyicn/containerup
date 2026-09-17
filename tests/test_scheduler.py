"""调度器测试：热生效、到点执行、互斥、候选就绪判断、状态上报。

覆盖"自动更新什么时候执行"的核心语义（PRD 5.3）：
- scan_interval_sec=0 → 挂起不执行；改配置 → 秒级热生效并开始自动执行
- 到期轮次 = 扫描（基线/发现）→ 候选就绪才 run_update(manual=False)
- 更新全局互斥：手动与调度忙时立即让行（不排队）
"""
import time

import pytest

from backend import db, engine, scheduler
from backend.detect import StaticRegistrySource
from backend.docker import MockDockerClient

DIGEST_OLD = "sha256:" + "1" * 64
DIGEST_NEW = "sha256:" + "2" * 64


@pytest.fixture(autouse=True)
def _stop_scheduler():
    """每个用例结束强制停线程，防止泄漏到后续用例。"""
    yield
    scheduler.stop()


def _wait_until(pred, timeout: float = 8.0, step: float = 0.2) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if pred():
            return True
        time.sleep(step)
    return pred()


def test_run_update_busy_returns_immediately():
    """更新互斥：锁被占用时 run_update 不排队，立即返回 already-running。"""
    with engine._UPDATE_LOCK:
        out = engine.run_update(MockDockerClient(), manual=True)
    assert out == {"status": "update already running", "job_id": None}


def test_auto_update_pending_filters():
    """候选就绪判断：仅"有更新 + 启用 + 未忽略/冻结 + 延迟到期"为真。"""
    docker = MockDockerClient()
    docker.seed_container("web", "nginx:1.25-alpine")
    with db.tx() as conn:
        conn.execute(
            "INSERT INTO containers(name, image_spec) VALUES('web','nginx:1.25-alpine')"
        )

    def set_flag(**kw):
        with db.tx() as conn:
            conn.execute(
                "UPDATE containers SET " + ", ".join(f"{k}=?" for k in kw),
                tuple(kw.values()),
            )

    set_flag(update_available=1)
    assert engine.auto_update_pending(docker) is True

    set_flag(ignored=1)
    assert engine.auto_update_pending(docker) is False
    set_flag(ignored=0, freeze=1)
    assert engine.auto_update_pending(docker) is False
    set_flag(freeze=0, update_enabled=0)
    assert engine.auto_update_pending(docker) is False
    set_flag(update_enabled=1, delay_update_for=9999, remote_changed_at=db.now_iso())
    assert engine.auto_update_pending(docker) is False
    set_flag(delay_update_for=0)
    assert engine.auto_update_pending(docker) is True


def test_scheduler_disabled_by_default():
    """默认（未配置间隔）挂起：线程活着但不产生任何轮次。"""
    docker = MockDockerClient()
    scheduler.start(lambda: docker)
    assert scheduler.status()["running"] is True
    assert scheduler.status()["enabled"] is False
    time.sleep(1.5)
    assert scheduler.status()["cycles"] == 0


def test_hot_reload_then_full_auto_cycle(monkeypatch):
    """热生效 + 全自动轮次：挂起态改配置 → 自动扫描 → 发现更新 → 自动执行。"""
    docker = MockDockerClient()
    docker.seed_container("web", "nginx:1.25-alpine")
    REGISTRY = StaticRegistrySource({
        "nginx:1.25-alpine": {"digest": DIGEST_OLD, "tags": ["1.25-alpine"]},
    })
    monkeypatch.setattr("backend.detect.make_registry_client", lambda: REGISTRY)

    # 挂起态启动（未配置间隔）
    scheduler.start(lambda: docker)
    time.sleep(1.2)
    assert scheduler.status()["cycles"] == 0

    # 热生效：改成 1 秒 → 无需重启自动开始
    db.setting_set("scan_interval_sec", "1")
    assert _wait_until(lambda: scheduler.status()["cycles"] >= 1), scheduler.status()

    # 首轮为基线：无更新、无任务
    st = scheduler.status()
    assert st["enabled"] is True
    assert st["interval_sec"] == 1
    assert st["last_result"]["scan"]["status"] == "ok"
    assert st["last_result"]["update"] == "no-ready-candidates"
    assert st["next_due"] is not None

    # 仓库发布新摘要 + docker pull 结果同步变化
    REGISTRY.specs["nginx:1.25-alpine"]["digest"] = DIGEST_NEW
    docker.set_digest("nginx:1.25-alpine", DIGEST_NEW)

    # 等待下一个自动轮次执行完更新（last_result 在 run_update 落库后写入）
    assert _wait_until(
        lambda: (scheduler.status().get("last_result") or {}).get("update") == "executed"
    ), scheduler.status()
    st = scheduler.status()
    assert st["last_result"]["containers"][0]["result"] == "updated"
    # DB 摘要对齐：更新可用清零、本地=远端=新摘要
    row = db.query_one("SELECT * FROM containers WHERE name='web'")
    assert row["update_available"] == 0
    assert row["local_digest"] == DIGEST_NEW
    assert row["remote_digest"] == DIGEST_NEW
    # 任务落库且成功
    job = db.query_one("SELECT * FROM jobs ORDER BY id DESC LIMIT 1")
    assert job and job["status"] == "done"
    # 自动更新产生任务完成通知（trigger=auto，与手动路径共用语义）
    notif = db.query_one(
        "SELECT * FROM notifications WHERE type='job' AND target='update-job' "
        "ORDER BY id DESC LIMIT 1"
    )
    assert notif, "auto update must emit job notification"
    assert db.jload(notif["payload"], {}).get("trigger") == "auto"
    # 容器保持运行
    assert docker.inspect("web")["running"] is True


def test_interval_change_takes_effect_without_restart(monkeypatch):
    """运行中改间隔：3 秒 → 1 秒，下一轮按新间隔到期（热生效语义）。"""
    docker = MockDockerClient()
    docker.seed_container("web", "nginx:1.25-alpine")
    REGISTRY = StaticRegistrySource({
        "nginx:1.25-alpine": {"digest": DIGEST_OLD, "tags": ["1.25-alpine"]},
    })
    monkeypatch.setattr("backend.detect.make_registry_client", lambda: REGISTRY)
    db.setting_set("scan_interval_sec", "30")
    scheduler.start(lambda: docker)
    assert _wait_until(lambda: scheduler.status()["cycles"] >= 1)
    # 运行中调小间隔 → 状态里的 interval 立即跟随
    db.setting_set("scan_interval_sec", "1")
    assert _wait_until(lambda: scheduler.status()["interval_sec"] == 1)
    scheduler.stop()
