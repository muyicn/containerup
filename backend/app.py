"""容器守望者 FastAPI 主应用：REST API + 静态前端托管 + 启动生命周期。

认证：JWT httpOnly Cookie（vt_token）；除 /api/public/* 与 /api/auth/* 外全部要求登录。
演示模式：DEMO_MODE=1 时种子 compose 三容器场景（PRD 5.2 复现）。
"""
import logging
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, Optional

from fastapi import Body, Depends, FastAPI, HTTPException, Request, Response
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel, field_validator

from backend import auth, db, detect, engine, notify, scheduler
from backend.config import CONFIG
from backend.docker import DockerError, MockDockerClient, make_docker_client
from backend.registry import detect_mode, parse_image_spec

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("app")

DOCKER = make_docker_client()
# 引擎降级提示以前端读取 /api/public/health 的 docker_impl 为准（侧边栏徽标），
# 此处不再维护独立的 sock 文件存在性判断（与引擎选择条件不一致，会产生静默降级盲区）


@asynccontextmanager
async def lifespan(app: FastAPI):
    db.init_db()
    if CONFIG.DEMO_MODE and isinstance(DOCKER, MockDockerClient):
        from backend.demo import seed_demo

        seed_demo(DOCKER)
        if CONFIG.DEMO_SEED and not auth.get_user():
            auth.set_password("admin", CONFIG.DEMO_INITIAL_ADMIN_PASSWORD)
    # 后台调度器：scan_interval_sec > 0 时周期扫描 + 自动更新（惰性取 DOCKER，兼容测试替换）
    scheduler.start(lambda: DOCKER)
    # 升级迁移：为存量容器补记版本基线（手动回退功能）
    engine.backfill_versions()
    yield
    scheduler.stop()


app = FastAPI(title="容器守望者", version="1.0.0", lifespan=lifespan)


# ---------- 认证依赖 ----------

def require_auth(request: Request) -> str:
    token = request.cookies.get("vt_token")
    if not token or not auth.verify_token(token):
        raise HTTPException(status_code=401, detail="unauthorized")
    payload_username = "admin"
    return payload_username


# ---------- 模型 ----------

class AuthBody(BaseModel):
    username: str = "admin"
    password: str

    @field_validator("password")
    @classmethod
    def pwd_len(cls, v: str) -> str:
        if len(v) < 6:
            raise ValueError("password must be at least 6 chars")
        return v


class ModeBody(BaseModel):
    mode: str = "auto"

    @field_validator("mode")
    @classmethod
    def mode_ok(cls, v: str) -> str:
        if v not in ("auto", "digest-only", "pin-watch"):
            raise ValueError("invalid mode")
        return v


class FlagBody(BaseModel):
    value: bool


class WatchBody(BaseModel):
    reference: str


class ChannelBody(BaseModel):
    kind: str = "webhook"
    name: str
    url: str

    @field_validator("kind")
    @classmethod
    def kind_ok(cls, v: str) -> str:
        if v not in ("webhook", "dingtalk", "feishu", "wecom"):
            raise ValueError("invalid channel kind")
        return v


class TestChannelBody(BaseModel):
    url: str
    kind: str = "webhook"

    @field_validator("kind")
    @classmethod
    def kind_ok(cls, v: str) -> str:
        if v not in ("webhook", "dingtalk", "feishu", "wecom"):
            raise ValueError("invalid channel kind")
        return v


class TestChannelBody(BaseModel):
    url: str
    kind: str = "webhook"


class SettingsBody(BaseModel):
    settings: dict[str, str]


# ---------- 公开端点 ----------

@app.get("/api/public/health")
def public_health() -> dict[str, Any]:
    return {
        "status": "ok",
        "version": app.version,
        "demo_mode": CONFIG.DEMO_MODE,
        "docker_impl": "mock" if isinstance(DOCKER, MockDockerClient) else "local",
    }


