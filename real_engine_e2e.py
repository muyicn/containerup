"""容器守望者 真实 Docker 引擎全场景端到端验证（CI 专用，标准库实现）。

前置环境（由 .github/workflows/real-engine-e2e.yml 准备）：
- runner 真实 Docker 引擎 + docker CLI；本地 insecure registry: 127.0.0.1:5000（registry:2）
- 测试镜像已推送：127.0.0.1:5000/e2e-app:{v1,pin,1.0.0} / 127.0.0.1:5000/e2e-db:v1
- 平台以容器形态运行（host 网络 + 挂载 docker.sock）：http://127.0.0.1:9412
  —— 与用户 NAS 的 compose 部署形态完全一致

验证场景：
 E1  引擎接入（容器内 CLI + sock → docker_impl=local）
 E2  首次初始化 + 登录 + 错误密码拒绝
 E3  真实容器扫描采集（compose label 反推 / protected 标记）
 E4  基线巡检（首扫不告警）
 E5  digest-only 更新发现（上游覆盖 tag 摘要 → update 事件）
 E6  compose 项目聚合通知
 E7  手动单容器更新（真实 停旧→起新 + 摘要对齐 + 版本台账）
 E8  健康门控失败 → 自动回滚（内嵌 HEALTHCHECK 失败镜像）
 E9  手动版本回退（版本列表 → 指定版本回退 → 自动关更新开关）
 E10 pin-watch 新版本 tag 通知（auto 模式数字 tag 判定）
 E11 watch 哨兵（远端摘要变化 → update；新版本 tag → new-tag）
 E12 ignored 策略
 E13 webhook 通知渠道投递
 E14 后台调度器：自动扫描 + 自动更新
"""
import http.client
import json
import os
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer

REG = "127.0.0.1:5000"
HOST = os.environ.get("E2E_API_HOST", "127.0.0.1")
PORT = int(os.environ.get("E2E_API_PORT", "9412"))

PASS, FAIL = [], []


def check(name: str, cond: bool, detail: str = "") -> bool:
    (PASS if cond else FAIL).append(name)
    print(f"[{'PASS' if cond else 'FAIL'}] {name}" + (f"  -> {detail}" if detail else ""), flush=True)
    return bool(cond)


class Client:
    """极简 HTTP 客户端：真实 socket + 手动 cookie。"""

    def __init__(self, host: str, port: int):
        self.host, self.port = host, port
        self.cookie = ""

    def req(self, method: str, path: str, body: dict | None = None) -> tuple[int, dict | str]:
        # 更新类请求含健康门控等待 + 自动回滚 + 回滚后等待，服务端可达 60s+，放宽客户端超时
        conn = http.client.HTTPConnection(self.host, self.port, timeout=90)
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


def sh(cmd: str) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, shell=True, capture_output=True, text=True)


def sh_ok(cmd: str) -> str:
    r = sh(cmd)
    if r.returncode != 0:
        raise RuntimeError(f"cmd failed: {cmd}\nSTDOUT:{r.stdout}\nSTDERR:{r.stderr}")
    return r.stdout.strip()


def dinspect(name: str) -> dict:
    r = sh(f"docker inspect {name}")
    if r.returncode != 0:
        return {}
    return json.loads(r.stdout)[0]


def container_image_id(name: str) -> str:
    return dinspect(name).get("Image", "")


def registry_digest(spec: str) -> str:
    """取本地镜像的 RepoDigest（上游发布后的摘要口径）。"""
    out = sh_ok(f"docker image inspect {spec} --format '{{{{index .RepoDigests 0}}}}'")
    return out.strip()


def push_app(tag: str, mark: str, bad: bool = False) -> None:
    df = "e2e/Dockerfile.app-bad" if bad else "e2e/Dockerfile.app"
    sh_ok(f"docker build -q -t {REG}/e2e-app:{tag} --build-arg MARK={mark} -f {df} e2e/")
    sh_ok(f"docker push -q {REG}/e2e-app:{tag}")


def push_db(tag: str, mark: str) -> None:
    sh_ok(f"docker build -q -t {REG}/e2e-db:{tag} --build-arg MARK={mark} -f e2e/Dockerfile.db e2e/")
    sh_ok(f"docker push -q {REG}/e2e-db:{tag}")


