"""认证：bcrypt 密码 + JWT（httpOnly Cookie）+ 登录限流（5 次失败锁 15 分钟）。

对齐 PRD 3.9：
- 密码 bcrypt 哈希落库（users 表）
- JWT access token（HS256），httpOnly + SameSite=Lax cookie
- 登录/初始化接口按 IP 限流：连续 LOGIN_MAX_FAILS 次失败锁定 LOGIN_LOCK_SEC 秒
- 首次部署未设密码时，setup 接口开放初始化；设置后要求授权
"""
import secrets
import threading
import time
from typing import Optional

import bcrypt
import jwt

from backend import db
from backend.config import CONFIG

_FAILS: dict[str, list[float]] = {}
_LOCK = threading.Lock()


# ---------- 密码 ----------

def hash_password(plain: str) -> str:
    return bcrypt.hashpw(plain.encode("utf-8"), bcrypt.gensalt()).decode("utf-8")


def verify_password(plain: str, hashed: str) -> bool:
    try:
        return bcrypt.checkpw(plain.encode("utf-8"), hashed.encode("utf-8"))
    except ValueError:
        return False


def get_user() -> Optional[dict]:
    return db.query_one("SELECT * FROM users ORDER BY id LIMIT 1")


def set_password(username: str, password: str) -> None:
    existing = get_user()
    with db.tx() as conn:
        if existing:
            conn.execute(
                "UPDATE users SET username=?, password_hash=? WHERE id=?",
                (username, hash_password(password), existing["id"]),
            )
        else:
            conn.execute(
                "INSERT INTO users(username, password_hash) VALUES(?,?)",
                (username, hash_password(password)),
            )


# ---------- 登录限流 ----------

def _prune_fails(ip: str, now: float) -> list[float]:
    fails = [t for t in _FAILS.get(ip, []) if now - t < CONFIG.LOGIN_LOCK_SEC]
    _FAILS[ip] = fails
    return fails


def login_locked(ip: str) -> int:
    """返回剩余锁定秒数（0 = 未锁定）。"""
    now = time.time()
    with _LOCK:
        fails = _prune_fails(ip, now)
        if len(fails) >= CONFIG.LOGIN_MAX_FAILS:
            remaining = int(CONFIG.LOGIN_LOCK_SEC - (now - fails[0]))
            return max(remaining, 1)
    return 0


def login_fail(ip: str) -> None:
    now = time.time()
    with _LOCK:
        _FAILS.setdefault(ip, []).append(now)


def login_success(ip: str) -> None:
    with _LOCK:
        _FAILS.pop(ip, None)


# ---------- JWT ----------

def secret_key() -> str:
    """JWT 密钥：环境变量优先，否则生成并持久化到 settings。"""
    if CONFIG.SECRET_KEY:
        return CONFIG.SECRET_KEY
    saved = db.setting_get("jwt_secret")
    if not saved:
        saved = secrets.token_hex(32)
        db.setting_set("jwt_secret", saved)
    return saved


def create_token(username: str) -> str:
    payload = {
        "sub": username,
        "type": "access",
        "iat": int(time.time()),
        "exp": int(time.time()) + CONFIG.ACCESS_TOKEN_MIN * 60,
    }
    return jwt.encode(payload, secret_key(), algorithm="HS256")


def verify_token(token: str) -> bool:
    try:
        payload = jwt.decode(token, secret_key(), algorithms=["HS256"])
        return payload.get("type") == "access"
    except jwt.PyJWTError:
        return False
