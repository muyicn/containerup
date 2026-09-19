"""SQLite 数据访问层：连接管理 + schema 初始化 + 通用查询助手。

同步 sqlite3 + RLock（FastAPI def 路由跑线程池，需线程安全）。
"""
import json
import sqlite3
import threading
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator, Optional

from backend.config import CONFIG

_LOCK = threading.RLock()
_CONN: Optional[sqlite3.Connection] = None


def now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S+00:00")


def _connect() -> sqlite3.Connection:
    conn = sqlite3.connect(CONFIG.DB_PATH, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    return conn


def init_db() -> None:
    """初始化连接与 schema（幂等）。"""
    global _CONN
    with _LOCK:
        Path(CONFIG.DB_PATH).parent.mkdir(parents=True, exist_ok=True)
        if _CONN is None:
            _CONN = _connect()
        _CONN.executescript(
            """
            CREATE TABLE IF NOT EXISTS settings (
                key TEXT PRIMARY KEY, value TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS containers (
                name TEXT PRIMARY KEY,
                image_spec TEXT NOT NULL,
                compose_id TEXT,
                service TEXT,
                mode TEXT NOT NULL DEFAULT 'auto',
                check_enabled INTEGER NOT NULL DEFAULT 1,
                update_enabled INTEGER NOT NULL DEFAULT 0,
                ignored INTEGER NOT NULL DEFAULT 0,
                freeze INTEGER NOT NULL DEFAULT 0,
                local_digest TEXT,
                remote_digest TEXT,
                local_version TEXT,
                remote_version TEXT,
                update_available INTEGER NOT NULL DEFAULT 0,
                newer_tags TEXT NOT NULL DEFAULT '[]',
                last_checked_at TEXT,
                remote_changed_at TEXT,
                delay_update_for INTEGER,
                updated_at TEXT,
                local_image INTEGER NOT NULL DEFAULT 0,
                protected INTEGER NOT NULL DEFAULT 0,
                is_self INTEGER NOT NULL DEFAULT 0
            );
            CREATE TABLE IF NOT EXISTS watches (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                reference TEXT NOT NULL UNIQUE,
                mode TEXT NOT NULL DEFAULT 'auto',
                ignored INTEGER NOT NULL DEFAULT 0,
                remote_digest TEXT,
                baseline_digest TEXT,
                last_checked_at TEXT,
                newer_tags TEXT NOT NULL DEFAULT '[]'
            );
            CREATE TABLE IF NOT EXISTS container_versions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL,
                digest TEXT NOT NULL,
                image_spec TEXT NOT NULL DEFAULT '',
                source TEXT NOT NULL DEFAULT 'update',
                job_id INTEGER,
                version TEXT,
                created_at TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_versions_name ON container_versions(name, id DESC);
            CREATE TABLE IF NOT EXISTS notifications (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                type TEXT NOT NULL,              -- update | new-tag | job
                target TEXT NOT NULL,            -- 容器名或 compose 项目名
                payload TEXT NOT NULL,            -- JSON 明细
                dedup_key TEXT,                  -- 防抖键
                read_at TEXT,
                created_at TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_notif_dedup ON notifications(dedup_key);
            CREATE TABLE IF NOT EXISTS channels (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                kind TEXT NOT NULL,              -- webhook | dingtalk | feishu
                name TEXT NOT NULL,
                url TEXT NOT NULL,
                enabled INTEGER NOT NULL DEFAULT 1
            );
            CREATE TABLE IF NOT EXISTS users (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                username TEXT NOT NULL UNIQUE,
                password_hash TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS scans (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                started_at TEXT NOT NULL,
                finished_at TEXT,
                summary TEXT                     -- JSON
            );
            CREATE TABLE IF NOT EXISTS jobs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                status TEXT NOT NULL,            -- pending|updating|pruning|done|failed
                result TEXT,                     -- JSON 逐容器结果
                started_at TEXT NOT NULL,
                finished_at TEXT
            );
            CREATE TABLE IF NOT EXISTS activity_logs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                ts TEXT NOT NULL,
                level TEXT NOT NULL,             -- info|ok|warn|err
                msg TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS tag_version_cache (
                repo TEXT NOT NULL,              -- GitHub owner/repo
                revision TEXT NOT NULL,          -- 构建 commit sha
                version TEXT NOT NULL DEFAULT '',-- 命中的 tag 名；空串=确认无匹配（防重查）
                updated_at TEXT,
                PRIMARY KEY(repo, revision)
            );
            """
        )
        _CONN.commit()
        # 轻量迁移：旧库 watches 表补 newer_tags 列（CREATE TABLE IF NOT EXISTS 不会变更旧表）
        watch_cols = {r["name"] for r in _CONN.execute("PRAGMA table_info(watches)").fetchall()}
        if "newer_tags" not in watch_cols:
            _CONN.execute("ALTER TABLE watches ADD COLUMN newer_tags TEXT NOT NULL DEFAULT '[]'")
            _CONN.commit()
        # 轻量迁移：旧库 containers 表补 local_image 列（本地导入镜像标记）
        container_cols = {r["name"] for r in _CONN.execute("PRAGMA table_info(containers)").fetchall()}
        if "local_image" not in container_cols:
            _CONN.execute("ALTER TABLE containers ADD COLUMN local_image INTEGER NOT NULL DEFAULT 0")
            _CONN.commit()
        # 轻量迁移：版本号列（镜像 OCI 标签里的版本，展示用）
        for col in ("local_version", "remote_version"):
            if col not in container_cols:
                _CONN.execute(f"ALTER TABLE containers ADD COLUMN {col} TEXT")
                _CONN.commit()
        # 轻量迁移：旧库 containers 表补 is_self 列（标记 ContainerUp 自身容器）
        if "is_self" not in container_cols:
            _CONN.execute("ALTER TABLE containers ADD COLUMN is_self INTEGER NOT NULL DEFAULT 0")
            _CONN.commit()
        # 轻量迁移：版本台账表补 version 列
        version_cols = {r["name"] for r in _CONN.execute("PRAGMA table_info(container_versions)").fetchall()}
        if "version" not in version_cols:
            _CONN.execute("ALTER TABLE container_versions ADD COLUMN version TEXT")
            _CONN.commit()
        # 默认策略项初始化：自动更新联动与默认开启自动更新（开箱即用）
        for k, v in (("auto_update_after_scan", "1"), ("default_update_enabled", "1")):
            _CONN.execute("INSERT OR IGNORE INTO settings(key, value) VALUES(?, ?)", (k, v))
        _CONN.commit()


def get_conn() -> sqlite3.Connection:
    if _CONN is None:
        init_db()
    assert _CONN is not None
    return _CONN


@contextmanager
def tx() -> Iterator[sqlite3.Connection]:
    """事务上下文：自动提交/回滚，内置线程锁。"""
    conn = get_conn()
    with _LOCK:
        try:
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise


def query(sql: str, params: tuple = ()) -> list[dict[str, Any]]:
    """线程安全只读查询，返回 dict 列表。"""
    with _LOCK:
        conn = get_conn()
        return [dict(row) for row in conn.execute(sql, params).fetchall()]


def query_one(sql: str, params: tuple = ()) -> Optional[dict[str, Any]]:
    rows = query(sql, params)
    return rows[0] if rows else None


def setting_get(key: str, default: str = "") -> str:
    row = query_one("SELECT value FROM settings WHERE key=?", (key,))
    return row["value"] if row else default


def setting_set(key: str, value: str) -> None:
    with tx() as conn:
        conn.execute(
            "INSERT INTO settings(key,value) VALUES(?,?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (key, value),
        )


def jdump(obj: Any) -> str:
    return json.dumps(obj, ensure_ascii=False, sort_keys=True)


def log_event(level: str, msg: str) -> None:
    """审计/活动日志：写入 activity_logs 表（UI 活动日志面板展示）。

    轻量自裁剪：仅保留最近 500 条，避免无限增长。
    """
    with tx() as conn:
        conn.execute(
            "INSERT INTO activity_logs(ts, level, msg) VALUES(?,?,?)",
            (now_iso(), level, msg[:500]),
        )
        conn.execute(
            "DELETE FROM activity_logs WHERE id NOT IN "
            "(SELECT id FROM activity_logs ORDER BY id DESC LIMIT 500)"
        )


def jload(raw: Optional[str], default: Any = None) -> Any:
    if not raw:
        return default
    try:
        return json.loads(raw)
    except (ValueError, TypeError):
        return default