@app.get("/api/public/logo.png", include_in_schema=False)
def public_logo() -> FileResponse:
    """统一品牌 Logo（512x512）：供企微 markdown_v2 通知内嵌，公开免认证。"""
    path = Path(__file__).resolve().parent.parent / "assets" / "logo-avatar-512.png"
    if not path.is_file():
        raise HTTPException(status_code=404, detail="logo not found")
    return FileResponse(path, media_type="image/png", headers={"Cache-Control": "max-age=86400"})


@app.get("/favicon.ico", include_in_schema=False)
def favicon() -> Response:
    # 统一品牌 logo：渐变圆角方块 + 白色盾牌 + 对勾（与前端 Logo.vue / index.html 同源）
    svg = (
        '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 64 64">'
        '<defs><linearGradient id="g" x1="0" y1="0" x2="64" y2="64" gradientUnits="userSpaceOnUse">'
        '<stop offset="0" stop-color="#3b6ef6"/><stop offset="1" stop-color="#1e36af"/>'
        '</linearGradient></defs>'
        '<rect width="64" height="64" rx="14" fill="url(#g)"/>'
        '<path d="M32 12l16 6v12c0 10-7 18-16 22-9-4-16-12-16-22V18l16-6z" fill="#fff"/>'
        '<path d="M25 32.5l5 5 9.5-10.5" fill="none" stroke="#1d40d8" stroke-width="4" '
        'stroke-linecap="round" stroke-linejoin="round"/></svg>'
    )
    return Response(content=svg, media_type="image/svg+xml")


# ---------- 认证 ----------

@app.get("/api/auth/check")
def auth_check(request: Request) -> dict[str, Any]:
    token = request.cookies.get("vt_token")
    ip = request.client.host if request.client else "unknown"
    locked = auth.login_locked(ip)
    return {
        "need_setup": auth.get_user() is None,
        "locked_sec": locked,
        "authenticated": bool(token and auth.verify_token(token)),
    }


@app.post("/api/auth/setup")
def auth_setup(body: AuthBody, response: Response) -> dict[str, Any]:
    if auth.get_user() is not None:
        raise HTTPException(status_code=409, detail="admin already initialized")
    auth.set_password(body.username, body.password)
    token = auth.create_token(body.username)
    response.set_cookie(
        "vt_token", token, httponly=True, samesite="lax", max_age=CONFIG.ACCESS_TOKEN_MIN * 60
    )
    return {"status": "ok"}


@app.post("/api/auth/login")
def auth_login(body: AuthBody, request: Request, response: Response) -> dict[str, Any]:
    ip = request.client.host if request.client else "unknown"
    locked = auth.login_locked(ip)
    if locked:
        raise HTTPException(status_code=429, detail=f"locked, retry in {locked}s")
    user = auth.get_user()
    if not user or not auth.verify_password(body.password, user["password_hash"]):
        auth.login_fail(ip)
        raise HTTPException(status_code=401, detail="invalid credentials")
    auth.login_success(ip)
    token = auth.create_token(user["username"])
    response.set_cookie(
        "vt_token", token, httponly=True, samesite="lax", max_age=CONFIG.ACCESS_TOKEN_MIN * 60
    )
    return {"status": "ok"}


@app.post("/api/auth/logout")
def auth_logout(response: Response) -> dict[str, Any]:
    response.delete_cookie("vt_token")
    return {"status": "ok"}


class PasswordChangeBody(BaseModel):
    old_password: str
    new_password: str

    @field_validator("new_password")
    @classmethod
    def pwd_len(cls, v: str) -> str:
        if len(v) < 6:
            raise ValueError("password must be at least 6 chars")
        return v


@app.put("/api/auth/password")
def auth_change_password(body: PasswordChangeBody, response: Response, request: Request, user: str = Depends(require_auth)) -> dict[str, Any]:
    """修改密码：验证旧密码 → 更新哈希 → 轮换 token。"""
    cur = auth.get_user()
    if not cur or not auth.verify_password(body.old_password, cur["password_hash"]):
        raise HTTPException(status_code=401, detail="旧密码不正确")
    auth.set_password(cur["username"], body.new_password)
    # 旧 token 立即失效：换发新 token
    token = auth.create_token(cur["username"])
    response.set_cookie(
        "vt_token", token, httponly=True, samesite="lax", max_age=CONFIG.ACCESS_TOKEN_MIN * 60
    )
    return {"status": "ok"}


