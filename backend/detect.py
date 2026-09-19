"""扫描编排：采集容器 → 并发双模式检测 → 两级通知（防抖/聚合/自动已读）→ 落库。

对齐 PRD 3.1 / 3.4 / 4.1：
- 首检基线：本地无摘要记录时只建基线不告警（继承 Vigil 语义）；
  但容器运行镜像已落后于上游时（repo_digest ≠ remote）首检即告警——与飞牛/Docker UI 直觉一致
- update 强提醒（摘要变化，同摘要一次）+ new-tag 弱提醒（Pin-Watch 更高版本 tag，每 tag 一次）
- 发现通知按 compose 项目聚合为一条（PRD 5.4）；独立容器单独一条
- 扫描互斥：同一时刻仅允许一次扫描；容器间 registry 检测并发执行（线程池）
"""
import hashlib
import logging
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Optional

from backend import db, notify
from backend.config import CONFIG
from backend.engine import (
    COMPOSE_PROJECT_LABEL,
    COMPOSE_SERVICE_LABEL,
    PROTECTED_LABEL,
    get_compose_id,
    get_service_name,
)
from backend.registry import (
    RegistryClient,
    RegistryResult,
    detect_mode,
    parse_image_spec,
    plausible_version,
    version_by_digest,
)

logger = logging.getLogger("detect")

_SCAN_LOCK = threading.Lock()


class StaticRegistrySource:
    """静态注入的 registry 检测源（演示模式/测试用）。

    specs: {image_spec: {"digest": str, "tags": [str]}}
    fallback=True 时未知规格合成确定性摘要（演示模式：任意 watch 可建基线）。
    """

    def __init__(self, specs: dict[str, dict[str, Any]], fallback: bool = False):
        self.specs = specs
        self.fallback = fallback

    def check(self, spec: str, local_digest: Optional[str], mode: str) -> RegistryResult:
        info = self.specs.get(spec)
        if info is None and self.fallback:
            from backend.docker import _fake_digest

            _, _, tag = parse_image_spec(spec)
            info = {"digest": _fake_digest(spec), "tags": [tag]}
        if not info:
            raise RuntimeError(f"mock registry: unknown spec {spec}")
        digest = info["digest"]
        tags = list(info.get("tags", []))
        from backend.registry import newer_version_tags

        _, _, tag = parse_image_spec(spec)
        return RegistryResult(digest=digest, newer_tags=newer_version_tags(tag, tags))

    def remote_version(self, spec: str) -> str:
        """Mock：specs 可带 version 字段（无则空串）。"""
        return str((self.specs.get(spec) or {}).get("version") or "")

    def close(self) -> None:  # 与 RegistryClient 接口对齐（demo 无资源需释放）
        pass


def make_registry_client() -> Any:
    """演示模式 → 静态源（未知规格 fallback 建基线）；否则真实 HTTP 客户端。"""
    if CONFIG.DEMO_MODE:
        from backend.demo import DEMO_REGISTRY_SPECS

        return StaticRegistrySource(DEMO_REGISTRY_SPECS, fallback=True)
    return RegistryClient()


