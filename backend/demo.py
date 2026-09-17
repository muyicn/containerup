"""演示模式种子：PRD 5.2 场景复现（compose 项目 demo-app：web → api → db）。

- demo-web（nginx:1.25-alpine）：有新摘要 → update 通知
- demo-api（myapp:2.0，pin-watch）：有新摘要 + 仓库出现 2.1 / 3.0 → update + new-tag 通知
- demo-db（postgres:16.2）：无变化 → 保持最新

依赖链：web 依赖 api（自定义标签），api 依赖 db（compose depends_on）。
"""
from typing import Any

from backend.docker import _fake_digest, MockDockerClient

DEMO_PROJECT = "demo-app"

DEMO_REGISTRY_SPECS: dict[str, dict[str, Any]] = {
    "nginx:1.25-alpine": {
        "digest": "sha256:" + "f" * 64,
        "tags": ["1.25-alpine", "1.25", "latest"],
    },
    "myapp:2.0": {
        "digest": "sha256:" + "e" * 64,
        "tags": ["2.0", "2.1", "3.0", "latest"],
    },
    # db 本地摘要与远端一致 → 无更新（_fake_digest("postgres:16.2")）
    "postgres:16.2": {
        "digest": _fake_digest("postgres:16.2"),
        "tags": ["16.2", "16.4", "17.0"],
    },
}

DB_LOCAL_DIGEST = _fake_digest("postgres:16.2")  # 保持与 seed_container 一致


def seed_demo(client: MockDockerClient) -> list[str]:
    """注入 3 容器 compose 场景，返回容器名列表（幂等）。"""
    names = []
    specs = [
        (
            "demo-web",
            "nginx:1.25-alpine",
            {
                "com.docker.compose.project": DEMO_PROJECT,
                "com.docker.compose.service": "web",
                "dev.quenary.tugtainer.depends_on": "demo-api",
            },
        ),
        (
            "demo-api",
            "myapp:2.0",
            {
                "com.docker.compose.project": DEMO_PROJECT,
                "com.docker.compose.service": "api",
                "com.docker.compose.depends_on": "db:condition:service_healthy",
                "dev.quenary.tugtainer.depends_on": "demo-db",
            },
        ),
        (
            "demo-db",
            "postgres:16.2",
            {
                "com.docker.compose.project": DEMO_PROJECT,
                "com.docker.compose.service": "db",
            },
        ),
    ]
    for name, image, labels in specs:
        if not client.exists(name):
            client.seed_container(name, image, labels=labels, running=True, health="healthy")
        names.append(name)
    return names