# ---------- 仪表盘 ----------

@app.get("/api/summary")
def summary(user: str = Depends(require_auth)) -> dict[str, Any]:
    rows = db.query("SELECT * FROM containers")
    unread = db.query("SELECT COUNT(*) AS n FROM notifications WHERE read_at IS NULL")[0]["n"]
    latest_job = db.query("SELECT * FROM jobs ORDER BY id DESC LIMIT 1")
    latest_scan = db.query("SELECT * FROM scans ORDER BY id DESC LIMIT 1")
    if latest_scan:
        latest_scan[0]["summary"] = db.jload(latest_scan[0].get("summary"), {})
    if latest_job:
        latest_job[0]["result"] = db.jload(latest_job[0].get("result"), {})
    projects: dict[str, int] = {}
    for r in rows:
        proj = r.get("compose_id") or ""
        projects[proj] = projects.get(proj, 0) + 1
    return {
        "containers": len(rows),
        "up_to_date": sum(1 for r in rows if not r["update_available"] and not r["ignored"]),
        "update_available": sum(1 for r in rows if r["update_available"]),
        "ignored": sum(1 for r in rows if r["ignored"]),
        "unread": unread,
        "projects": projects,
        "docker_impl": "mock" if isinstance(DOCKER, MockDockerClient) else "local",
        "demo_mode": CONFIG.DEMO_MODE,
        "latest_job": latest_job[0] if latest_job else None,
        "latest_scan": latest_scan[0] if latest_scan else None,
    }


# ---------- 容器 ----------

@app.get("/api/containers")
def list_containers(user: str = Depends(require_auth), status: Optional[str] = None) -> list[dict]:
    rows = db.query("SELECT * FROM containers ORDER BY update_available DESC, name")
    if status == "update":
        rows = [r for r in rows if r["update_available"]]
    elif status == "latest":
        rows = [r for r in rows if not r["update_available"] and not r["ignored"]]
    elif status == "ignored":
        rows = [r for r in rows if r["ignored"]]
    for r in rows:
        newer = db.jload(r.get("newer_tags"), [])
        r["newer_tags"] = newer
        # tag 对比：当前运行 tag（来自 image_spec = docker/compose 配置） → 远端可用最新 tag
        try:
            _, _, cur_tag = parse_image_spec(r.get("image_spec", ""))
        except ValueError:
            cur_tag = "-"
        r["cur_tag"] = cur_tag
        r["latest_tag"] = newer[-1] if newer else cur_tag
    return rows


@app.get("/api/containers/{name}")
def container_detail(name: str, user: str = Depends(require_auth)) -> dict[str, Any]:
    row = db.query_one("SELECT * FROM containers WHERE name=?", (name,))
    if not row:
        raise HTTPException(status_code=404, detail="not found")
    row["newer_tags"] = db.jload(row.get("newer_tags"), [])
    try:
        row["runtime"] = DOCKER.inspect(name)
    except Exception as e:
        row["runtime"] = {"error": str(e)}
    return row


@app.put("/api/containers/{name}/mode")
def set_mode(name: str, body: ModeBody, user: str = Depends(require_auth)) -> dict[str, Any]:
    with db.tx() as conn:
        cur = conn.execute("UPDATE containers SET mode=? WHERE name=?", (body.mode, name))
        if cur.rowcount == 0:
            raise HTTPException(status_code=404, detail="not found")
    return {"status": "ok", "mode": body.mode}


@app.put("/api/containers/{name}/ignored")
def set_ignored(name: str, body: FlagBody, user: str = Depends(require_auth)) -> dict[str, Any]:
    with db.tx() as conn:
        cur = conn.execute("UPDATE containers SET ignored=? WHERE name=?", (1 if body.value else 0, name))
        if cur.rowcount == 0:
            raise HTTPException(status_code=404, detail="not found")
    return {"status": "ok", "ignored": body.value}


