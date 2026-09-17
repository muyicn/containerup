"""更新引擎：Compose 组识别 → 依赖图 → 拓扑计划 → 执行状态机 → 自动回滚。

对齐 PRD 第五章（5.2 partial 策略 / 5.3 时机 / 5.4 通知由 notify 层聚合）。

计划语义（继承上游 Tugtainer plan_builder + 项目组扩展）：
- to_update：候选（有更新 + update_enabled + 非 protected/freeze/ignored + 发布延迟到期）
- affected：依赖 to_update 的容器（BFS 反向闭包）——partial 策略下不换镜像，只停止后重启重连
- order：组内拓扑序（先更新被依赖方）；停止按 reversed(order)
- 失败传播：上游失败 → 下游跳过并保持运行，绝不出现"下游新、上游旧"撕裂

执行状态机：pending → updating（→ pruning）→ done/failed；
容器结果：updated / rolled_back / failed / skipped / started(仅受影响恢复)。
"""
import logging
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Optional

from backend import db, notify
from backend.docker import DockerError
from backend.registry import parse_image_spec

logger = logging.getLogger("engine")

# 全局更新互斥：手动触发与后台调度同一时刻仅允许一个更新任务（忙时不排队，立即让行）
_UPDATE_LOCK = threading.Lock()

COMPOSE_PROJECT_LABEL = "com.docker.compose.project"
COMPOSE_SERVICE_LABEL = "com.docker.compose.service"
COMPOSE_DEPENDING_LABEL = "com.docker.compose.depends_on"
CUSTOM_DEPENDING_LABEL = "dev.quenary.tugtainer.depends_on"
PROTECTED_LABEL = "dev.quenary.tugtainer.protected"


def get_compose_id(c: dict[str, Any]) -> Optional[str]:
    labels = c.get("labels") or {}
    proj = labels.get(COMPOSE_PROJECT_LABEL, "")
    return proj or None


def get_service_name(c: dict[str, Any]) -> Optional[str]:
    labels = c.get("labels") or {}
    return labels.get(COMPOSE_SERVICE_LABEL) or None


def get_dependencies(c: dict[str, Any]) -> set[str]:
    """依赖解析：compose service 名（按服务名映射回容器名）+ 自定义容器名标签。"""
    labels = c.get("labels") or {}
    deps: set[str] = set()
    raw = labels.get(CUSTOM_DEPENDING_LABEL, "")
    for part in raw.split(","):
        name = part.strip().split(":")[0].strip()
        if name:
            deps.add(name)
    return deps


def get_compose_service_deps(c: dict[str, Any]) -> set[str]:
    """compose depends_on 声明（service 名，需在组内经 service→name 映射）。"""
    labels = c.get("labels") or {}
    raw = labels.get(COMPOSE_DEPENDING_LABEL, "")
    deps: set[str] = set()
    for part in raw.split(","):
        name = part.strip().split(":")[0].strip()
        if name:
            deps.add(name)
    return deps


@dataclass
class UpdatePlan:
    to_update: set[str] = field(default_factory=set)
    affected: set[str] = field(default_factory=set)
    order: list[str] = field(default_factory=list)
    groups: dict[str, list[str]] = field(default_factory=dict)  # compose_id -> 容器名
    reasons: dict[str, str] = field(default_factory=dict)  # 未入选原因（审计）

    def public(self) -> dict[str, Any]:
        return {
            "to_update": sorted(self.to_update),
            "affected": sorted(self.affected),
            "order": self.order,
            "groups": {k: sorted(v) for k, v in self.groups.items()},
            "reasons": self.reasons,
        }