def _sync_containers(docker_client: Any) -> dict[str, dict[str, Any]]:
    """容器采集 upsert：运行时快照 → containers 表行（保留策略列）。"""
    runtime = docker_client.list_containers()
    runtime_names = set()
    for c in runtime:
        name = c["name"]
        runtime_names.add(name)
        labels = c.get("labels") or {}
        compose_id = get_compose_id(c) or ""
        service = get_service_name(c) or ""
        protected = 1 if labels.get(PROTECTED_LABEL, "").lower() == "true" else 0
        image_spec = c.get("image", "")
        # 容器 Config.Image 可能退化为镜像 ID 或 tag@digest 引用（回退定向重建后）；
        # 检测必须用 tag 引用 → 保留 DB 里原有 image_spec，不用这类引用覆盖
        if image_spec.startswith("sha256:") or "@" in image_spec:
            prev = db.query_one("SELECT image_spec FROM containers WHERE name=?", (name,))
            if prev and prev["image_spec"] and not prev["image_spec"].startswith("sha256:"):
                image_spec = prev["image_spec"]
            elif "@" in image_spec:
                image_spec = image_spec.split("@")[0]
        # 更新对比口径：manifest 摘要（pull 时记录的 RepoDigests），与 registry 检测同口径；
        # 本地构建/导入镜像无 RepoDigests → 置空（首巡仅建基线，不做 digest 对比）
        repo_digest = c.get("repo_digest") or ""
        # 镜像版本号（构建时写入的 OCI 标签，随容器 Labels 透出）
        image_version = c.get("image_version") or ""
        with db.tx() as conn:
            conn.execute(
                """
                INSERT INTO containers(name, image_spec, compose_id, service, mode,
                    check_enabled, update_enabled, protected, local_digest, local_version)
                VALUES(?,?,?,?,?,1,0,?,?,?)
                ON CONFLICT(name) DO UPDATE SET
                    image_spec=excluded.image_spec,
                    compose_id=excluded.compose_id,
                    service=excluded.service,
                    protected=excluded.protected,
                    local_digest=excluded.local_digest,
                    local_version=excluded.local_version
                """,
                (name, image_spec, compose_id, service, "auto", protected,
                 repo_digest or None, image_version or None),
            )
    # 宿主机已物理移除（docker rm / compose down）的容器：从监控台账中同步清理，杜绝幽灵容器
    rows = db.query("SELECT name FROM containers")
    for r in rows:
        if r["name"] not in runtime_names:
            with db.tx() as conn:
                conn.execute("DELETE FROM containers WHERE name=?", (r["name"],))
    return {c["name"]: c for c in runtime}