def push_watch(tag: str, mark: str) -> None:
    sh_ok(f"docker build -q -t {REG}/e2e-watch:{tag} --build-arg MARK={mark} -f e2e/Dockerfile.db e2e/")
    sh_ok(f"docker push -q {REG}/e2e-watch:{tag}")


class Api:
    def __init__(self):
        self.c = Client(HOST, PORT)

    def get_container(self, name: str) -> dict:
        st, body = self.c.req("GET", "/api/containers")
        assert st == 200, f"GET /api/containers -> {st}"
        return next((x for x in body if x["name"] == name), {})

    def scan(self, force: bool = False) -> dict:
        st, body = self.c.req("POST", f"/api/scan?force={str(force).lower()}")
        assert st == 200, f"scan -> {st} {body}"
        return body

    def notifs(self) -> list[tuple]:
        st, body = self.c.req("GET", "/api/notifications")
        assert st == 200
        return [(n["type"], n["target"], (n.get("payload") or {}).get("watch", False), n["id"]) for n in body]

    def settings(self, kv: dict) -> None:
        st, body = self.c.req("PUT", "/api/settings", {"settings": kv})
        assert st == 200, f"settings -> {st} {body}"


def start_webhook_sink() -> tuple[HTTPServer, list]:
    """通知接收器：返回 (server, 收到的请求体列表)。"""
    received: list = []

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            length = int(self.headers.get("Content-Length", 0))
            received.append(self.rfile.read(length).decode("utf-8", errors="replace"))
            self.send_response(200)
            self.end_headers()

        def log_message(self, *args):
            pass

    srv = HTTPServer(("127.0.0.1", 9999), Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv, received


def main() -> int:
    api = Api()
    c = api.c

    # ---------- E1 引擎接入 ----------
    st, h = c.req("GET", "/api/public/health")
    ok = check("E1 服务健康", st == 200 and h.get("status") == "ok", str(h))
    ok &= check("E1 真实引擎接入（docker_impl=local）", h.get("docker_impl") == "local" and h.get("demo_mode") is False, str(h))
    if not ok:
        return 1

    # ---------- E2 初始化 + 登录 ----------
    st, body = c.req("GET", "/api/auth/check")
    check("E2 初始化检测（need_setup）", st == 200 and body.get("need_setup") is True, str(body))
    st, _ = c.req("POST", "/api/auth/setup", {"username": "admin", "password": "e2e-admin-123"})
    check("E2 首次初始化管理员", st == 200)
    st, _ = c.req("POST", "/api/auth/login", {"username": "admin", "password": "wrong-pwd"})
    check("E2 错误密码拒绝", st == 401)
    st, _ = c.req("POST", "/api/auth/login", {"username": "admin", "password": "e2e-admin-123"})
    check("E2 登录成功（JWT cookie）", st == 200 and c.cookie.startswith("vt_token="))

    # ---------- E3 真实容器采集 ----------
    # 先 pull：让本地镜像带 RepoDigests（manifest 口径）——与真实用户"docker run 已拉取镜像"一致
    for spec in (f"{REG}/e2e-app:v1", f"{REG}/e2e-db:v1", f"{REG}/e2e-app:1.0.0", f"{REG}/e2e-app:pin"):
        sh_ok(f"docker pull {spec}")
    sh_ok(f"docker run -d --name e2e-web --label com.docker.compose.project=e2e-app --label com.docker.compose.service=web {REG}/e2e-app:v1")
    sh_ok(f"docker run -d --name e2e-db --label com.docker.compose.project=e2e-app --label com.docker.compose.service=db {REG}/e2e-db:v1")
    sh_ok(f"docker run -d --name e2e-pin {REG}/e2e-app:1.0.0")
    sh_ok(f"docker run -d --name e2e-prot --label dev.quenary.tugtainer.protected=true {REG}/e2e-app:pin")
    time.sleep(2)
    first = api.scan()
    check("E3 扫描检查数>=4（真实引擎容器入库）", first.get("checked", 0) >= 4, str(first))
    row = api.get_container("e2e-web")
    check("E3 compose 项目识别", row.get("compose_id") == "e2e-app", str(row))
    check("E3 protected 标记识别", api.get_container("e2e-prot").get("protected") == 1)

    # ---------- E4 基线巡检 ----------
    check("E4 首巡不告警（events=0）", first.get("events") == 0, str(first))

    # ---------- E5 digest-only 更新发现 ----------
    push_app("v1", "second-release")
    time.sleep(1)
    api.scan()
    check("E5 上游摘要变化 → 更新标记", api.get_container("e2e-web").get("update_available") == 1)

    # ---------- E6 compose 聚合通知 ----------
    push_db("v1", "second-release")
    time.sleep(1)
    api.scan()
    check("E6 项目双容器均标记更新",
          api.get_container("e2e-web").get("update_available") == 1 and api.get_container("e2e-db").get("update_available") == 1)
    check("E6 聚合通知 target=e2e-app", any(t == "update" and tgt == "e2e-app" and not w for t, tgt, w, _ in api.notifs()),
          str(api.notifs()[:5]))

    # ---------- E7 手动单容器更新 ----------
    old_img = container_image_id("e2e-web")
    st, body = c.req("POST", "/api/containers/e2e-web/update")
    results = {x["name"]: x["result"] for x in body.get("containers", [])}
    check("E7 单容器更新成功", st == 200 and results.get("e2e-web") == "updated", str(body))
    new_img = container_image_id("e2e-web")
    check("E7 镜像真实更换（Image ID 变化）", bool(new_img) and new_img != old_img, f"{old_img[:20]} -> {new_img[:20]}")
    check("E7 新容器运行中", dinspect("e2e-web").get("State", {}).get("Running") is True)
    st, v = c.req("GET", "/api/containers/e2e-web/versions")
    check("E7 版本台账记录", st == 200 and len(v.get("versions", [])) >= 2, str(v)[:200])

    # ---------- E8 健康门控失败 → 自动回滚 ----------
    api.settings({"health_wait_sec": "30"})
    push_app("v1", "bad-release", bad=True)
    time.sleep(1)
    api.scan()
    check("E8 坏版本被发现", api.get_container("e2e-web").get("update_available") == 1)
    cur_img = container_image_id("e2e-web")
    st, body = c.req("POST", "/api/containers/e2e-web/update")
    results = {x["name"]: x["result"] for x in body.get("containers", [])}
    check("E8 健康门控失败 → 自动回滚", st == 200 and results.get("e2e-web") == "rolled_back", str(body))
    rolled_img = container_image_id("e2e-web")
    check("E8 回滚后容器存在且镜像回到上一版", bool(rolled_img) and rolled_img == cur_img,
          f"{cur_img[:20]} vs {rolled_img[:20]}")
    check("E8 回滚后容器健康运行", dinspect("e2e-web").get("State", {}).get("Running") is True)

    # ---------- E9 手动版本回退 ----------
    st, v = c.req("GET", "/api/containers/e2e-web/versions")
    versions = v.get("versions", [])
    target = next((x for x in reversed(versions) if x["digest"] and not x["is_current"]), None)
    if not check("E9 版本列表可选历史版本", target is not None, str(v)[:200]):
        check("E9 指定版本回退成功", False, "skipped: no target version")
        check("E9 回退后自动关闭更新开关", False)
        check("E9 回退后容器运行", False)
    else:
        # 回退依赖本地镜像（定向重建语义）；真实环境镜像可能被清理，按 digest 重新拉取
        r = sh(f"docker pull {REG}/e2e-app@{target['digest']}")
        check("E9 按 digest 重新拉取历史镜像", r.returncode == 0, r.stderr[-120:])
        st, body = c.req("POST", "/api/containers/e2e-web/rollback", {"digest": target["digest"]})
        check("E9 指定版本回退成功", st == 200 and body.get("status") == "ok", str(body))
        check("E9 回退后自动关闭更新开关", api.get_container("e2e-web").get("update_enabled") == 0)
        check("E9 回退后容器运行", dinspect("e2e-web").get("State", {}).get("Running") is True)

    # ---------- E10 pin-watch 新版本 tag ----------
    push_app("1.1.0", "new-version")
    time.sleep(1)
    api.scan()
    check("E10 数字 tag 新版本 → new-tag 通知", any(t == "new-tag" and tgt == "e2e-pin" and not w for t, tgt, w, _ in api.notifs()),
          str(api.notifs()[:6]))
    check("E10 pin 容器不自动置更新标记", api.get_container("e2e-pin").get("update_available") == 0)

    # ---------- E11 watch 哨兵 ----------
    push_app("2.0.0", "watch-baseline")  # pin watch 的基线 tag 必须真实存在，否则检查 404
    push_watch("v1", "watch-seed")       # digest watch 同理：仓库与 tag 必须存在才能建基线
    st, _ = c.req("POST", "/api/watches", {"reference": f"{REG}/e2e-watch:v1"})
    check("E11 添加 digest watch", st == 200)
    st, _ = c.req("POST", "/api/watches", {"reference": f"{REG}/e2e-app:2.0.0"})
    check("E11 添加 pin watch", st == 200)
    api.scan()  # 基线
    push_watch("v1", "watch-second")
    push_app("2.1.0", "watch-newtag")
    time.sleep(1)
    api.scan()
    check("E11 watch 摘要变化 → update 通知", any(t == "update" and w for t, tgt, w, _ in api.notifs()), str(api.notifs()[:6]))
    check("E11 watch 新版本 tag → new-tag 通知", any(t == "new-tag" and w for t, tgt, w, _ in api.notifs()), str(api.notifs()[:8]))

    # ---------- E12 ignored 策略（干净容器，无历史更新标记） ----------
    sh_ok(f"docker run -d --name e2e-ign {REG}/e2e-db:v1")
    time.sleep(1)
    api.scan()  # 基线入库（update_available=0）
    st, _ = c.req("PUT", "/api/containers/e2e-ign/ignored", {"value": True})
    check("E12 设置忽略", st == 200)
    push_db("v1", "third-release")
    time.sleep(1)
    api.scan()
    check("E12 忽略后不标记更新", api.get_container("e2e-ign").get("update_available") == 0)

    # ---------- E13 webhook 通知渠道 ----------
    srv, received = start_webhook_sink()
    st, _ = c.req("POST", "/api/channels", {"kind": "webhook", "name": "e2e", "url": "http://127.0.0.1:9999/hook"})
    check("E13 添加 webhook 渠道", st == 200)
    st, body = c.req("POST", "/api/channels/test", {"kind": "webhook", "url": "http://127.0.0.1:9999/hook"})
    time.sleep(1)
    check("E13 渠道连通性测试投递", st == 200 and body.get("delivered") is True and len(received) >= 1, f"received={len(received)}")
    srv.shutdown()

    # ---------- E14 调度器自动扫描 + 自动更新 ----------
    st, _ = c.req("PUT", "/api/containers/e2e-db/ignored", {"value": False})
    push_db("v1", "fourth-release")
    time.sleep(1)
    api.scan()
    check("E14 前置：恢复跟踪并标记更新", api.get_container("e2e-db").get("update_available") == 1)
    jobs_before = max((j.get("id", 0) for j in c.req("GET", "/api/jobs")[1]), default=0)
    api.settings({"scan_interval_sec": "3"})
    auto_jobs = []
    deadline = time.time() + 45
    while time.time() < deadline:
        time.sleep(3)
        st, jobs = c.req("GET", "/api/jobs")
        auto_jobs = [j for j in jobs if j.get("id", 0) > jobs_before and j.get("status") == "done"]
        if auto_jobs:
            break
    check("E14 调度器自动执行更新任务", bool(auto_jobs), f"last job: {str(jobs[:1])[:160]}")
    auto_hit = any(
        x.get("name") == "e2e-db" and x.get("result") == "updated"
        for j in auto_jobs for x in (j.get("result") or {}).get("containers", [])
    ) if auto_jobs else False
    check("E14 自动更新真实执行（e2e-db updated）", auto_hit, str(jobs[:1])[:200])
    check("E14 自动更新后标记清除", api.get_container("e2e-db").get("update_available") == 0)
    api.settings({"scan_interval_sec": "0", "health_wait_sec": "0"})

    print(f"\n===== 真实引擎全场景验证：{len(PASS)} 通过 / {len(FAIL)} 失败 =====")
    if FAIL:
        for f in FAIL:
            print(f"  失败项: {f}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
