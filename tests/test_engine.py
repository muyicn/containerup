"""更新引擎测试：compose 组识别 + 拓扑计划（PRD 5.2 A/B/C 场景）+ 执行/回滚/失败传播。

核心场景复现（PRD 5.2）：compose 项目 demo-app 内 web → api → db，
web 与 api 有更新、db 没有 —— 验证 partial 策略的顺序、affected 语义与失败传播。
"""
import pytest

from backend import db
from backend.docker import MockDockerClient
from backend.engine import (
    PROTECTED_LABEL,
    UpdateEngine,
    build_update_plan,
    get_compose_id,
    run_update,
)


def _seed_demo_stack(client: MockDockerClient, *, api_health_fail_once=False):
    """web → api → db 依赖链（compose 项目 demo-app）。"""
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
        labels={
            "com.docker.compose.project": "demo-app",
            "com.docker.compose.service": "db",
        },
    )
    if api_health_fail_once:
        client.mark_unhealthy_once("demo-api")


def _db_rows(*, web_update=True, api_update=True, db_update=False, **policy):
    """构造 containers 表行（策略与检测状态）。"""
    rows = {}
    for name, image, upd in [
        ("demo-web", "nginx:1.25-alpine", web_update),
        ("demo-api", "myapp:2.0", api_update),
        ("demo-db", "postgres:16.2", db_update),
    ]:
        rows[name] = {
            "name": name, "image_spec": image, "compose_id": "demo-app",
            "update_available": 1 if upd else 0, "update_enabled": 1,
            "ignored": policy.get("ignored", 0), "freeze": policy.get("freeze", 0),
            "delay_update_for": policy.get("delay"), "remote_changed_at": None,
            "local_digest": "sha256:old",
        }
    return rows


class TestComposeModeling:
    def test_compose_id_from_label(self):
        _seed_demo_stack(MockDockerClient())
        c = MockDockerClient()
        c.seed_container("x", "a:1", labels={"com.docker.compose.project": "p1"})
        assert get_compose_id(c.inspect("x")) == "p1"

    def test_no_compose_returns_none(self):
        c = MockDockerClient()
        c.seed_container("x", "a:1")
        assert get_compose_id(c.inspect("x")) is None


class TestPlanBuilder:
    """PRD 5.2 计划构建：候选过滤、affected 闭包、拓扑顺序。"""

    def _plan(self, client, db_rows_map, **kw):
        return build_update_plan(
            client.list_containers(), db_rows_map, **kw
        )

    def test_partial_web_and_api_update_db_untouched(self):
        client = MockDockerClient()
        _seed_demo_stack(client)
        plan = self._plan(client, _db_rows())
        assert plan.to_update == {"demo-web", "demo-api"}
        assert "demo-db" not in plan.to_update
        assert plan.groups["demo-app"] == ["demo-web", "demo-api", "demo-db"]

    def test_topology_order_db_before_api_before_web(self):
        """拓扑序：被依赖方（db）在前 → api → web（更新阶段先上游后下游）。"""
        client = MockDockerClient()
        _seed_demo_stack(client)
        plan = self._plan(client, _db_rows())
        # order 只含 to_update + affected 成员
        idx = {n: i for i, n in enumerate(plan.order)}
        assert idx["demo-api"] < idx["demo-web"]  # api（被 web 依赖）先于 web

    def test_affected_when_only_api_updates(self):
        """只有 api 更新时，依赖它的 web 是 affected（停止后重启重连）。"""
        client = MockDockerClient()
        _seed_demo_stack(client)
        rows = _db_rows(api_update=True, web_update=False)
        plan = self._plan(client, rows)
        assert plan.to_update == {"demo-api"}
        assert plan.affected == {"demo-web"}

    def test_ignored_and_freeze_excluded(self):
        client = MockDockerClient()
        _seed_demo_stack(client)
        # 逐个验证：ignored / freeze 语义
        rows = _db_rows()
        rows["demo-web"]["ignored"] = 1
        plan = self._plan(client, rows)
        assert "demo-web" not in plan.to_update
        assert plan.reasons.get("demo-web") == "ignored"
        rows = _db_rows()
        rows["demo-web"]["freeze"] = 1
        plan = self._plan(client, rows)
        assert "demo-web" not in plan.to_update
        assert plan.reasons.get("demo-web") == "freeze"

    def test_protected_label_hard_block_even_manual(self):
        client = MockDockerClient()
        client.seed_container(
            "proxy", "socket:1", labels={PROTECTED_LABEL: "true"}
        )
        rows = {"proxy": {"name": "proxy", "image_spec": "socket:1",
                          "update_available": 1, "update_enabled": 1,
                          "ignored": 0, "freeze": 0, "delay_update_for": None,
                          "remote_changed_at": None, "local_digest": "x"}}
        plan = build_update_plan(client.list_containers(), rows, candidates={"proxy"})
        assert plan.to_update == set()
        assert plan.reasons["proxy"] == "protected"

    def test_manual_candidates_bypass_delay(self):
        """手动单容器更新绕过发布延迟（PRD 5.3：绕过②③不绕健康门控）。"""
        client = MockDockerClient()
        _seed_demo_stack(client)
        rows = _db_rows()
        rows["demo-web"]["delay_update_for"] = 3600
        rows["demo-web"]["remote_changed_at"] = db.now_iso()
        plan_auto = self._plan(client, rows, global_delay_sec=0)
        assert "demo-web" not in plan_auto.to_update  # 延迟未到期
        plan_manual = build_update_plan(client.list_containers(), rows, candidates={"demo-web"})
        assert plan_manual.to_update == {"demo-web"}

    def test_merge_wait_holds_group(self):
        """合并等待窗口：组内 db 有更新但延迟未到期 → 整组挂起（消除中间态）。"""
        client = MockDockerClient()
        _seed_demo_stack(client)
        rows = _db_rows(db_update=True)
        rows["demo-db"]["delay_update_for"] = 3600
        rows["demo-db"]["remote_changed_at"] = db.now_iso()
        plan = self._plan(client, rows, merge_wait_sec=60)
        assert plan.to_update == set()
        assert plan.reasons.get("demo-web") == "merge-wait"
        assert plan.reasons.get("demo-db").startswith("delay")


