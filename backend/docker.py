"""Docker 客户端层：抽象接口 + 本地 CLI 实现 + 内存 Mock 实现。

- LocalDockerClient：subprocess 调 docker CLI（有 Docker 引擎的环境用）。
- MockDockerClient：内存容器模型，支撑无 Docker 环境下的端到端复现与测试；
  支持预设"更新后 unhealthy"以驱动回滚路径验证。

统一容器视图（inspect 结果）：
{name, image(spec), image_id, running(bool), health(one of healthy/unhealthy/none),
 labels(dict), config(dict: 重建所需的最小配置)}
"""
import hashlib
import json
import logging
import subprocess
from typing import Any, Optional

from backend.config import CONFIG
from backend.registry import parse_image_spec


class DockerError(RuntimeError):
    pass


# 平台托管标记：容器显式打上 dev.containerup.managed=false 时，扫描跳过该容器
MANAGED_LABEL = "dev.containerup.managed"


def _fake_digest(spec: str) -> str:
    """Mock 摘要：对引用做稳定哈希（模拟 sha256 digest）。"""
    return "sha256:" + hashlib.sha256(spec.encode()).hexdigest()


class LocalDockerClient:
    """基于 docker CLI 的真实实现（JSON 输出）。"""

    def _run(self, *args: str) -> str:
        cmd = [CONFIG.DOCKER_BIN, *args]
        try:
            out = subprocess.run(
                cmd, capture_output=True, text=True, timeout=120, encoding="utf-8"
            )
        except FileNotFoundError as e:
            raise DockerError(f"docker binary not found: {CONFIG.DOCKER_BIN}") from e
        if out.returncode != 0:
            raise DockerError(f"docker {' '.join(args)} failed: {out.stderr.strip()}")
        return out.stdout

    def list_containers(self) -> list[dict[str, Any]]:
        # 注意：不能用 `--filter label=k!=v`——Docker 的语义是"必须拥有 k 且值不同"，
        # 没有该 label 的普通容器会被整体排除（曾导致真实引擎扫描"检查 0"）。
        # 列出全部容器后在应用侧过滤：仅排除显式 managed=false 的容器。
        raw = self._run("ps", "-a", "--format", "{{json .}}")
        out = []
        for line in raw.splitlines():
            if not line.strip():
                continue
            c = json.loads(line)
            name = c.get("Names", "")
            if not name:
                continue
            info = self.inspect(name)
            if (info.get("labels") or {}).get(MANAGED_LABEL, "").strip().lower() == "false":
                continue
            out.append(info)
        return out

    def inspect(self, name: str) -> dict[str, Any]:
        raw = self._run("inspect", name)
        data = json.loads(raw)[0]
        labels = (data.get("Config") or {}).get("Labels") or {}
        state = data.get("State") or {}
        health = (state.get("Health") or {}).get("Status", "none")
        image_spec = (data.get("Config") or {}).get("Image", "")
        repo_digests = data.get("RepoDigests") or []
        return {
            "name": (data.get("Name") or "").lstrip("/"),
            "image": image_spec,
            "image_id": (data.get("Image") or "")[:71] or _fake_digest(image_spec),
            # manifest 摘要（pull 时记录）：与 registry 检测同口径，用于更新对比；
            # image_id 是 config digest，仅用于回退定向重建，两者不可混用
            "repo_digest": repo_digests[0].split("@")[-1] if repo_digests else "",
            "running": state.get("Running", False),
            "health": health if health != "none" else "none",
            "labels": labels,
            "config": {
                "env": (data.get("Config") or {}).get("Env") or [],
                "cmd": (data.get("Config") or {}).get("Cmd") or [],
                "ports": (data.get("HostConfig") or {}).get("PortBindings") or {},
                "restart": (data.get("HostConfig") or {}).get("RestartPolicy", {}).get("Name", "no"),
            },
        }

    def exists(self, name: str) -> bool:
        out = subprocess.run(
            [CONFIG.DOCKER_BIN, "inspect", name], capture_output=True, text=True
        )
        return out.returncode == 0

    def stop(self, name: str) -> None:
        self._run("stop", name)

    def start(self, name: str) -> None:
        self._run("start", name)

    def remove(self, name: str) -> None:
        self._run("rm", "-f", name)

    def pull(self, spec: str) -> str:
        self._run("pull", spec)
        raw = self._run("inspect", f"{spec}@json") if False else ""
        # 取 pull 后镜像 digest
        out = subprocess.run(
            [CONFIG.DOCKER_BIN, "image", "inspect", spec, "--format", "{{index .RepoDigests 0}}"],
            capture_output=True, text=True,
        )
        if out.returncode == 0 and out.stdout.strip():
            return out.stdout.strip()
        return _fake_digest(spec)

    def resolve_image_ref(self, repo_spec: str, digest: str) -> str:
        """把台账中的 manifest digest 解析为本地可运行的镜像引用。

        优先匹配本地已存在的 RepoDigest；缺失时按 repo@digest 从 registry 重新拉取
        （镜像被清理后的现实恢复路径）。返回 daemon 可直接 run 的引用。
        """
        try:
            raw = self._run("images", "--digests", "--format", "{{json .}}")
            for line in raw.splitlines():
                if not line.strip():
                    continue
                img = json.loads(line)
                if (img.get("Digest") or "") == digest and img.get("ID"):
                    return img["ID"]
        except DockerError:
            pass
        try:
            registry, repo, _ = parse_image_spec(repo_spec)
        except ValueError:
            registry, repo = "", ""
        if repo:
            ref = f"{repo}@{digest}" if registry in {"registry-1.docker.io"} else f"{registry}/{repo}@{digest}"
            self._run("pull", ref)
            return self._run("image", "inspect", ref, "--format", "{{.Id}}").strip()
        return digest

    def create(self, name: str, image: str, config: dict[str, Any], labels: Optional[dict] = None, image_id: Optional[str] = None) -> dict[str, Any]:
        # image_id：定向用指定镜像 ID 重建（回退场景；本地 dangling 镜像仍存在时有效）
        # 注意：_run() 会自动加 DOCKER_BIN 前缀，这里只传纯参数（曾误加导致 "docker docker run"）
        ref = image_id or image
        cmd = ["run", "-d", "--name", name]
        for k, v in (labels or {}).items():
            cmd += ["-l", f"{k}={v}"]
        for e in config.get("env", []):
            cmd += ["-e", e]
        policy = config.get("restart", "unless-stopped")
        cmd += [f"--restart={policy}", ref, *config.get("cmd", [])]
        self._run(*cmd)
        return self.inspect(name)