def _check_target(
    client: Any,
    target: str,
    image_spec: str,
    row: dict[str, Any],
    mode_override: str,
    local_digest: Optional[str],
    force: bool,
) -> dict[str, Any]:
    """检查单个目标，返回事件明细。不直接发通知（由调用方聚合）。

    Vigil 语义：首巡把现存 tag 记为已见基线不告警；
    后续仅对"基线之外新出现"的更高版本 tag 发 new-tag（每 tag 一次）；
    强制扫描无视基线与去重重播。
    """
    _, _, tag = parse_image_spec(image_spec)
    mode = detect_mode(tag, mode_override)
    events: list[dict[str, Any]] = []
    new_digest: Optional[str] = None
    remote_version: str = ""
    # 本地版本号（镜像 OCI 标签，_sync_containers 已写入 DB 行）
    local_version: str = row.get("local_version") or ""
    # 版本号补齐：标签值不像版本号（空/main 这类分支名）时，用 manifest digest
    # 匹配 Docker Hub tags 列表反解真实版本号（如 f4133b472867 ≡ v0.7.21）。
    # 持久缓存下首轮后零外呼；查到后写回 DB 供前端展示与台账记录。
    if not plausible_version(local_version) and local_digest:
        tv = version_by_digest(image_spec, local_digest)
        if tv and tv != local_version:
            local_version = tv
            with db.tx() as conn:
                conn.execute(
                    "UPDATE containers SET local_version=? WHERE name=?", (tv, target)
                )
    # 首巡判定：从未成功记录过远端摘要 → 本次为基线巡检（不告警）
    first_seen = not row.get("remote_digest")

    result = client.check(image_spec, local_digest, mode)
    # digest-only 更新判定不受首巡抑制：local(repo_digest) 与 remote 不同即代表
    # 容器运行镜像落后于上游 —— 与飞牛/Docker UI 的直觉语义一致（曾因首巡基线吞掉落后状态）
    if local_digest and result.digest and result.digest != local_digest:
        new_digest = result.digest
        # 版本号（展示用）：远端摘要变化时拉取；摘要未变但版本号缺失（上次拉取失败/
        # 旧版存量数据为空）时重试拉取——拉到后走缓存复用，不重复请求
        if row.get("remote_digest") != result.digest or not row.get("remote_version"):
            fetcher = getattr(client, "remote_version", None)
            if callable(fetcher):
                try:
                    remote_version = fetcher(image_spec)
                except Exception:
                    remote_version = ""
            else:
                remote_version = ""
        else:
            remote_version = row.get("remote_version") or ""
        # config 标签没有像样的版本号（空/main 等）→ tags digest 匹配兜底
        # （数据源 registry 本身：远端 latest 摘要 ≡ 某个版本号 tag 摘要）
        if not plausible_version(remote_version):
            tv = version_by_digest(image_spec, result.digest)
            if tv:
                remote_version = tv
        events.append(
            {
                "type": "update",
                "target": target,
                "payload": {
                    "image": image_spec,
                    "old_digest": local_digest,
                    "new_digest": result.digest,
                },
                "dedup": notify.digest_dedup_key(target, result.digest),
            }
        )

    # new-tag：基线（已见集合）之外新出现的更高版本 tag
    seen: set[str] = set(db.jload(row.get("newer_tags"), []))
    if force:
        alert_tags = list(result.newer_tags)
    elif first_seen:
        alert_tags = []
    else:
        alert_tags = [t for t in result.newer_tags if t not in seen]
    for t in alert_tags:
        events.append(
            {
                "type": "new-tag",
                "target": target,
                "payload": {"image": image_spec, "tag": t,
                            "new_digest": new_digest or ""},
                "dedup": notify.tag_dedup_key(target, t),
            }
        )

    # 更新 DB：已见集合累积合并；首巡对齐基线（local = remote）
    merged_seen = sorted(seen | set(result.newer_tags))
    changed_now = db.now_iso()
    with db.tx() as conn:
        if new_digest:
            # 检测到更新：记远端摘要与版本号、标记可更新；local_digest 保持容器实际值（_sync_containers 已写入）
            conn.execute(
                "UPDATE containers SET remote_digest=?, remote_version=?, update_available=1, local_image=0, "
                "remote_changed_at=COALESCE(remote_changed_at,?), last_checked_at=?, newer_tags=? "
                "WHERE name=?",
                (result.digest, remote_version, changed_now, changed_now, db.jdump(merged_seen), target),
            )
        elif first_seen:
            # 首巡未检测到更新（local == remote 或 local 为空）：
            # - local_digest 已由 _sync_containers 写入容器实际 RepoDigests，不覆盖；
            # - 仅当 local 为空（本地构建镜像无 RepoDigests）时用远端摘要填充，启用后续 304 优化
            conn.execute(
                "UPDATE containers SET "
                "local_digest=CASE WHEN local_digest IS NULL OR local_digest='' "
                "THEN ? ELSE local_digest END, "
                "remote_digest=?, local_image=0, last_checked_at=?, newer_tags=? WHERE name=?",
                (result.digest, result.digest, changed_now, db.jdump(merged_seen), target),
            )
        else:
            # 非首巡且无新摘要：容器与远端一致 → 清除更新标记与远端版本；刷新远端摘要供下次 304 优化
            up_to_date = bool(local_digest and result.digest and local_digest == result.digest)
            sets = "last_checked_at=?, newer_tags=?, remote_digest=?, local_image=0"
            params: list = [changed_now, db.jdump(merged_seen), result.digest]
            if up_to_date:
                sets += ", update_available=0, remote_version=''"
            params.append(target)
            conn.execute(f"UPDATE containers SET {sets} WHERE name=?", params)

        # 基线巡检与版本台账记录（供手动回退）
        if first_seen and result.digest:
            from backend.engine import record_version

            record_version(target, result.digest, image_spec, "baseline",
                           version=local_version or "")
        elif new_digest and result.digest:
            # 更新发现：台账预记远端目标版本（实际入库在更新成功后）
            from backend.engine import record_version

            record_version(target, result.digest, image_spec, "update",
                           version=remote_version or "")
    return {"events": events, "new_digest": new_digest, "newer_tags": merged_seen}