@app.put("/api/containers/{name}/update_enabled")
def set_update_enabled(name: str, body: FlagBody, user: str = Depends(require_auth)) -> dict[str, Any]:
    with db.tx() as conn:
        cur = conn.execute("UPDATE containers SET update_enabled=? WHERE name=?", (1 if body.value else 0, name))
        if cur.rowcount == 0:
            raise HTTPException(status_code=404, detail="not found")
    return {"status": "ok", "update_enabled": body.value}


@app.post("/api/containers/{name}/update")
def update_container(name: str, user: str = Depends(require_auth)) -> dict[str, Any]:
    row = db.query_one("SELECT * FROM containers WHERE name=?", (name,))
    if not row:
        raise HTTPException(status_code=404, detail="not found")
    return engine.run_update(DOCKER, names=[name], health_wait_sec=_health_wait())


@app.post("/api/update")
def update_all(manual: bool = True, user: str = Depends(require_auth)) -> dict[str, Any]:
    return engine.run_update(DOCKER, manual=manual, health_wait_sec=_health_wait())


@app.get("/api/containers/{name}/versions")
def container_versions(name: str, user: str = Depends(require_auth)) -> dict[str, Any]:
    """版本台账：当前版本 + 历史版本（含建议回退目标）。"""
    row = db.query_one("SELECT local_digest FROM containers WHERE name=?", (name,))
    if not row:
        raise HTTPException(status_code=404, detail="not found")
    versions = db.query(
        "SELECT digest, image_spec, source, job_id, created_at FROM container_versions "
        "WHERE name=? ORDER BY id DESC LIMIT 10",
        (name,),
    )
    current = row["local_digest"] or ""
    target = next((v for v in versions if v["digest"] and v["digest"] != current), None)
    return {
        "name": name,
        "current_digest": current,
        "rollback_target": target,
        "versions": [
            {**v, "is_current": v["digest"] == current} for v in versions
        ],
    }


@app.post("/api/containers/{name}/rollback")
def rollback_container(name: str, body: dict = Body(default={}), user: str = Depends(require_auth)) -> dict[str, Any]:
    """手动回退到台账中的上一个（或指定 digest）版本。"""
    try:
        return engine.run_rollback(DOCKER, name, digest=(body or {}).get("digest"), health_wait_sec=_health_wait())
    except DockerError as e:
        raise HTTPException(status_code=400, detail=str(e))


# ---------- 远端监控 ----------

@app.get("/api/watches")
def list_watches(user: str = Depends(require_auth)) -> list[dict]:
    rows = db.query("SELECT * FROM watches ORDER BY id")
    for r in rows:
        r["newer_tags"] = db.jload(r.get("newer_tags"), [])
        try:
            _, _, tag = parse_image_spec(r["reference"])
            r["effective_mode"] = detect_mode(tag, r["mode"])
        except ValueError:
            r["effective_mode"] = r["mode"]
    return rows


@app.post("/api/watches")
def add_watch(body: WatchBody, user: str = Depends(require_auth)) -> dict[str, Any]:
    try:
        parse_image_spec(body.reference)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    try:
        with db.tx() as conn:
            conn.execute(
                "INSERT INTO watches(reference, mode) VALUES(?, 'auto')", (body.reference,)
            )
    except Exception:
        raise HTTPException(status_code=409, detail="watch already exists")
    return {"status": "ok", "reference": body.reference}


@app.delete("/api/watches/{wid}")
def del_watch(wid: int, user: str = Depends(require_auth)) -> dict[str, Any]:
    with db.tx() as conn:
        cur = conn.execute("DELETE FROM watches WHERE id=?", (wid,))
        if cur.rowcount == 0:
            raise HTTPException(status_code=404, detail="not found")
    return {"status": "ok"}


# ---------- 扫描 ----------

@app.post("/api/scan")
def do_scan(force: bool = False, user: str = Depends(require_auth)) -> dict[str, Any]:
    return detect.scan(DOCKER, force=force)


@app.get("/api/scheduler")
def scheduler_status(user: str = Depends(require_auth)) -> dict[str, Any]:
    """后台调度器状态：启用/间隔/下次执行/最近一轮结果（仪表盘展示）。"""
    return scheduler.status()