class MockDockerClient:
    """内存容器模型（演示模式与测试）。

    行为钩子：
    - unhealthy_once: set[容器名] —— 该容器"重建后的第一次启动"返回 unhealthy，
      模拟新版本启动失败 → 驱动回滚路径；后续（回滚重建后）恢复 healthy。
    - pull_hook: 可注入 spec→digest 映射（默认按引用哈希，tag 不变摘要不变）。
    """

    def __init__(self) -> None:
        self._containers: dict[str, dict[str, Any]] = {}
        self._unhealthy_once: set[str] = set()
        self._rebuild_count: dict[str, int] = {}
        self._digest_map: dict[str, str] = {}
        self.event_log: list[str] = []  # 动作审计（测试断言顺序用）

    # ---------- 场景构造（测试/演示用） ----------

    def seed_container(
        self,
        name: str,
        image: str,
        labels: Optional[dict[str, str]] = None,
        running: bool = True,
        health: str = "healthy",
    ) -> None:
        self._containers[name] = {
            "name": name,
            "image": image,
            "image_id": _fake_digest(image),
            "repo_digest": _fake_digest(image),
            "running": running,
            "health": health,
            "labels": dict(labels or {}),
            "config": {"env": [], "cmd": [], "restart": "unless-stopped"},
        }

    def set_digest(self, spec: str, digest: str) -> None:
        """注入 mock registry 摘要（pull 结果）。"""
        self._digest_map[spec] = digest

    def mark_unhealthy_once(self, name: str) -> None:
        self._unhealthy_once.add(name)

    # ---------- 统一接口 ----------

    def list_containers(self) -> list[dict[str, Any]]:
        return [dict(c) for c in self._containers.values()]

    def inspect(self, name: str) -> dict[str, Any]:
        if name not in self._containers:
            raise DockerError(f"no such container: {name}")
        return dict(self._containers[name])

    def exists(self, name: str) -> bool:
        return name in self._containers

    def stop(self, name: str) -> None:
        self._require(name)
        self._containers[name]["running"] = False
        self.event_log.append(f"stop:{name}")

    def start(self, name: str) -> None:
        self._require(name)
        self._containers[name]["running"] = True
        self.event_log.append(f"start:{name}")
        # 预设的"新版本启动失败"剧本
        if name in self._unhealthy_once:
            n = self._rebuild_count.get(name, 0)
            if n == 1:  # 第一次重建后的启动 → unhealthy
                self._containers[name]["health"] = "unhealthy"
                self.event_log.append(f"unhealthy:{name}")
                return
        self._containers[name]["health"] = "healthy"

    def remove(self, name: str) -> None:
        self._require(name)
        del self._containers[name]
        self.event_log.append(f"remove:{name}")

    def pull(self, spec: str) -> str:
        self.event_log.append(f"pull:{spec}")
        if spec in self._digest_map:
            return self._digest_map[spec]
        return _fake_digest(spec)

    def resolve_image_ref(self, repo_spec: str, digest: str) -> str:
        """Mock 无 config/manifest digest 之分，原样返回。"""
        return digest

    def create(
        self, name: str, image: str, config: dict[str, Any], labels: Optional[dict] = None,
        image_id: Optional[str] = None,
    ) -> dict[str, Any]:
        self._rebuild_count[name] = self._rebuild_count.get(name, 0) + 1
        self._containers[name] = {
            "name": name,
            "image": image,
            "image_id": image_id or self.pull(image),
            "repo_digest": image_id or self.pull(image),
            "running": False,
            "health": "none",
            "labels": dict(labels or {}),
            "config": dict(config),
        }
        self.event_log.append(f"create:{name}")
        return dict(self._containers[name])

    def _require(self, name: str) -> None:
        if name not in self._containers:
            raise DockerError(f"no such container: {name}")


def make_docker_client():
    """按配置选择实现：DEMO/无 docker CLI → Mock；否则 Local。"""
    if CONFIG.DEMO_MODE:
        return MockDockerClient()
    import shutil

    if shutil.which(CONFIG.DOCKER_BIN):
        return LocalDockerClient()
    # 无 Docker CLI 自动降级 Mock——必须显式告警，杜绝静默降级（用户侧表现为"扫描检查 0"）
    logging.getLogger("docker").warning(
        "未找到 docker CLI（VT_DOCKER_BIN=%s），已降级为内存 Mock 客户端，扫描将看不到任何容器。"
        "修复：容器部署请使用内置 CLI 的官方镜像并挂载 /var/run/docker.sock（重新 docker compose pull）；"
        "源码部署请安装 Docker CLI 或设置 VT_DOCKER_BIN 指向可用二进制。",
        CONFIG.DOCKER_BIN,
    )
    return MockDockerClient()


MOCK_FALLBACK = "无可用 Docker 引擎，已自动降级为内存 Mock 客户端（演示模式语义）"
