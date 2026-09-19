"""通知中心：两级通知（update 强提醒 / new-tag 弱提醒）+ 防抖 + 自动已读 + 渠道投递。

对齐 PRD 3.4 / 5.4：
- 同摘要常规扫描仅通知一次（dedup_key 去重；强制扫描 force 重播）
- 每个新 tag 仅通知一次（tag 级 dedup）
- 目标达成自动已读：本地已同步新摘要 / 已运行通知指向的版本 → 未读转已读
- 已读裁剪至最近 N 条；未读永不自动删除
- 渠道：站内 + webhook/dingtalk/feishu（简单 JSON POST）
"""
import logging
import threading
from typing import Any, Optional

import httpx

from backend import db
from backend.config import CONFIG

logger = logging.getLogger("notify")


def push(
    ntype: str,            # update | new-tag | job
    target: str,           # 容器名或 compose 项目名
    payload: dict[str, Any],
    dedup_key: Optional[str] = None,
    force: bool = False,
) -> Optional[int]:
    """写入站内通知。命中防抖键且非强制 → 跳过。返回通知 id 或 None。"""
    if dedup_key and not force:
        exists = db.query_one(
            "SELECT id FROM notifications WHERE dedup_key=?", (dedup_key,)
        )
        if exists:
            return None
    with db.tx() as conn:
        cur = conn.execute(
            "INSERT INTO notifications(type, target, payload, dedup_key, created_at) "
            "VALUES(?,?,?,?,?)",
            (ntype, target, db.jdump(payload), dedup_key, db.now_iso()),
        )
        nid = cur.lastrowid
    return nid


def digest_dedup_key(target: str, new_digest: str) -> str:
    return f"update|{target}|{new_digest}"


def tag_dedup_key(target: str, tag: str) -> str:
    return f"newtag|{target}|{tag}"


def auto_mark_read(containers_map: dict[str, dict[str, Any]]) -> int:
    """目标达成自动已读（PRD 3.4）。

    containers_map: name -> 容器当前 DB 行（含 local_digest / image_spec）。
    - update 通知（单容器或项目聚合）：本地摘要已同步到通知记录的新摘要 → 已读；
      聚合通知需全部 services 均已解决才标记（防漏看）。
    - new-tag 通知：本地运行引用的 tag >= 通知指向的版本 → 已读。
    返回置为已读的条数。
    """
    marked = 0
    unread = db.query("SELECT * FROM notifications WHERE read_at IS NULL")
    for n in unread:
        payload = db.jload(n["payload"], default={})
        target = n["target"]
        row = containers_map.get(target)
        if not row and not payload.get("services"):
            continue  # 聚合通知按 services 逐个核对；单容器通知无行则跳过
        if n["type"] == "update":
            services = payload.get("services") or [
                {"target": target, "new_digest": payload.get("new_digest")}
            ]
            resolved = []
            for s in services:
                srow = containers_map.get(s.get("target", ""))
                resolved.append(
                    bool(srow)
                    and s.get("new_digest")
                    and srow.get("local_digest") == s.get("new_digest")
                )
            if services and all(resolved):
                if _mark_read(n["id"]):
                    marked += 1
        elif n["type"] == "new-tag":
            services = payload.get("services") or [{"target": target, "tag": payload.get("tag")}]
            from backend.registry import is_version_tag, _version_key

            resolved = []
            for s in services:
                srow = containers_map.get(s.get("target", ""))
                cur_spec = (srow or {}).get("image_spec", "")
                cur_tag = cur_spec.rsplit(":", 1)[-1] if ":" in cur_spec else "latest"
                new_tag = s.get("tag", "")
                resolved.append(
                    bool(srow)
                    and new_tag
                    and is_version_tag(new_tag)
                    and is_version_tag(cur_tag)
                    and _version_key(cur_tag) >= _version_key(new_tag)
                )
            if services and all(resolved):
                if _mark_read(n["id"]):
                    marked += 1
    return marked