@app.get("/api/scans")
def list_scans(user: str = Depends(require_auth)) -> list[dict]:
    rows = db.query("SELECT * FROM scans ORDER BY id DESC LIMIT 20")
    for r in rows:
        r["summary"] = db.jload(r.get("summary"), {})
    return rows


# ---------- 通知 ----------

@app.get("/api/notifications")
def list_notifications(user: str = Depends(require_auth), unread: bool = False) -> list[dict]:
    sql = "SELECT * FROM notifications"
    rows = db.query(sql + (" WHERE read_at IS NULL" if unread else "") + " ORDER BY id DESC LIMIT 200")
    for r in rows:
        r["payload"] = db.jload(r.get("payload"), {})
    return rows


@app.post("/api/notifications/{nid}/read")
def read_one(nid: int, user: str = Depends(require_auth)) -> dict[str, Any]:
    if not notify._mark_read(nid):
        raise HTTPException(status_code=404, detail="not found or already read")
    return {"status": "ok"}


@app.post("/api/notifications/read-all")
def read_all(user: str = Depends(require_auth)) -> dict[str, Any]:
    return {"status": "ok", "marked": notify.mark_all_read()}


@app.post("/api/notifications/clear-read")
def clear_read_ep(user: str = Depends(require_auth)) -> dict[str, Any]:
    return {"status": "ok", "cleared": notify.clear_read()}


# ---------- 渠道 ----------

@app.get("/api/channels")
def list_channels(user: str = Depends(require_auth)) -> list[dict]:
    return db.query("SELECT * FROM channels ORDER BY id")


@app.post("/api/channels")
def add_channel(body: ChannelBody, user: str = Depends(require_auth)) -> dict[str, Any]:
    with db.tx() as conn:
        conn.execute(
            "INSERT INTO channels(kind, name, url) VALUES(?,?,?)",
            (body.kind, body.name, body.url),
        )
    return {"status": "ok"}


@app.delete("/api/channels/{cid}")
def del_channel(cid: int, user: str = Depends(require_auth)) -> dict[str, Any]:
    with db.tx() as conn:
        cur = conn.execute("DELETE FROM channels WHERE id=?", (cid,))
        if cur.rowcount == 0:
            raise HTTPException(status_code=404, detail="not found")
    return {"status": "ok"}


@app.post("/api/channels/test")
def test_channel_ep(body: TestChannelBody, user: str = Depends(require_auth)) -> dict[str, Any]:
    ok = notify.test_channel(body.url, body.kind)
    return {"status": "ok" if ok else "failed", "delivered": ok}


# ---------- 任务 ----------

@app.get("/api/jobs")
def list_jobs(user: str = Depends(require_auth)) -> list[dict]:
    rows = db.query("SELECT * FROM jobs ORDER BY id DESC LIMIT 10")
    for r in rows:
        r["result"] = db.jload(r.get("result"), {})
    return rows


# ---------- 活动日志（后端持久化审计流） ----------

@app.get("/api/logs")
def list_logs(user: str = Depends(require_auth), limit: int = 200) -> list[dict]:
    """活动日志：后端生命周期事件（发现更新/任务执行/回滚等），UI 底部面板展示。"""
    n = max(1, min(500, limit))
    rows = db.query("SELECT * FROM activity_logs ORDER BY id DESC LIMIT ?", (n,))
    return list(reversed(rows))


# ---------- 设置 ----------

_ALLOWED_SETTINGS = {"delay_update_sec", "scan_interval_sec", "registry_mirror", "demo_failure", "public_base_url", "health_wait_sec"}


def _health_wait() -> int:
    """健康门控等待秒数（settings 可配，0=单次快查 + starting 宽限）。"""
    try:
        return max(0, int(db.setting_get("health_wait_sec", "0") or 0))
    except (TypeError, ValueError):
        return 0


@app.get("/api/settings")
def get_settings(user: str = Depends(require_auth)) -> dict[str, Any]:
    rows = db.query("SELECT key, value FROM settings WHERE key IN ('delay_update_sec','scan_interval_sec','registry_mirror','public_base_url','health_wait_sec')")
    return {r["key"]: r["value"] for r in rows}