def build_update_plan(
    containers: list[dict[str, Any]],
    db_map: dict[str, dict[str, Any]],
    candidates: Optional[set[str]] = None,
    global_delay_sec: int = 0,
    merge_wait_sec: int = 0,
) -> UpdatePlan:
    """构建更新计划（PRD 5.2 partial 策略）。

    :param containers: 运行时容器快照（docker.list_containers）
    :param db_map: name -> containers 表行（策略与检测状态）
    :param candidates: 显式候选（手动单容器/手动全量）；None = 自动推导
    :param global_delay_sec: 全局发布延迟基线
    :param merge_wait_sec: 项目合并等待窗口（组内不足则整组挂起）
    """
    plan = UpdatePlan()
    by_name = {c["name"]: c for c in containers}

    # ---- compose 分组 + 服务名映射 ----
    service_map: dict[str, str] = {}  # (compose_id, service) -> container_name
    for c in containers:
        cid = get_compose_id(c)
        svc = get_service_name(c)
        if cid and svc:
            service_map[f"{cid}|{svc}"] = c["name"]
            plan.groups.setdefault(cid, []).append(c["name"])
        else:
            plan.groups.setdefault("", []).append(c["name"])

    # ---- 依赖图（自定义标签 + compose 服务依赖映射到容器名）----
    depends: dict[str, set[str]] = {}
    dependents: dict[str, set[str]] = {}
    for c in containers:
        name = c["name"]
        deps = set(get_dependencies(c))
        cid = get_compose_id(c)
        for svc in get_compose_service_deps(c):
            target = service_map.get(f"{cid}|{svc}") if cid else None
            if target:
                deps.add(target)
        deps &= set(by_name)  # 只保留存在的容器
        depends[name] = deps
        for d in deps:
            dependents.setdefault(d, set()).add(name)

    # ---- 候选过滤 ----
    now = time.time()
    for c in containers:
        name = c["name"]
        row = db_map.get(name)
        labels = c.get("labels") or {}
        if labels.get(PROTECTED_LABEL, "").lower() == "true":
            plan.reasons[name] = "protected"
            continue
        if candidates is not None:
            # 手动模式（单容器/手动全量）：用户明确意志，跳过 freeze/ignored/延迟
            if name in candidates:
                plan.to_update.add(name)
            else:
                plan.reasons[name] = "not-selected"
            continue
        if row and row.get("ignored"):
            plan.reasons[name] = "ignored"
            continue
        if row and row.get("freeze"):
            plan.reasons[name] = "freeze"
            continue
        if not (row and row.get("update_available") and row.get("update_enabled")):
            plan.reasons[name] = "no-update-or-disabled"
            continue
        # 发布延迟（PRD 5.3 层④）
        delay = row.get("delay_update_for")
        eff = delay if delay is not None else global_delay_sec
        changed_at = row.get("remote_changed_at")
        if eff > 0 and changed_at:
            try:
                from datetime import datetime

                ts = datetime.fromisoformat(changed_at).timestamp()
                if now - ts < eff:
                    plan.reasons[name] = f"delay({int(eff - (now - ts))}s left)"
                    continue
            except ValueError:
                pass
        plan.to_update.add(name)

    # ---- 合并等待窗口（PRD 5.3 层③）：组内"有更新但未到期/未发现"→ 整组挂起 ----
    if merge_wait_sec > 0 and candidates is None:
        for cid, members in plan.groups.items():
            if not cid:
                continue
            pending = [m for m in members if m not in plan.to_update and _is_pending(db_map.get(m))]
            if pending and any(m in plan.to_update for m in members):
                for m in plan.groups[cid]:
                    if m in plan.to_update:
                        plan.to_update.discard(m)
                        plan.reasons[m] = "merge-wait"

    # ---- affected：BFS 反向闭包（依赖 to_update 的容器，随计划停止并重启重连）----
    queue: list[str] = list(plan.to_update)
    while queue:
        node = queue.pop(0)
        if node in plan.affected:
            continue
        if node not in plan.to_update:
            plan.affected.add(node)
        for dep in dependents.get(node, set()):
            if dep not in plan.affected:
                queue.append(dep)

    # ---- 拓扑排序（DFS，环容忍：已访问即返回）----
    visited: set[str] = set()
    order: list[str] = []

    def dfs(node: str) -> None:
        if node in visited:
            return
        visited.add(node)
        for dep in depends.get(node, set()):
            dfs(dep)
        order.append(node)

    for n in sorted(plan.to_update | plan.affected):
        dfs(n)
    plan.order = [n for n in order if n in plan.to_update or n in plan.affected]
    return plan


def _is_pending(row: Optional[dict[str, Any]]) -> bool:
    return bool(row and row.get("update_available") and row.get("update_enabled"))


@dataclass
class ContainerJobResult:
    name: str
    result: str  # updated | rolled_back | failed | skipped | started
    errors: list[str] = field(default_factory=list)

    def public(self) -> dict[str, Any]:
        return {"name": self.name, "result": self.result, "errors": self.errors}