def _emit_events(events: list[dict[str, Any]], containers_map: dict[str, dict[str, Any]], force: bool) -> int:
    """两级通知 + 项目聚合（PRD 5.4）。返回实际产生的新通知数。

    聚合粒度 = (compose 项目, 通知级别)：update 与 new-tag 分开聚合，
    保证"可选更新"弱提醒不被强提醒吞没；独立容器逐条发。
    """
    if not events:
        return 0
    pushed = 0
    # 按 (项目, 级别) 聚合
    buckets: dict[str, list[dict[str, Any]]] = {}
    for ev in events:
        row = containers_map.get(ev["target"], {})
        proj = row.get("compose_id") or ""
        buckets.setdefault(f"{proj}|{ev['type']}", []).append(ev)
    for key, evs in buckets.items():
        proj, _, ntype = key.rpartition("|")
        if not proj:
            # 独立容器：逐条发
            for ev in evs:
                if notify.push(ev["type"], ev["target"], ev["payload"], dedup_key=ev["dedup"], force=force):
                    pushed += 1
            continue
        # 项目聚合通知
        batch_key = "|".join(sorted(e["dedup"] for e in evs))
        agg_key = "agg|" + ntype + "|" + hashlib.sha256(batch_key.encode()).hexdigest()[:16]
        payload = {
            "project": proj,
            "services": [
                {"target": e["target"], "type": e["type"], "image": e["payload"].get("image", ""),
                 "tag": e["payload"].get("tag", ""), "new_digest": e["payload"].get("new_digest")}
                for e in evs
            ],
        }
        if notify.push(ntype, proj, payload, dedup_key=agg_key, force=force):
            pushed += 1
    return pushed


