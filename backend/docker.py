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
from backend.registry import parse_image_spec, plausible_version


class DockerError(RuntimeError):
    pass


# 平台托管标记：容器显式打上 dev.containerup.managed=false 时，扫描跳过该容器
MANAGED_LABEL = "dev.containerup.managed"

# 镜像版本标签（OCI 标准 + label-schema 约定，构建时写入，运行时随容器 Labels 透出）
_VERSION_LABELS = ("org.opencontainers.image.version", "org.label-schema.version")


def _label_version(labels: Optional[dict[str, str]]) -> str:
    for k in _VERSION_LABELS:
        v = (labels or {}).get(k)
        if v:
            return str(v)[:64]
    return ""


def compose_spec(labels: Optional[dict[str, str]]) -> Optional[dict[str, Any]]:
    """从容器标签提取 compose 项目规格（project/service/config_files 三要素齐全才视为 compose 管理）。

    compose 管理的容器更新必须走 compose up 重建（保留端口/挂载/网络等全部配置），
    不能用 docker run 自行重建 —— 那会丢掉 compose 定义的所有运行时配置。
    """
    if not labels:
        return None
    project = (labels.get("com.docker.compose.project") or "").strip()
    service = (labels.get("com.docker.compose.service") or "").strip()
    files_raw = (labels.get("com.docker.compose.project.config_files") or "").strip()
    if not project or not service or not files_raw:
        return None
    files = [f.strip() for f in files_raw.split(",") if f.strip()]
    if not files:
        return None
    workdir = (labels.get("com.docker.compose.project.working_dir") or "").strip()
    return {"project": project, "service": service, "files": files, "workdir": workdir}


