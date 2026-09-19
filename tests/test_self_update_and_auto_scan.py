"""针对 ContainerUp 自身容器保护、守护自更、以及扫描后自动执行更新的专项测试。"""
import pytest
from fastapi.testclient import TestClient
from backend import db, detect, engine
from backend.docker import MockDockerClient, is_self_container, _fake_digest
from backend.detect import StaticRegistrySource


REGISTRY = StaticRegistrySource({
    "mock/web:1.0": {"digest": _fake_digest("mock/web:1.0"), "tags": ["1.0"]},
    "learycn/containerup:latest": {"digest": _fake_digest("learycn/containerup:latest"), "tags": ["latest"]},
})


@pytest.fixture()
def client(monkeypatch):
    import backend.app as app_module

    app_module.DOCKER = MockDockerClient()
    monkeypatch.setattr(
        "backend.detect.make_registry_client", lambda: REGISTRY, raising=True
    )
    with TestClient(app_module.app) as tc:
        # 登录认证
        tc.post("/api/auth/setup", json={"username": "admin", "password": "admin123456"})
        yield tc, app_module.DOCKER


def test_is_self_container_identification():
    """测试 ContainerUp 自身运行实例的精准识别机制。"""
    # 1. 明确名字
    assert is_self_container({"name": "containerup", "image": "some/image"}) is True
    assert is_self_container({"name": "/containerup", "image": "some/image"}) is True
    assert is_self_container({"name": "vigiltainer", "image": "some/image"}) is True

    # 2. 标签标识
    assert is_self_container({"name": "my-custom-up", "labels": {"dev.containerup.self": "true"}}) is True
    assert is_self_container({"name": "my-custom-up", "labels": {"dev.containerup.self": "TRUE"}}) is True

    # 3. 镜像名称
    assert is_self_container({"name": "app", "image": "learycn/containerup:latest"}) is True
    assert is_self_container({"name": "app", "image": "muyicn/containerup:v1.1.5"}) is True

    # 4. 普通容器绝不误判
    assert is_self_container({"name": "redis", "image": "redis:alpine"}) is False
    assert is_self_container({"name": "nginx", "image": "nginx:latest"}) is False
    assert is_self_container({"name": "daidai", "image": "linzixuanzz/daidai-panel:latest"}) is False
    assert is_self_container(None) is False
    assert is_self_container({}) is False


def test_detect_sync_defaults_and_self_protection():
    """测试容器纳管同步：默认模式与自定义策略，自身容器始终默认关闭自动并受保护。"""
    docker = MockDockerClient()
    docker.seed_container("redis", "redis:alpine")
    docker.seed_container("containerup", "learycn/containerup:latest")

    # 1. 默认设置 default_update_enabled = 0
    db.setting_set("default_update_enabled", "0")
    detect._sync_containers(docker)

    row_redis = db.query_one("SELECT * FROM containers WHERE name='redis'")
    assert row_redis is not None
    assert row_redis["is_self"] == 0
    assert row_redis["protected"] == 0
    assert row_redis["update_enabled"] == 0

    row_self = db.query_one("SELECT * FROM containers WHERE name='containerup'")
    assert row_self is not None
    assert row_self["is_self"] == 1
    assert row_self["protected"] == 1  # 自身受保护
    assert row_self["update_enabled"] == 0  # 默认关闭自动更新，杜绝意外自杀

    # 2. 开启 default_update_enabled = 1 时，新容器自动开启，但自身容器依然坚决关闭自动
    docker.seed_container("nginx", "nginx:alpine")
    docker.seed_container("vigiltainer", "vigiltainer:latest")
    db.setting_set("default_update_enabled", "1")
    detect._sync_containers(docker)

    row_nginx = db.query_one("SELECT * FROM containers WHERE name='nginx'")
    assert row_nginx is not None
    assert row_nginx["update_enabled"] == 1

    row_vt = db.query_one("SELECT * FROM containers WHERE name='vigiltainer'")
    assert row_vt is not None
    assert row_vt["is_self"] == 1
    assert row_vt["update_enabled"] == 0  # 自身容器即使开启了全局默认更新也强制为 0