def scan(docker_client: Any, force: bool = False) -> dict[str, Any]:
    """全量扫描：容器采集 → 逐容器检测 → 通知（防抖/聚合）→ 自动已读 → 裁剪。"""
    if not _SCAN_LOCK.acquire(blocking=False):
        return {"status": "scan already running"}
    try:
        started = time.time()
        with db.tx() as conn:
            conn.execute("INSERT INTO scans(started_at) VALUES(?)", (db.now_iso(),))
        containers_map = _sync_containers(docker_client)
        client = make_registry_client()
        try:
            rows = db.query("SELECT * FROM containers")
            all_events: list[dict[str, Any]] = []
            checked = errors = 0
            error_details: list[dict[str, str]] = []
            targets = [r for r in rows if r["check_enabled"] and not r["ignored"]]

            def _do(row: dict[str, Any]) -> tuple[Optional[dict[str, Any]], Optional[tuple[str, str]]]:
                try:
                    out = _check_target(
                        client,
                        row["name"],
                        row["image_spec"],
                        row,
                        row["mode"],
                        row["local_digest"],
                        force,
                    )
                    return out, None
                except Exception as e:  # 单容器失败不阻断扫描
                    msg = str(e)
                    # 本地导入/构建镜像（无 RepoDigests）在公共 registry 不存在（401/404）
                    # → 非错误：标记 local_image，跳过更新检测（镜像推送后自动恢复跟踪）
                    if not row.get("local_digest") and ("returned 401" in msg or "returned 404" in msg):
                        logger.info("local image %s: not found on registry, skip (%s)", row["name"], msg)
                        return None, ("__local__", row["name"])
                    logger.warning("check failed for %s: %s", row["name"], e)
                    return None, (row["name"], msg[:200])

            # 并发检测：瓶颈是 registry 网络往返，线程池 8 并发把逐容器串行等待压缩为批次
            workers = max(1, min(8, len(targets)))
            with ThreadPoolExecutor(max_workers=workers) as ex:
                for out, failed in ex.map(_do, targets):
                    if failed:
                        if failed[0] == "__local__":
                            # 本地镜像：置标记、不算错误；远端可用时扫描会自动清除
                            with db.tx() as conn:
                                conn.execute(
                                    "UPDATE containers SET local_image=1, update_available=0 WHERE name=?",
                                    (failed[1],),
                                )
                        else:
                            errors += 1
                            error_details.append({"name": failed[0], "error": failed[1]})
                    else:
                        checked += 1
                        all_events.extend(out["events"])

            # 纯远端监控（watches）：基线/时间线 + 哨兵通知（PRD 3.6 升级）
            # 首检基线不告警；后续摘要变化 → update 通知（同摘要一次）；
            # pin-watch 基线外新版本 tag → new-tag 通知（每 tag 一次）；force 重播
            watch_targets: set[str] = set()
            watch_pushed = 0
            for w in db.query("SELECT * FROM watches"):
                if w["ignored"]:
                    continue
                try:
                    ref = w["reference"]
                    _, _, tag = parse_image_spec(ref)
                    mode = detect_mode(tag, w["mode"])
                    res = client.check(ref, w["remote_digest"], mode)
                    first_check = not w["remote_digest"]
                    changed = bool(w["remote_digest"] and res.digest and res.digest != w["remote_digest"])
                    if changed and notify.push(
                        "update", ref,
                        {"reference": ref, "old_digest": w["remote_digest"],
                         "new_digest": res.digest, "watch": True},
                        dedup_key=f"watch-update|{ref}|{res.digest}", force=force,
                    ):
                        watch_pushed += 1
                        watch_targets.add(ref)
                    seen: set[str] = set(db.jload(w.get("newer_tags"), []))
                    if force:
                        alert_tags = list(res.newer_tags)
                    elif first_check:
                        alert_tags = []
                    else:
                        alert_tags = [t for t in res.newer_tags if t not in seen]
                    for t in alert_tags:
                        if notify.push(
                            "new-tag", ref,
                            {"reference": ref, "tag": t, "watch": True},
                            dedup_key=f"watch-newtag|{ref}|{t}", force=force,
                        ):
                            watch_pushed += 1
                            watch_targets.add(ref)
                    merged_seen = sorted(seen | set(res.newer_tags))
                    with db.tx() as conn:
                        conn.execute(
                            "UPDATE watches SET remote_digest=?, last_checked_at=?, "
                            "baseline_digest=COALESCE(baseline_digest,?), newer_tags=? WHERE id=?",
                            (res.digest, db.now_iso(), res.digest, db.jdump(merged_seen), w["id"]),
                        )
                    checked += 1
                except Exception as e:
                    errors += 1
                    logger.warning("watch check failed for %s: %s", w["reference"], e)
                    error_details.append({"name": w["reference"], "error": str(e)[:200]})
        finally:
            client.close()

        # 事件聚合 + 两级通知（用 DB 行取 compose_id 做项目聚合）
        db_rows_map = {r["name"]: r for r in db.query("SELECT * FROM containers")}
        # 审计日志：逐项记录发现了什么更新（UI 活动日志）
        for ev in all_events:
            p = ev.get("payload") or {}
            if ev["type"] == "update":
                db.log_event(
                    "warn",
                    f"发现更新 {ev['target']}：{p.get('image', '')} "
                    f"{str(p.get('old_digest', ''))[:19]} → {str(p.get('new_digest', ''))[:19]}",
                )
            else:
                db.log_event(
                    "info",
                    f"可选新版本 {ev['target']}：{p.get('image', '')} 出现更高版本 tag {p.get('tag', '')}（需手动升级）",
                )
        pushed = _emit_events(all_events, db_rows_map, force) + watch_pushed

        # 自动已读 + 已读裁剪 + 渠道投递
        containers_rows = {r["name"]: r for r in db.query("SELECT * FROM containers")}
        marked = notify.auto_mark_read(containers_rows)
        notify.prune_read()
        if pushed:
            targets = sorted({e["target"] for e in all_events} | watch_targets)
            text = f"容器守望者：{pushed} 项更新发现（{', '.join(targets)[:80]}）"
            notify.deliver(text, {"count": pushed, "force": force})

        summary = {
            "checked": checked,
            "errors": errors,
            "errors_detail": error_details,  # 失败容器/引用 + 原因，UI 与扫描历史可见
            "events": pushed,  # 实际产生的新通知数（防抖后）
            "auto_read": marked,
            "duration_ms": int((time.time() - started) * 1000),
            "force": force,
        }
        scan_id = db.query_one("SELECT id FROM scans ORDER BY id DESC LIMIT 1")
        if scan_id:
            with db.tx() as conn:
                conn.execute(
                    "UPDATE scans SET finished_at=?, summary=? WHERE id=?",
                    (db.now_iso(), db.jdump(summary), scan_id["id"]),
                )
        return {"status": "ok", **summary}
    finally:
        _SCAN_LOCK.release()