class UpdateEngine:
    """执行更新计划：有序停止 → 有序更新（健康门控）→ 自动回滚 → 受影响恢复。"""

    def __init__(self, docker_client: Any, health_wait_sec: int = 0):
        self.docker = docker_client
        self.health_wait_sec = health_wait_sec

    def execute(self, plan: UpdatePlan) -> list[ContainerJobResult]:
        results: list[ContainerJobResult] = []
        snapshot: dict[str, dict[str, Any]] = {}
        for name in plan.to_update | plan.affected:
            if self.docker.exists(name):
                snapshot[name] = self.docker.inspect(name)

        # ---- 停止：拓扑逆序（先停依赖方）；to_update 与 affected 均随计划停止 ----
        for name in reversed(plan.order):
            if (name in plan.to_update or name in plan.affected) and self.docker.exists(name):
                self.docker.stop(name)

        # ---- 更新：拓扑正序，健康门控 + 失败传播 ----
        failed_upstream: set[str] = set()
        for name in plan.order:
            if name in plan.affected and name not in plan.to_update:
                res = ContainerJobResult(name=name, result="started")
                try:
                    # 恢复受影响容器：随计划停止后，上游恢复完成时重启重连
                    if self.docker.exists(name):
                        cur = self.docker.inspect(name)
                        if snapshot[name].get("running") and not cur["running"]:
                            self.docker.start(name)
                except DockerError as e:
                    res.errors.append(str(e))
                results.append(res)
                continue
            if name not in plan.to_update:
                continue
            # 失败传播：依赖的上游已失败 → 跳过，保持停止前的最终恢复动作
            deps = self._deps_of(name, snapshot)
            if any(d in failed_upstream for d in deps):
                res = ContainerJobResult(name=name, result="skipped", errors=["upstream failed"])
                # 跳过的候选按原配置重启（保持运行，PRD 失败传播语义）
                try:
                    if self.docker.exists(name) and snapshot[name]["running"]:
                        self.docker.start(name)
                except DockerError:
                    pass
                results.append(res)
                continue
            res = self._update_one(name, snapshot.get(name))
            if res.result in ("failed", "rolled_back"):
                failed_upstream.add(name)
            results.append(res)
        return results

    def _deps_of(self, name: str, snapshot: dict[str, dict[str, Any]]) -> set[str]:
        deps = get_dependencies(snapshot.get(name, {}))
        cid = get_compose_id(snapshot.get(name, {}))
        svc_deps = get_compose_service_deps(snapshot.get(name, {}))
        out = set(deps)
        for svc in svc_deps:
            # 服务名 → 同组容器名（快照内反查）
            for n2, c2 in snapshot.items():
                if get_compose_id(c2) == cid and get_service_name(c2) == svc:
                    out.add(n2)
        return out

    def _update_one(self, name: str, old: Optional[dict[str, Any]]) -> ContainerJobResult:
        res = ContainerJobResult(name=name, result="failed")
        if not old:
            res.errors.append("snapshot missing")
            return res
        old_image = old["image"]
        old_labels = dict(old.get("labels") or {})
        old_config = dict(old.get("config") or {})
        was_running = old.get("running", False)
        try:
            self.docker.pull(old_image)
        except DockerError as e:
            res.errors.append(f"pull failed: {e}")
            self._restore(name, old)
            return res

        try:
            if self.docker.exists(name):
                self.docker.stop(name)
                self.docker.remove(name)
            self.docker.create(name, old_image, old_config, labels=old_labels)
            if not was_running:
                res.result = "updated"  # 原本停止 → 重建即完成
                return res
            self.docker.start(name)
            healthy = self._wait_healthy(name)
            if healthy:
                res.result = "updated"
                return res
            res.errors.append("healthcheck failed after update")
        except DockerError as e:
            res.errors.append(str(e))

        # ---- 自动回滚：旧镜像 ID 定向重建（真实 Docker 中 pull 后 tag 已指向新镜像，
        #      按 tag 重建等于没回退 —— 必须用旧镜像 ID）----
        old_image_id = (old.get("image_id") or "") or None
        try:
            if self.docker.exists(name):
                self.docker.stop(name)
                self.docker.remove(name)
            self.docker.create(name, old_image, old_config, labels=old_labels, image_id=old_image_id)
            if was_running:
                self.docker.start(name)
                if not self._wait_healthy(name):
                    res.result = "failed"
                    res.errors.append("unhealthy after rollback")
                    return res
            res.result = "rolled_back"
            res.errors.append("rolled back to previous image")
        except DockerError as e:
            res.result = "failed"
            res.errors.append(f"rollback failed: {e}")
        return res

    def _restore(self, name: str, old: dict[str, Any]) -> None:
        try:
            if not self.docker.exists(name) and old.get("running"):
                self.docker.create(name, old["image"], dict(old.get("config") or {}), labels=dict(old.get("labels") or {}))
                self.docker.start(name)
        except DockerError:
            logger.exception("restore failed for %s", name)

    def _wait_healthy(self, name: str) -> bool:
        if self.health_wait_sec <= 0:
            # 无健康检查等待：直接查一次。真实引擎上，镜像内嵌 HEALTHCHECK 的新容器
            # 短暂处于 starting——需要短暂宽限窗口等出 healthy/unhealthy，避免误判回滚
            try:
                h = self.docker.inspect(name).get("health")
            except DockerError:
                return False
            if h in ("healthy", "none"):
                return True
            if h == "starting":
                deadline = time.time() + 10
                while time.time() < deadline:
                    time.sleep(0.5)
                    try:
                        h = self.docker.inspect(name).get("health")
                    except DockerError:
                        return False
                    if h in ("healthy", "none"):
                        return True
                    if h == "unhealthy":
                        return False
            return h == "healthy"
        deadline = time.time() + self.health_wait_sec
        while time.time() < deadline:
            try:
                c = self.docker.inspect(name)
                if c.get("health") == "unhealthy":
                    return False
                if c.get("health") == "healthy":
                    return True
            except DockerError:
                return False
            time.sleep(0.2)
        try:
            return self.docker.inspect(name).get("health") in ("healthy", "none")
        except DockerError:
            return False


