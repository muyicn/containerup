"""容器守望者 L0 端到端验证脚本（真实 uvicorn HTTP 服务 + 真实 socket 调用）。

验证链路（对齐 PRD 4.1 / 5.2 / 验收标准）：
  健康检查 → 初始化/登录 → 容器采集 → 基线扫描 → 模拟发布 → 发现通知(项目聚合)
  → 拓扑更新 → 回滚剧本 → 任务/通知审计

用法：python verify_e2e.py   （进程内启动 uvicorn 于随机端口，结束自动退出）
"""
import http.client
import json
import os
import random
import sys
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

_PORT = random.randint(15000, 25000)
_DB = str(ROOT / ".temp" / "e2e.db")

os.environ["VT_PORT"] = str(_PORT)
os.environ["VT_DEMO_MODE"] = "1"
os.environ["VT_DB_PATH"] = _DB

Path(_DB).unlink(missing_ok=True)

PASS, FAIL = [], []


def check(name: str, cond: bool, detail: str = "") -> None:
    tag = "PASS" if cond else "FAIL"
    (PASS if cond else FAIL).append(name)
    print(f"[{tag}] {name}" + (f"  -> {detail}" if detail else ""))


class Client:
    """极简 HTTP 客户端：真实 socket + 手动 cookie。"""

    def __init__(self, host: str, port: int):
        self.host, self.port = host, port
        self.cookie = ""

    def req(self, method: str, path: str, body: dict | None = None) -> tuple[int, dict | str]:
        conn = http.client.HTTPConnection(self.host, self.port, timeout=15)
        headers = {"Content-Type": "application/json"}
        if self.cookie:
            headers["Cookie"] = self.cookie
        payload = json.dumps(body) if body is not None else None
        conn.request(method, path, body=payload, headers=headers)
        resp = conn.getresponse()
        raw = resp.read().decode("utf-8", errors="replace")
        setc = resp.getheader("set-cookie")
        if setc:
            self.cookie = setc.split(";")[0]
        status = resp.status
        conn.close()
        try:
            return status, json.loads(raw) if raw else {}
        except ValueError:
            return status, raw


def main() -> int:
    import uvicorn

    from backend.config import CONFIG

    CONFIG.DEMO_MODE = True
    CONFIG.DB_PATH = _DB
    CONFIG.PORT = _PORT

    from backend.app import app

    config = uvicorn.Config(app, host="127.0.0.1", port=_PORT, log_level="warning")
    server = uvicorn.Server(config)
    t = threading.Thread(target=server.run, daemon=True)
    t.start()

    c = Client("127.0.0.1", _PORT)
    for _ in range(30):
        try:
            st, body = c.req("GET", "/api/public/health")
            if st == 200:
                break
        except OSError:
            pass
        time.sleep(0.5)
    else:
        print("[FAIL] 服务启动超时")
        return 1
    check("服务健康检查", st == 200 and isinstance(body, dict) and body.get("status") == "ok", str(body))

    # 前端
    st, html = c.req("GET", "/")
    check("前端页面托管", st == 200 and "容器守望者" in str(html))

    # 认证
    st, body = c.req("GET", "/api/auth/check")
    check("初始化检测（演示种子已建管理员）", st == 200 and body.get("need_setup") is False, str(body))
    st, body = c.req("POST", "/api/auth/login", {"username": "admin", "password": "admin123"})
    check("管理员登录（bcrypt + JWT cookie）", st == 200 and c.cookie.startswith("vt_token="))
    st, body = c.req("POST", "/api/auth/login", {"username": "admin", "password": "wrong-pwd"})
    check("错误密码拒绝", st == 401)

    # 基线扫描（容器采集入库发生在首次扫描的 _sync_containers）
    st, body = c.req("POST", "/api/scan")
    check("基线扫描（首巡不告警，3 容器入库）", st == 200 and body.get("events") == 0 and body.get("checked") == 3, str(body))

    st, body = c.req("GET", "/api/containers")
    check("容器采集（compose 三容器入库）", st == 200 and len(body) == 3, ", ".join(x["name"] for x in body))
    web = next((x for x in body if x["name"] == "demo-web"), {})
    check("compose 项目识别", web.get("compose_id") == "demo-app")

    # 模拟远端发布 → 发现
    st, body = c.req("POST", "/api/demo/release", {"spec": "nginx:1.25-alpine"})
    check("模拟发布 nginx 新版本", st == 200)
    st, body = c.req("POST", "/api/scan")
    check("二扫发现更新（项目聚合通知）", st == 200 and body.get("events") == 1, str(body))
    st, body = c.req("GET", "/api/summary")
    check("仪表盘统计（1 项有更新）", st == 200 and body.get("update_available") == 1)
    st, body = c.req("GET", "/api/notifications")
    types = [f"{n['type']}@{n['target']}" for n in body]
    check("通知中心（demo-app 聚合通知）", st == 200 and any(t == "update@demo-app" for t in types), " | ".join(types))

    # 拓扑更新（PRD 5.2：web 依赖 api → api 先更新）
    st, body = c.req("POST", "/api/update?manual=true")
    results = {x["name"]: x["result"] for x in body.get("containers", [])}
    check("全量更新（web/api 成功、db 不动）", results.get("demo-web") == "updated" and "demo-db" not in results, str(results))
    st, body = c.req("GET", "/api/jobs")
    check("任务落库审计", st == 200 and body and body[0]["status"] == "done")

    # 回滚剧本（PRD 3.2⑤）
    c.req("POST", "/api/demo/release", {"spec": "postgres:16.2"})
    c.req("POST", "/api/scan")
    st, body = c.req("POST", "/api/demo/simulate-failure", {"name": "demo-db"})
    check("预设 db 启动失败剧本", st == 200)
    st, body = c.req("POST", "/api/containers/demo-db/update")
    rb = {x["name"]: x["result"] for x in body.get("containers", [])}
    check("健康门控失败 → 自动回滚", rb.get("demo-db") == "rolled_back", str(rb))
    st, body = c.req("GET", "/api/notifications")
    rb_notif = [n for n in body if n["payload"].get("event") == "rolled_back" and n["target"] == "demo-db"]
    check("回滚单列通知", bool(rb_notif))

    # 策略与监控
    st, body = c.req("PUT", "/api/containers/demo-db/mode", {"mode": "pin-watch"})
    check("检测模式切换", st == 200 and body.get("mode") == "pin-watch")
    st, body = c.req("PUT", "/api/containers/demo-db/ignored", {"value": True})
    check("忽略策略", st == 200)
    st, body = c.req("POST", "/api/watches", {"reference": "nginx:1.25-alpine"})
    check("远端监控添加", st == 200)

    # 登录限流（连续失败锁定）
    locked = False
    for i in range(7):
        st, _ = c.req("POST", "/api/auth/login", {"username": "admin", "password": "x" * 20})
        if st == 429:
            locked = True
            break
    check("登录限流（失败锁定）", locked)

    print(f"\n===== 验证结果：{len(PASS)} 通过 / {len(FAIL)} 失败 =====")
    if FAIL:
        for f in FAIL:
            print(f"  失败项: {f}")
    server.should_exit = True
    t.join(timeout=5)
    return 0 if not FAIL else 1


if __name__ == "__main__":
    sys.exit(main())
