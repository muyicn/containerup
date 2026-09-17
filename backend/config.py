"""容器守望者全局配置。

环境变量优先级：环境变量 > .env 文件 > 默认值。
敏感值支持 _FILE 形式（Docker secrets）。
"""
import os
from pathlib import Path


def _env_file_load(path: str = ".env") -> None:
    """加载 .env 文件（KEY=VALUE，忽略注释），不覆盖已存在的环境变量。"""
    p = Path(path)
    if not p.is_file():
        return
    for line in p.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        os.environ.setdefault(key.strip(), value.strip())


_env_file_load()


def _get(name: str, default: str = "") -> str:
    return os.environ.get(name, default)


def _get_secret(name: str, default: str = "") -> str:
    """敏感配置：支持 VAR_FILE 形式从文件读取。VAR 与 VAR_FILE 互斥，VAR 优先。"""
    val = _get(name)
    if val:
        return val
    file_path = _get(f"{name}_FILE")
    if file_path:
        try:
            return Path(file_path).read_text(encoding="utf-8").strip()
        except OSError:
            return default
    return default


def _get_int(name: str, default: int) -> int:
    raw = _get(name)
    try:
        return int(raw) if raw else default
    except ValueError:
        return default


def _get_bool(name: str, default: bool = False) -> bool:
    raw = _get(name).strip().lower()
    if not raw:
        return default
    return raw in ("1", "true", "yes", "on")


class Config:
    """运行时配置（模块级直接引用 CONFIG）。"""

    # --- 服务 ---
    HOST: str = _get("VT_HOST", "0.0.0.0")
    PORT: int = _get_int("VT_PORT", 9412)
    DB_PATH: str = _get("VT_DB_PATH", str(Path(__file__).resolve().parent.parent / "vigiltainer.db"))
    FRONTEND_DIR: str = _get("VT_FRONTEND_DIR", str(Path(__file__).resolve().parent.parent / "frontend" / "dist"))
    SECRET_KEY: str = _get_secret("VT_JWT_SECRET")  # 为空则启动时生成并落库
    ACCESS_TOKEN_MIN: int = _get_int("VT_ACCESS_TOKEN_MIN", 120)  # 分钟

    # --- 认证/安全 ---
    DEMO_MODE: bool = _get_bool("VT_DEMO_MODE", False)
    DEMO_SEED: bool = _get_bool("VT_DEMO_SEED", True)  # 演示数据
    LOGIN_MAX_FAILS: int = _get_int("VT_LOGIN_MAX_FAILS", 5)
    LOGIN_LOCK_SEC: int = _get_int("VT_LOGIN_LOCK_SEC", 900)

    # --- 检测 ---
    SCAN_INTERVAL_SEC: int = _get_int("VT_SCAN_INTERVAL_SEC", 0)  # 0 = 不自动扫，手动驱动
    REGISTRY_TIMEOUT_SEC: float = float(_get("VT_REGISTRY_TIMEOUT", "10"))
    REGISTRY_MIRROR: str = _get("VT_REGISTRY_MIRROR", "")  # 例 https://mirror.example.com
    INSECURE_REGISTRIES: str = _get("VT_INSECURE_REGISTRIES", "")  # 逗号分隔 host[:port]
    REGISTRY_REQ_DELAY_SEC: float = float(_get("VT_REGISTRY_REQ_DELAY", "0"))
    GLOBAL_DELAY_UPDATE_SEC: int = _get_int("VT_DELAY_UPDATE_SEC", 0)  # 发布延迟基线
    MERGE_WAIT_SEC: int = _get_int("VT_MERGE_WAIT_SEC", 0)  # 合并等待窗口（0=禁用）
    MAX_CONCURRENT_PROJECTS: int = _get_int("VT_MAX_CONCURRENT_PROJECTS", 2)

    # --- Docker ---
    DOCKER_BIN: str = _get("VT_DOCKER_BIN", "docker")

    # --- 通知 ---
    CHANNEL_TIMEOUT_SEC: float = float(_get("VT_CHANNEL_TIMEOUT", "5"))
    NOTIFICATION_KEEP_READ: int = _get_int("VT_NOTIFICATION_KEEP_READ", 500)
    DEMO_INITIAL_ADMIN_PASSWORD: str = _get("VT_DEMO_ADMIN_PASSWORD", "admin123")


CONFIG = Config()