def run_args_from_config(config: dict[str, Any], labels: Optional[dict] = None) -> list[str]:
    """把 inspect 快照的运行时配置映射为 docker run 参数（重建保真）。

    覆盖：端口/卷挂载/网络与静态 IP/别名/hosts/dns/cap/devices/security/
    user/workdir/hostname/tty/init/rm/log/pid/ipc/entrypoint(单项)/volumes-from。
    """
    args: list[str] = []
    for k, v in (labels or {}).items():
        args += ["-l", f"{k}={v}"]
    for e in config.get("env") or []:
        args += ["-e", e]
    # 重启策略（on-failure 带重试次数）
    _rp = config.get("restart") or "no"
    if isinstance(_rp, dict):
        _rn = _rp.get("Name") or "no"
        _rc = _rp.get("MaximumRetryCount") or 0
        _rp = f"{_rn}:{_rc}" if _rn == "on-failure" and _rc else _rn
    if _rp and _rp != "no":
        args += ["--restart", str(_rp)]
    # 端口映射（PortBindings：{"80/tcp": [{HostIp, HostPort}]})
    for port, binds in (config.get("ports") or {}).items():
        for b in binds or [{}]:
            hp = str((b or {}).get("HostPort") or "")
            hip = str((b or {}).get("HostIp") or "").strip()
            p = str(port)
            if hip and hp:
                args += ["-p", f"{hip}:{hp}:{p}"]
            elif hp:
                args += ["-p", f"{hp}:{p}"]
            else:
                args += ["-p", p]
    # 卷挂载（Binds 经典式："/host:/ctr:rw"）
    for b in config.get("binds") or []:
        args += ["-v", b]
    # Mounts 新式（具名卷/bind；匿名卷由镜像 VOLUME 指令自动带出，不重复声明）
    for m in config.get("mounts") or []:
        src = m.get("Source") or m.get("Name") or ""
        tgt = m.get("Target") or ""
        if not src or not tgt or tgt in {b.split(":")[1] for b in (config.get("binds") or []) if ":" in b}:
            continue
        spec_v = f"{src}:{tgt}"
        if m.get("RW") is False:
            spec_v += ":ro"
        args += ["-v", spec_v]
    nm = str(config.get("network_mode") or "")
    if nm and nm not in ("bridge", "default"):
        args += ["--network", nm]
    # network-alias 与 --ip 仅在自定义网络下受支持（bridge/default/host/none 传递会导致 Docker 报错）
    if nm and nm not in ("bridge", "default", "host", "none") and not nm.startswith("container:"):
        for a in config.get("network_aliases") or []:
            args += ["--network-alias", str(a)]
        ip4 = str(config.get("ipam_v4") or "")
        if ip4:
            args += ["--ip", ip4]
    for h in config.get("extra_hosts") or []:
        args += ["--add-host", str(h)]
    for d in config.get("dns") or []:
        args += ["--dns", str(d)]
    for c in config.get("cap_add") or []:
        args += ["--cap-add", c]
    for c in config.get("cap_drop") or []:
        args += ["--cap-drop", c]
    if config.get("privileged"):
        args.append("--privileged")
    for d in config.get("devices") or []:
        dev = d.get("PathOnHost", "") if isinstance(d, dict) else str(d)
        if not dev:
            continue
        in_c = (d.get("PathInContainer") or dev) if isinstance(d, dict) else dev
        perms = (d.get("CgroupPermissions") or "rwm") if isinstance(d, dict) else "rwm"
        args += ["--device", f"{dev}:{in_c}:{perms}"]
    for s in config.get("security_opt") or []:
        args += ["--security-opt", str(s)]
    if config.get("user"):
        args += ["--user", str(config["user"])]
    if config.get("working_dir"):
        args += ["--workdir", str(config["working_dir"])]
    if config.get("hostname"):
        args += ["--hostname", str(config["hostname"])]
    if config.get("tty"):
        args.append("-t")
    if config.get("open_stdin"):
        args.append("-i")
    if config.get("init"):
        args.append("--init")
    if config.get("auto_remove"):
        args.append("--rm")
    for g in config.get("group_add") or []:
        args += ["--group-add", str(g)]
    if config.get("shm_size"):
        args += ["--shm-size", str(config["shm_size"])]
    log_type = str(config.get("log_type") or "")
    if log_type and log_type != "json-file":  # json-file 是默认驱动，不必显式传
        args += ["--log-driver", log_type]
    for k, v in (config.get("log_opts") or {}).items():
        args += ["--log-opt", f"{k}={v}"]
    if config.get("pid_mode"):
        args += ["--pid", str(config["pid_mode"])]
    if config.get("ipc_mode"):
        args += ["--ipc", str(config["ipc_mode"])]
    ep_cmd = config.get("entrypoint") or []
    if len(ep_cmd) == 1:  # docker run --entrypoint 仅支持单项
        args += ["--entrypoint", str(ep_cmd[0])]
    for vf in config.get("volumes_from") or []:
        args += ["--volumes-from", str(vf)]
    return args


def _fake_digest(spec: str) -> str:
    """Mock 摘要：对引用做稳定哈希（模拟 sha256 digest）。"""
    return "sha256:" + hashlib.sha256(spec.encode()).hexdigest()