def notify_job_result(out: dict[str, Any]) -> None:
    """任务结果聚合通知（PRD 5.4：按项目分组汇总 + 回滚单列）。

    手动 API 与后台调度器共用：自动更新（尤其回滚）同样必须产生通知。
    """
    results = out.get("containers", [])
    if not results:
        return
    rolled = [r for r in results if r["result"] == "rolled_back"]
    failed = [r for r in results if r["result"] == "failed"]
    ok = [r for r in results if r["result"] == "updated"]
    started = [r for r in results if r["result"] == "started"]
    skipped = [r for r in results if r["result"] == "skipped"]
    text = (
        f"容器守望者更新完成：成功 {len(ok)}，回滚 {len(rolled)}，"
        f"失败 {len(failed)}，受影响重启 {len(started)}，跳过 {len(skipped)}"
    )
    payload = {
        "job_id": out.get("job_id"),
        "trigger": out.get("trigger", "manual"),
        "updated": [r["name"] for r in ok],
        "rolled_back": [r["name"] for r in rolled],
        "failed": [r["name"] for r in failed],
        "started": [r["name"] for r in started],
        "skipped": [r["name"] for r in skipped],
    }
    notify.push("job", "update-job", payload)
    notify.deliver(text, payload)
    # 回滚单列高优先级（PRD 5.4）
    for r in rolled:
        notify.push(
            "job",
            r["name"],
            {"event": "rolled_back", "name": r["name"], "errors": r.get("errors", [])},
            dedup_key=f"rollback|{r['name']}|{out.get('job_id')}",
        )


def record_version(name: str, digest: str, image_spec: str, source: str, job_id: Optional[int] = None) -> None:
    """版本台账：追加容器运行过的镜像版本（去重连续相同，每容器保留最近 10 条）。

    source: baseline（首次纳入监控）/ update（更新成功后的新版本）/ rollback（回退前留存）。
    """
    if not digest:
        return
    exists = db.query_one(
        "SELECT 1 FROM container_versions WHERE name=? AND digest=?", (name, digest)
    )
    if exists:
        return
    with db.tx() as conn:
        conn.execute(
            "INSERT INTO container_versions(name, digest, image_spec, source, job_id, created_at) "
            "VALUES(?,?,?,?,?,?)",
            (name, digest, image_spec, source, job_id, db.now_iso()),
        )
        conn.execute(
            "DELETE FROM container_versions WHERE name=? AND id NOT IN "
            "(SELECT id FROM container_versions WHERE name=? ORDER BY id DESC LIMIT 10)",
            (name, name),
        )