def _mark_read(nid: int) -> bool:
    with db.tx() as conn:
        cur = conn.execute(
            "UPDATE notifications SET read_at=? WHERE id=? AND read_at IS NULL",
            (db.now_iso(), nid),
        )
        return cur.rowcount > 0


def prune_read(keep: Optional[int] = None) -> int:
    """已读通知裁剪至最近 keep 条。"""
    keep = keep or CONFIG.NOTIFICATION_KEEP_READ
    rows = db.query(
        "SELECT id FROM notifications WHERE read_at IS NOT NULL ORDER BY id DESC"
    )
    stale = [r["id"] for r in rows[int(keep):]]
    if stale:
        with db.tx() as conn:
            conn.executemany(
                "DELETE FROM notifications WHERE id=?", [(i,) for i in stale]
            )
    return len(stale)


# ---------- 渠道投递 ----------

def _logo_base_url() -> str:
    """应用外部可访问地址（settings.public_base_url），用于企微通知内嵌 logo。"""
    raw = db.setting_get("public_base_url").strip().rstrip("/")
    return raw


def _wecom_payload(text: str) -> dict[str, Any]:
    """企微消息体：配置了应用访问地址 → markdown_v2（内嵌应用 logo 图片，
    需客户端 >= 4.1.36）；否则回退旧版 markdown（盾牌 emoji 标题）。"""
    base = _logo_base_url()
    if base:
        content = (
            f"![logo]({base}/api/public/logo.png)\n"
            "## 容器守望者通知\n"
            f"> {text}\n"
        )
        return {"msgtype": "markdown_v2", "markdown_v2": {"content": content}}
    return {
        "msgtype": "markdown",
        "markdown": {"content": f"## 🛡️ 容器守望者通知\n> {text}\n"},
    }


def _deliver_channel(ch: dict[str, Any], text: str, summary: dict[str, Any]) -> bool:
    try:
        if ch["kind"] == "dingtalk":
            body = {"msgtype": "text", "text": {"content": text}}
        elif ch["kind"] == "feishu":
            body = {"msg_type": "text", "content": {"text": text}}
        elif ch["kind"] == "wecom":
            # 企业微信机器人 webhook：markdown 消息（text 不会被 @ 提醒，markdown 展示更佳）
            body = _wecom_payload(text)
        else:  # 通用 webhook：结构化 JSON
            body = {"text": text, "summary": summary}
        with httpx.Client(timeout=CONFIG.CHANNEL_TIMEOUT_SEC) as client:
            resp = client.post(ch["url"], json=body)
            if resp.status_code < 300:
                try:
                    data = resp.json()
                    if ch["kind"] in ("wecom", "dingtalk") and data.get("errcode", 0) != 0:
                        logger.warning("%s delivery rejected: %s", ch["kind"], data)
                        return False
                    if ch["kind"] == "feishu" and (data.get("code", 0) != 0 or data.get("StatusCode", 0) != 0):
                        logger.warning("feishu delivery rejected: %s", data)
                        return False
                except ValueError:
                    pass
                return True
            return False
    except (httpx.HTTPError, ValueError):
        logger.exception("channel %s delivery failed", ch.get("name"))
        return False


def deliver(text: str, summary: dict[str, Any]) -> dict[str, Any]:
    """向所有启用渠道投递文本。返回逐渠道结果。"""
    channels = db.query("SELECT * FROM channels WHERE enabled=1")
    out = {}
    for ch in channels:
        out[ch["name"]] = _deliver_channel(ch, text, summary)
    return out


def mark_all_read() -> int:
    with db.tx() as conn:
        cur = conn.execute(
            "UPDATE notifications SET read_at=? WHERE read_at IS NULL", (db.now_iso(),)
        )
        return cur.rowcount


def clear_read() -> int:
    with db.tx() as conn:
        cur = conn.execute("DELETE FROM notifications WHERE read_at IS NOT NULL")
        return cur.rowcount


def test_channel(url: str, kind: str) -> bool:
    return _deliver_channel({"kind": kind, "url": url}, "容器守望者测试通知", {"test": True})