def test_engine_plan_self_protection_in_auto_and_batch():
    """测试更新规划：自动模式与全量手动更新均排除自身容器；定向手动更新允许自身容器。"""
    docker = MockDockerClient()
    docker.seed_container("app1", "app:latest")
    docker.seed_container("containerup", "learycn/containerup:latest")

    detect._sync_containers(docker)

    # 标记两容器均有可用更新并开启自动更新
    with db.tx() as conn:
        conn.execute("UPDATE containers SET update_available=1, update_enabled=1 WHERE name='app1'")
        conn.execute("UPDATE containers SET update_available=1, update_enabled=1 WHERE name='containerup'")

    # 1. 自动模式（candidates is None）：自身容器被 self-protected 排除
    plan_auto = engine._build_plan(docker, manual=False, names=None)
    assert "app1" in plan_auto.to_update
    assert "containerup" not in plan_auto.to_update
    assert plan_auto.reasons.get("containerup") == "self-protected"

    # 2. 手动全量更新（manual=True, names=None）：自身容器同样排除，避免全部更新时中断自己
    plan_batch = engine._build_plan(docker, manual=True, names=None)
    assert "app1" in plan_batch.to_update
    assert "containerup" not in plan_batch.to_update

    # 3. 定向单选自身更新（names=['containerup']）：用户明确单选自更
    plan_targeted = engine._build_plan(docker, manual=True, names=["containerup"])
    assert "containerup" in plan_targeted.to_update


def test_engine_execute_self_update_detached():
    """测试自身容器定向更新：不直接 stop，而是调用 _update_self（Mock 模式下成功返回 updated）。"""
    docker = MockDockerClient()
    docker.seed_container("containerup", "learycn/containerup:latest")
    detect._sync_containers(docker)

    with db.tx() as conn:
        conn.execute("UPDATE containers SET update_available=1 WHERE name='containerup'")

    # 执行更新自身容器
    res = engine.run_update(docker, manual=True, names=["containerup"])
    assert res.get("job_id") is not None
    containers = res.get("containers", [])
    assert len(containers) == 1
    assert containers[0]["name"] == "containerup"
    assert containers[0]["result"] == "updated"


def test_api_scan_auto_update_linkage(client):
    """测试 /api/scan 接口：开启 auto_update_after_scan 时，发现更新后自动联动更新。"""
    tc, docker = client

    docker.seed_container("web", "mock/web:1.0")
    detect._sync_containers(docker)

    # 容器开启自动更新且设置 auto_update_after_scan 为 1
    with db.tx() as conn:
        conn.execute("UPDATE containers SET update_enabled=1 WHERE name='web'")
    db.setting_set("auto_update_after_scan", "1")

    # 模拟 registry 有新镜像
    REGISTRY.specs["mock/web:1.0"] = {"digest": "sha256:newdigest999", "tags": ["1.0"]}

    # 发起扫描：检测到有更新后应自动执行更新
    r = tc.post("/api/scan?force=true&auto_update=true")
    assert r.status_code == 200
    data = r.json()
    assert data.get("status") == "ok"
    assert "update" in data
    assert data["update"].get("job_id") is not None


def test_api_get_update_command(client):
    """测试 /api/containers/{name}/update_command 获取宿主机 CLI 与 Compose 命令。"""
    tc, docker = client
    docker.seed_container("containerup", "learycn/containerup:latest", labels={
        "com.docker.compose.project": "containerup-stack",
        "com.docker.compose.service": "containerup",
        "com.docker.compose.project.config_files": "/opt/compose.yml",
        "com.docker.compose.project.working_dir": "/opt",
    })
    detect._sync_containers(docker)

    r = tc.get("/api/containers/containerup/update_command")
    assert r.status_code == 200
    data = r.json()
    assert data["name"] == "containerup"
    assert data["is_self"] is True
    assert data["is_compose"] is True
    assert "compose" in data["commands"]
    assert "docker compose -p containerup-stack" in data["commands"]["compose"]
    assert "compose_cd" in data["commands"]
    assert "cd /opt" in data["commands"]["compose_cd"]