class TestExecute:
    def test_update_order_and_db_untouched(self):
        """执行验证：api 先于 web 更新（事件序），db 不重建。"""
        client = MockDockerClient()
        _seed_demo_stack(client)
        plan = build_update_plan(client.list_containers(), _db_rows())
        engine = UpdateEngine(client)
        results = engine.execute(plan)
        by_name = {r.name: r.result for r in results}
        assert by_name["demo-web"] == "updated"
        assert by_name["demo-api"] == "updated"
        assert "demo-db" not in by_name  # db 未入选也未受影响（无人依赖它更新）
        log = client.event_log
        # 停止逆序：web 先停
        assert log.index("stop:demo-web") < log.index("stop:demo-api")
        # 更新正序：api 先重建
        assert log.index("create:demo-api") < log.index("create:demo-web")
        assert not any(e.startswith("create:demo-db") for e in log)
        # 均恢复运行
        for n in ("demo-web", "demo-api", "demo-db"):
            assert client.inspect(n)["running"] is True

    def test_affected_restart_reconnect(self):
        """仅 api 更新：web 作为 affected 被停止并在上游恢复后重启（PRD partial）。"""
        client = MockDockerClient()
        _seed_demo_stack(client)
        rows = _db_rows(api_update=True, web_update=False)
        plan = build_update_plan(client.list_containers(), rows)
        results = UpdateEngine(client).execute(plan)
        by_name = {r.name: r.result for r in results}
        assert by_name["demo-api"] == "updated"
        assert by_name["demo-web"] == "started"  # 受影响容器：不换镜像，重启重连
        assert client.inspect("demo-web")["image"] == "nginx:1.25-alpine"  # 镜像未变
        log = client.event_log
        assert log.index("stop:demo-web") < log.index("create:demo-api") < log.index("start:demo-api")
        # web 的重连发生在 api 更新完成之后
        assert log.index("create:demo-api") < log.index("start:demo-web")

    def test_unhealthy_after_update_rolls_back(self):
        """新版本启动失败 → 自动回滚旧镜像（PRD 3.2 ⑤ / 验收标准）。"""
        client = MockDockerClient()
        _seed_demo_stack(client, api_health_fail_once=True)
        old_image_id = client.inspect("demo-api")["image_id"]
        rows = _db_rows()
        plan = build_update_plan(client.list_containers(), rows)
        results = UpdateEngine(client).execute(plan)
        by_name = {r.name: r for r in results}
        assert by_name["demo-api"].result == "rolled_back"
        assert "unhealthy:demo-api" in client.event_log  # 复现了启动失败剧本
        assert client.inspect("demo-api")["health"] == "healthy"  # 回滚后恢复
        assert client.inspect("demo-api")["running"] is True
        # web 不应被换新（失败传播：上游失败 → 下游跳过保持）
        assert by_name["demo-web"].result == "skipped"

    def test_upstream_failure_skips_downstream(self):
        """上游 api 回滚 → 下游 web 跳过且保持运行（无撕裂状态）。"""
        client = MockDockerClient()
        _seed_demo_stack(client, api_health_fail_once=True)
        rows = _db_rows()
        plan = build_update_plan(client.list_containers(), rows)
        results = UpdateEngine(client).execute(plan)
        assert {r.name: r.result for r in results}["demo-web"] == "skipped"
        assert client.inspect("demo-web")["image"] == "nginx:1.25-alpine"
        assert client.inspect("demo-web")["running"] is True

    def test_stopped_container_stays_stopped_after_update(self):
        """原本停止的容器：更新后保持停止（PRD 3.2 "wasn't running → 视为成功"）。"""
        client = MockDockerClient()
        _seed_demo_stack(client)
        client.stop("demo-db")
        rows = _db_rows(db_update=True)
        plan = build_update_plan(client.list_containers(), rows)
        results = UpdateEngine(client).execute(plan)
        assert {r.name: r.result for r in results}["demo-db"] == "updated"
        assert client.inspect("demo-db")["running"] is False


class TestRunUpdateIntegration:
    def test_run_update_persists_job_and_clears_flag(self):
        """API 级：任务落库 + 成功容器清 update_available。"""
        client = MockDockerClient()
        _seed_demo_stack(client)
        with db.tx() as conn:
            for name, image, avail in [
                ("demo-web", "nginx:1.25-alpine", 1),
                ("demo-api", "myapp:2.0", 1),
                ("demo-db", "postgres:16.2", 0),
            ]:
                conn.execute(
                    "INSERT INTO containers(name, image_spec, compose_id, update_available, update_enabled) "
                    "VALUES(?,?,?,?,1)",
                    (name, image, "demo-app", avail),
                )
        out = run_update(client, manual=False)
        assert out["job_id"] > 0
        job = db.query_one("SELECT * FROM jobs WHERE id=?", (out["job_id"],))
        assert job["status"] == "done"
        payload = db.jload(job["result"])
        assert {c["name"]: c["result"] for c in payload["containers"]}["demo-web"] == "updated"
        web_row = db.query_one("SELECT update_available FROM containers WHERE name='demo-web'")
        assert web_row["update_available"] == 0