def run_rollback(
    docker_client: Any,
    name: str,
    digest: Optional[str] = None,
    health_wait_sec: int = 0,
) -> dict[str, Any]:
    """手动版本回退：把容器重建到台账中的上一个（或指定）版本。

    - 与更新共用全局互斥（_UPDATE_LOCK）；健康门控与自动回滚同语义
    - 回退失败 → 自动恢复到回退前的版本（不把容器留在更糟的状态）
    - 成功后更新 containers.local_digest；远端若有更新，下轮扫描会重新标记可更新
    """
    if not _UPDATE_LOCK.acquire(blocking=False):
        return {"status": "update already running", "name": name}
    try:
        if not docker_client.exists(name):
            raise DockerError(f"no such container: {name}")
        row = db.query_one("SELECT * FROM containers WHERE name=?", (name,))
        if not row:
            raise DockerError(f"container not tracked: {name}")
        current = docker_client.inspect(name)
        current_digest = row.get("local_digest") or current.get("image_id") or ""

        # 回退前先把当前版本存进台账（避免回退后丢失当前位置）
        record_version(name, current_digest, row.get("image_spec", ""), "rollback")

        # 选目标：显式指定 digest → 精确匹配；否则取最近一个非当前版本
        versions = db.query(
            "SELECT * FROM container_versions WHERE name=? ORDER BY id DESC LIMIT 10", (name,)
        )
        target = None
        for v in versions:
            if v["digest"] and v["digest"] != current_digest:
                if digest is None or v["digest"] == digest:
                    target = v
                    break
        if target is None:
            raise DockerError("no previous version available to roll back")

        engine = UpdateEngine(docker_client, health_wait_sec=health_wait_sec)
        try:
            if docker_client.exists(name):
                docker_client.stop(name)
                docker_client.remove(name)
            docker_client.create(
                name, row.get("image_spec", ""), dict(current.get("config") or {}),
                labels=dict(current.get("labels") or {}), image_id=target["digest"],
            )
            docker_client.start(name)
            if not engine._wait_healthy(name):
                raise DockerError("unhealthy after rollback")
        except DockerError as e:
            # 回退失败 → 恢复回退前版本
            try:
                if docker_client.exists(name):
                    docker_client.stop(name)
                    docker_client.remove(name)
                docker_client.create(
                    name, row.get("image_spec", ""), dict(current.get("config") or {}),
                    labels=dict(current.get("labels") or {}), image_id=current.get("image_id"),
                )
                docker_client.start(name)
            except DockerError:
                logger.exception("restore-after-failed-rollback failed for %s", name)
            raise DockerError(f"rollback failed: {e}") from e

        # 成功：local_digest 对齐目标版本；清除“有更新”标记（下轮扫描按远端重算）
        # 并自动关闭该容器的自动更新开关 —— 回退是明确表态，之后由用户决定是否恢复自动
        with db.tx() as conn:
            conn.execute(
                "UPDATE containers SET local_digest=?, update_available=0, update_enabled=0, "
                "updated_at=? WHERE name=?",
                (target["digest"], db.now_iso(), name),
            )
        logger.info("rolled back %s → %s（已自动关闭自动更新）", name, target["digest"][:20])
        return {
            "status": "ok",
            "name": name,
            "from_digest": current_digest,
            "to_digest": target["digest"],
            "update_enabled": False,
            "target": {"digest": target["digest"], "image_spec": target["image_spec"], "created_at": target["created_at"]},
        }
    finally:
        _UPDATE_LOCK.release()


def backfill_versions() -> int:
    """升级迁移：为台账中还没有记录的存量容器补记当前版本为基线。

    保证老库升级后每个被监控容器至少有一个可回退基点。返回补记条数。
    """
    n = 0
    for r in db.query(
        "SELECT name, local_digest, image_spec FROM containers "
        "WHERE local_digest IS NOT NULL AND local_digest != ''"
    ):
        exists = db.query_one(
            "SELECT 1 FROM container_versions WHERE name=? AND digest=?",
            (r["name"], r["local_digest"]),
        )
        if not exists:
            record_version(r["name"], r["local_digest"], r["image_spec"], "baseline")
            n += 1
    return n