class LocalDockerClient:
    """基于 docker CLI 的真实实现（JSON 输出）。"""

    def _run(self, *args: str, timeout: int = 120) -> str:
        cmd = [CONFIG.DOCKER_BIN, *args]
        try:
            out = subprocess.run(
                cmd, capture_output=True, text=True, timeout=timeout, encoding="utf-8", errors="replace"
            )
        except FileNotFoundError as e:
            raise DockerError(f"docker binary not found: {CONFIG.DOCKER_BIN}") from e
        except subprocess.TimeoutExpired as e:
            raise DockerError(f"docker {' '.join(args)} 超时（超过 {timeout} 秒）") from e
        except Exception as e:
            raise DockerError(f"docker {' '.join(args)} 执行异常: {e}") from e
        if out.returncode != 0:
            raise DockerError(f"docker {' '.join(args)} 失败: {out.stderr.strip() or out.stdout.strip()}")
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
        full_image_id = data.get("Image") or ""
        image_id = full_image_id[:71] or _fake_digest(image_spec)
        # RepoDigests 是镜像属性（不在容器 inspect 里），需额外查 image inspect
        repo_digests: list[str] = []
        if full_image_id:
            try:
                img_raw = self._run("image", "inspect", full_image_id, "--format", "{{json .RepoDigests}}")
                repo_digests = json.loads(img_raw) if img_raw.strip() else []
            except DockerError:
                pass
        host = data.get("HostConfig") or {}
        cfgc = data.get("Config") or {}
        net = data.get("NetworkSettings") or {}
        networks = net.get("Networks") or {}
        nm = host.get("NetworkMode") or ""
        # 与 NetworkMode 匹配的 endpoint（静态 IP/别名）；缺省取唯一网络
        ep = networks.get(nm) or (next(iter(networks.values())) if len(networks) == 1 else None) or {}
        return {
            "name": (data.get("Name") or "").lstrip("/"),
            "image": image_spec,
            "image_id": image_id,
            # manifest 摘要（pull 时记录）：与 registry 检测同口径，用于更新对比；
            # image_id 是 config digest，仅用于回退定向重建，两者不可混用
            "repo_digest": repo_digests[0].split("@")[-1] if repo_digests else "",
            # 镜像构建时写入的版本号（容器 Labels 已包含镜像标签，零额外开销）
            "image_version": _label_version(labels),
            "running": state.get("Running", False),
            "health": health if health != "none" else "none",
            "labels": labels,
            "config": {
                "env": cfgc.get("Env") or [],
                "cmd": cfgc.get("Cmd") or [],
                "ports": host.get("PortBindings") or {},
                "restart": (host.get("RestartPolicy") or {}).get("Name", "no"),
                # ---- 完整运行时配置（重建保真：端口/挂载/网络等不再丢失）----
                "binds": host.get("Binds") or [],
                "mounts": host.get("Mounts") or [],
                "network_mode": nm or "bridge",
                "network_aliases": (ep.get("Aliases") or []) if isinstance(ep, dict) else [],
                "ipam_v4": ((ep.get("IPAMConfig") or {}).get("IPv4Address") or "") if isinstance(ep, dict) else "",
                "extra_hosts": host.get("ExtraHosts") or [],
                "dns": host.get("Dns") or [],
                "cap_add": host.get("CapAdd") or [],
                "cap_drop": host.get("CapDrop") or [],
                "privileged": bool(host.get("Privileged")),
                "devices": host.get("Devices") or [],
                "security_opt": host.get("SecurityOpt") or [],
                "user": cfgc.get("User") or "",
                "working_dir": cfgc.get("WorkingDir") or "",
                "hostname": cfgc.get("Hostname") or "",
                "tty": bool(cfgc.get("Tty")),
                "open_stdin": bool(cfgc.get("OpenStdin")),
                "init": bool(host.get("Init")),
                "auto_remove": bool(host.get("AutoRemove")),
                "group_add": host.get("GroupAdd") or [],
                "shm_size": host.get("ShmSize") or 0,
                "log_type": (host.get("LogConfig") or {}).get("Type") or "",
                "log_opts": (host.get("LogConfig") or {}).get("Config") or {},
                "entrypoint": cfgc.get("Entrypoint") or [],
                "volumes_from": host.get("VolumesFrom") or [],
                "pid_mode": host.get("PidMode") or "",
                "ipc_mode": host.get("IpcMode") or "",
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
        """把台账中的 manifest digest 解析为可运行的镜像引用。

        返回 repo:tag@sha256:digest 形式：既精确锁定历史版本，又保留 tag 语义
        （容器镜像引用仍显示 latest 而非镜像 ID，符合 compose/用户直觉）。
        本地缺失该 digest 时按 digest 重新拉取（镜像被清理后的现实恢复路径）。
        """
        try:
            registry, repo, tag = parse_image_spec(repo_spec)
        except ValueError:
            registry, repo, tag = "", "", ""
        if repo and digest:
            # 本地已有该 digest 则直接引用；缺失时先按 digest 拉取
            have = False
            try:
                raw = self._run("images", "--digests", "--format", "{{json .}}")
                for line in raw.splitlines():
                    if not line.strip():
                        continue
                    img = json.loads(line)
                    if (img.get("Digest") or "") == digest and (img.get("Repository") or "").endswith(repo.split("/")[-1]):
                        have = True
                        break
            except DockerError:
                pass
            if not have:
                try:
                    pull_ref = f"{repo}@{digest}" if registry in {"registry-1.docker.io", "docker.io"} \
                        else f"{registry}/{repo}@{digest}"
                    self._run("pull", pull_ref)
                except DockerError:
                    pass  # 预拉失败不阻断：docker run 时会再拉
            prefix = "" if registry in {"registry-1.docker.io", "docker.io"} else f"{registry}/"
            return f"{prefix}{repo}@{digest}"
        # 兼容兑底：无 repo/tag 信息时退回完整 Image ID
        try:
            raw = self._run("images", "--digests", "--format", "{{json .}}")
            for line in raw.splitlines():
                if not line.strip():
                    continue
                img = json.loads(line)
                if (img.get("Digest") or "") == digest and img.get("ID"):
                    return self._run("image", "inspect", img["ID"], "--format", "{{.Id}}").strip()
        except DockerError:
            pass
        return digest

    def remove_image(self, image_spec: str, digest: str, image_id: Optional[str] = None) -> bool:
        """删除旧版本镜像（更新成功后的清理）：优先按完整镜像 ID 直删，
        回退 repo@digest；被其他容器引用/不存在时安全忽略（记录原因）。"""
        try:
            registry, repo, _ = parse_image_spec(image_spec)
        except ValueError:
            registry, repo = "", ""
        prefix = "" if registry in {"registry-1.docker.io", "docker.io", ""} else f"{registry}/"
        attempts: list[str] = []
        if image_id:
            attempts.append(image_id)
        if repo and digest:
            attempts.append(f"{prefix}{repo}@{digest}")
        for t in attempts:
            try:
                self._run("rmi", t)
                return True
            except DockerError as e:
                # 被其他容器引用/已删除等：记录原因便于诊断，继续下一尝试
                logging.getLogger("docker").warning("rmi %s skipped: %s", t[:40], e)
        return False

    def local_image_versions(self, repo_spec: str) -> list[dict[str, str]]:
        """枚举本地与该镜像仓库相关的镜像（含 dangling 历史版本），供台账回填。

        返回 [{repo_digest, image_id, version, created_at}]，按创建时间倒序。
        """
        try:
            registry, repo, _ = parse_image_spec(repo_spec)
        except ValueError:
            return []
        prefix_repo = repo if registry in {"registry-1.docker.io"} else f"{registry}/{repo}"
        out: list[dict[str, str]] = []
        try:
            raw = self._run(
                "images", "--digests", "--format",
                "{{.Repository}}\t{{.Tag}}\t{{.Digest}}\t{{.ID}}\t{{.CreatedAt}}",
            )
        except DockerError:
            return []
        for line in raw.splitlines():
            parts = line.split("\t")
            if len(parts) != 5:
                continue
            repository, _tag, digest, iid, created = parts
            if repository not in (repo, prefix_repo, f"{repo.split('/')[-1]}") and repository != "<none>":
                continue
            if not digest or digest == "<none>" or "<none>" in iid:
                continue
            version = ""
            try:
                vraw = self._run("image", "inspect", iid, "--format",
                                 "{{index .Config.Labels \"org.opencontainers.image.version\"}}")
                version = (vraw or "").strip()
                if not version or version == "<no value>":
                    vraw2 = self._run("image", "inspect", iid, "--format",
                                      "{{index .Config.Labels \"org.label-schema.version\"}}")
                    version = (vraw2 or "").strip()
                if version == "<no value>":
                    version = ""
            except DockerError:
                pass
            # 标签无像样版本号（空/main 等）→ digest 匹配 Docker Hub tags 反解
            # （如旧 dangling 镜像 ≡ v0.7.20），回退弹窗的历史版本显示真实版本号
            if not version or not plausible_version(version):
                try:
                    from backend.registry import version_by_digest

                    tv = version_by_digest(repo_spec, digest)
                    if tv:
                        version = tv
                except Exception:
                    pass
            out.append({"repo_digest": digest, "image_id": iid, "version": version[:64], "created_at": created})
        return out

    def create(self, name: str, image: str, config: dict[str, Any], labels: Optional[dict] = None, image_id: Optional[str] = None) -> dict[str, Any]:
        # image_id：定向用指定镜像 ID 重建（回退场景；本地 dangling 镜像仍存在时有效）
        # 重建保真：run_args_from_config 完整映射端口/挂载/网络/安全等运行时配置
        ref = image_id or image
        cmd = ["run", "-d", "--name", name, *run_args_from_config(config or {}, labels), ref, *(config.get("cmd") or [])]
        self._run(*cmd)
        return self.inspect(name)

    def compose_up(self, spec: dict[str, Any]) -> str:
        """按容器标签里的 compose 项目规格重建服务（保留 compose 全部配置）。

        docker pull 已把新镜像拉到本地；compose up 检测到镜像 ID 变化会自动
        recreate 容器，端口/挂载/网络/依赖等全部按 compose 文件保留。
        """
        cmd = ["compose", "-p", spec["project"]]
        for f in spec["files"]:
            cmd += ["-f", f]
        if spec.get("workdir"):
            cmd += ["--project-directory", spec["workdir"]]
        cmd += ["up", "-d", "--no-deps", spec["service"]]
        return self._run(*cmd, timeout=300)


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
            "image_version": _label_version(labels),
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
        """Mock 无 config/manifest digest 之分，返回 tag@digest 引用形态。"""
        try:
            _, repo, tag = parse_image_spec(repo_spec)
        except ValueError:
            repo, tag = "", ""
        return f"{repo}@{digest}" if repo else digest

    def remove_image(self, image_spec: str, digest: str, image_id: Optional[str] = None) -> bool:
        """Mock：记录清理动作。"""
        if not digest and not image_id:
            return False
        self.event_log.append(f"rmi:{(digest or image_id or '')[:20]}")
        return True

    def local_image_versions(self, repo_spec: str) -> list[dict[str, str]]:
        """Mock：无本地镜像仓库可枚举。"""
        return []

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
            "image_version": _label_version(labels),
            "running": False,
            "health": "none",
            "labels": dict(labels or {}),
            "config": dict(config),
        }
        self.event_log.append(f"create:{name}")
        return dict(self._containers[name])

    def compose_up(self, spec: dict[str, Any]) -> str:
        """Mock：模拟 compose up——把该项目/服务的容器重建为 pull 后的新镜像（配置保留、拉起）。"""
        self.event_log.append(f"compose_up:{spec['project']}/{spec['service']}")
        for n, c in self._containers.items():
            lb = c.get("labels") or {}
            if lb.get("com.docker.compose.project") == spec["project"] \
                    and lb.get("com.docker.compose.service") == spec["service"]:
                self._rebuild_count[n] = self._rebuild_count.get(n, 0) + 1
                new_digest = self._digest_map.get(c["image"], _fake_digest(c["image"]))
                c["image_id"] = new_digest
                c["repo_digest"] = new_digest
                c["running"] = True
                # 预设的"新版本启动失败"剧本：重建后的第一次拉起 → unhealthy
                if n in self._unhealthy_once and self._rebuild_count[n] == 1:
                    c["health"] = "unhealthy"
                    self.event_log.append(f"unhealthy:{n}")
                else:
                    c["health"] = "healthy"
        return "mocked"

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