@app.put("/api/settings")
def put_settings(body: SettingsBody, user: str = Depends(require_auth)) -> dict[str, Any]:
    unknown = set(body.settings) - _ALLOWED_SETTINGS
    if unknown:
        raise HTTPException(status_code=400, detail=f"unknown settings: {sorted(unknown)}")
    for k, v in body.settings.items():
        db.setting_set(k, v)
    return {"status": "ok", "settings": body.settings}


# ---------- 演示控制（仅 DEMO 模式） ----------

@app.post("/api/demo/release")
def demo_release(body: dict, user: str = Depends(require_auth)) -> dict[str, Any]:
    """模拟镜像仓库发布新版本（仅演示模式）：更新静态 registry 摘要。

    每次调用生成唯一新摘要 → 模拟真实仓库的"新镜像 push"，可重复演示完整闭环。
    """
    if not CONFIG.DEMO_MODE:
        raise HTTPException(status_code=403, detail="demo mode only")
    import uuid

    from backend.demo import DEMO_REGISTRY_SPECS

    spec = body.get("spec", "")
    if spec not in DEMO_REGISTRY_SPECS:
        raise HTTPException(status_code=404, detail="unknown demo spec")
    new_digest = "sha256:" + uuid.uuid4().hex + uuid.uuid4().hex
    DEMO_REGISTRY_SPECS[spec]["digest"] = new_digest
    return {"status": "ok", "spec": spec, "new_digest": new_digest}


@app.post("/api/demo/simulate-failure")
def simulate_failure(body: dict, user: str = Depends(require_auth)) -> dict[str, Any]:
    # 仅 Mock 客户端支持该剧本（真 Docker 引擎无法预设启动失败）
    if not isinstance(DOCKER, MockDockerClient):
        raise HTTPException(status_code=403, detail="requires mock docker client")
    name = body.get("name", "")
    if not name or not DOCKER.exists(name):
        raise HTTPException(status_code=404, detail="container not found")
    DOCKER.mark_unhealthy_once(name)
    return {"status": "ok", "message": f"{name} 下一次重建将模拟 unhealthy → 验证自动回滚"}


@app.exception_handler(HTTPException)
def http_exc_handler(request: Request, exc: HTTPException):
    return JSONResponse(status_code=exc.status_code, content={"detail": exc.detail})


# ---------- 静态前端 ----------

@app.get("/")
def index() -> Any:
    path = Path(CONFIG.FRONTEND_DIR) / "index.html"
    if not path.is_file():
        return FileResponse(_fallback_index(), media_type="text/html")
    # 禁止缓存 HTML：确保发新版后浏览器立即拉取新 hash 资源
    return FileResponse(
        path,
        media_type="text/html",
        headers={"Cache-Control": "no-cache, no-store, must-revalidate"},
    )


@app.get("/assets/{file_path:path}")
def static_assets(file_path: str) -> Any:
    """Vite 构建产物静态资源（JS/CSS/字体）。"""
    safe = Path(file_path)
    if ".." in safe.parts:
        raise HTTPException(status_code=404, detail="not found")
    path = Path(CONFIG.FRONTEND_DIR) / "assets" / safe
    if not path.is_file():
        raise HTTPException(status_code=404, detail="not found")
    media = "text/css" if path.suffix == ".css" else "application/javascript"
    if path.suffix in (".svg",):
        media = "image/svg+xml"
    if path.suffix in (".woff2", ".woff"):
        media = "font/" + path.suffix.lstrip(".")
    if path.suffix in (".png", ".jpg", ".ico"):
        media = "image/" + path.suffix.lstrip(".")
    return FileResponse(path, media_type=media)


def _fallback_index() -> Path:
    """前端缺失时的兜底占位页。"""
    p = Path(CONFIG.FRONTEND_DIR) / "_missing.html"
    if not p.exists():
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text("<h1>容器守望者</h1><p>frontend not built</p>", encoding="utf-8")
    return p