def _build_plan(docker_client: Any, manual: bool, names: Optional[list[str]]) -> UpdatePlan:
    """读取容器 + 策略 → 构建更新计划（run_update 与调度器候选判断共用）。"""
    from backend.config import CONFIG

    containers = docker_client.list_containers()
    db_map = {r["name"]: r for r in db.query("SELECT * FROM containers")}
    if names is not None:
        candidates: Optional[set[str]] = set(names)
    elif manual:
        candidates = {n for n, r in db_map.items() if r.get("update_available")}
    else:
        candidates = None
    delay_raw = db.setting_get("delay_update_sec")
    try:
        global_delay = int(delay_raw) if delay_raw else CONFIG.GLOBAL_DELAY_UPDATE_SEC
    except ValueError:
        global_delay = CONFIG.GLOBAL_DELAY_UPDATE_SEC
    return build_update_plan(
        containers,
        db_map,
        candidates=candidates,
        global_delay_sec=global_delay,
        merge_wait_sec=CONFIG.MERGE_WAIT_SEC,
    )


def auto_update_pending(docker_client: Any) -> bool:
    """自动模式下是否存在就绪候选（有可用更新 + 未忽略/冻结 + 延迟与合并窗口已过）。

    后台调度器用它避免在无候选周期里产生空任务。
    """
    return bool(_build_plan(docker_client, manual=False, names=None).to_update)


def run_update(
    docker_client: Any,
    manual: bool = False,
    names: Optional[list[str]] = None,
    health_wait_sec: int = 0,
) -> dict[str, Any]:
    """API 入口：构建计划 → 执行 → 落库任务结果。

    全局互斥（_UPDATE_LOCK）：手动触发与后台调度同一时刻仅允许一个更新任务；
    忙碌时立即返回 update-already-running，不排队（调用方可重试）。
    """
    if not _UPDATE_LOCK.acquire(blocking=False):
        return {"status": "update already running", "job_id": None}
    try:
        plan = _build_plan(docker_client, manual, names)
        engine = UpdateEngine(docker_client, health_wait_sec=health_wait_sec)
        job_id = None
        with db.tx() as conn:
            cur = conn.execute(
                "INSERT INTO jobs(status, started_at) VALUES('updating', ?)",
                (db.now_iso(),),
            )
            job_id = cur.lastrowid
        try:
            results = engine.execute(plan)
            payload = {
                "plan": plan.public(),
                "containers": [r.public() for r in results],
            }
            with db.tx() as conn:
                conn.execute(
                    "UPDATE jobs SET status='done', result=?, finished_at=? WHERE id=?",
                    (db.jdump(payload), db.now_iso(), job_id),
                )
            # 成功容器清 update_available，并把本地摘要对齐到已同步版本
            for r in results:
                if r.result == "updated":
                    with db.tx() as conn:
                        conn.execute(
                            "UPDATE containers SET update_available=0, updated_at=?, "
                            "local_digest=COALESCE(remote_digest, local_digest) WHERE name=?",
                            (db.now_iso(), r.name),
                        )
                    # 版本台账：记录新版本（供手动回退）
                    vrow = db.query_one(
                        "SELECT local_digest, image_spec FROM containers WHERE name=?", (r.name,)
                    )
                    if vrow:
                        record_version(r.name, vrow["local_digest"], vrow["image_spec"], "update", job_id)
            # 任务结果聚合通知（手动/调度共用；调度传入 trigger=auto）
            out = {"job_id": job_id, "trigger": "auto" if not manual and names is None else "manual", **payload}
            notify_job_result(out)
            return out
        except Exception as e:
            with db.tx() as conn:
                conn.execute(
                    "UPDATE jobs SET status='failed', result=?, finished_at=? WHERE id=?",
                    (db.jdump({"error": str(e)}), db.now_iso(), job_id),
                )
            raise
    finally:
        _UPDATE_LOCK.release()


# 防 pyflakes 误报（parse_image_spec 供 API 层引用）
_ = parse_image_spec
