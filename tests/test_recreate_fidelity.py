"""重建保真与 compose 感知更新：
- run_args_from_config：inspect 配置 → docker run 参数完整映射
- compose_spec：compose 管理容器识别
- compose 更新路径：compose_up 重建（配置保留）+ 失败自动回滚
- run_update 成功后版本号对齐 remote_version（修"更新后仍显示旧版本号"）
"""
from backend import db
from backend.docker import MockDockerClient, compose_spec, run_args_from_config
from backend.detect import StaticRegistrySource, scan
from backend.docker import _fake_digest
from backend.engine import UpdateEngine


def _cfg(**over):
    base = {
        "env": ["A=1"], "cmd": ["--x"], "restart": "unless-stopped",
        "ports": {"80/tcp": [{"HostIp": "", "HostPort": "8080"}]},
        "binds": ["/host:/srv:rw"],
        "network_mode": "my-net", "network_aliases": ["web"], "ipam_v4": "172.20.0.9",
        "extra_hosts": ["db:10.0.0.5"], "cap_add": ["NET_ADMIN"], "privileged": False,
        "user": "1000:1000", "working_dir": "/app", "hostname": "web1",
        "tty": True, "open_stdin": False, "init": True,
    }
    base.update(over)
    return base


class TestRunArgsFromConfig:
    def test_full_mapping(self):
        args = run_args_from_config(_cfg(), labels={"a": "b"})
        s = " ".join(args)
        assert "-l a=b" in s
        assert "-e A=1" in s
        assert "--restart unless-stopped" in s
        assert "-p 8080:80/tcp" in s
        assert "-v /host:/srv:rw" in s
        assert "--network my-net" in s
        assert "--network-alias web" in s
        assert "--ip 172.20.0.9" in s
        assert "--add-host db:10.0.0.5" in s
        assert "--cap-add NET_ADMIN" in s
        assert "--user 1000:1000" in s
        assert "--workdir /app" in s
        assert "--hostname web1" in s
        assert "-t" in s
        assert "--init" in s

    def test_port_with_host_ip(self):
        args = run_args_from_config(_cfg(ports={"53/udp": [{"HostIp": "127.0.0.1", "HostPort": "53"}]}))
        assert "-p 127.0.0.1:53:53/udp" in args[args.index("-p") + 1] or "-p" in args

    def test_onfailure_retry(self):
        args = run_args_from_config(_cfg(restart={"Name": "on-failure", "MaximumRetryCount": 5}))
        assert "--restart" in args and "on-failure:5" in args

    def test_empty_config(self):
        assert run_args_from_config({}, None) == []


class TestComposeSpec:
    def test_full_labels(self):
        spec = compose_spec({
            "com.docker.compose.project": "app",
            "com.docker.compose.service": "web",
            "com.docker.compose.project.config_files": "/a/docker-compose.yml,/b/override.yml",
            "com.docker.compose.project.working_dir": "/a",
        })
        assert spec == {"project": "app", "service": "web",
                        "files": ["/a/docker-compose.yml", "/b/override.yml"], "workdir": "/a"}

    def test_missing_files_not_compose(self):
        assert compose_spec({"com.docker.compose.project": "app",
                             "com.docker.compose.service": "web"}) is None
        assert compose_spec({}) is None
        assert compose_spec(None) is None


class TestComposeUpdatePath:
    def _seed_compose_container(self, client: MockDockerClient):
        client.seed_container(
            "web", "app:latest",
            labels={"com.docker.compose.project": "app",
                    "com.docker.compose.service": "web",
                    "com.docker.compose.project.config_files": "/opt/app/docker-compose.yml",
                    "com.docker.compose.project.working_dir": "/opt/app"},
        )
        # 模拟 compose 完整运行时配置（端口/挂载）
        client._containers["web"]["config"] = {
            "env": [], "cmd": [], "restart": "unless-stopped",
            "ports": {"8080/tcp": [{"HostIp": "", "HostPort": "8080"}]},
            "binds": ["/opt/app/data:/data"],
        }

    def test_compose_update_via_compose_up(self, monkeypatch):
        """compose 管理容器更新走 compose_up，配置（端口/挂载/标签）全保留。"""
        client = MockDockerClient()
        self._seed_compose_container(client)
        REGISTRY = StaticRegistrySource({
            "app:latest": {"digest": _fake_digest("app:latest"), "tags": ["latest"]},
        }, fallback=True)
        monkeypatch.setattr("backend.detect.make_registry_client", lambda: REGISTRY)
        scan(client)  # 基线
        # 远端发布新内容
        REGISTRY.specs["app:latest"]["digest"] = "sha256:" + "9" * 64
        scan(client)
        assert db.query_one("SELECT * FROM containers WHERE name='web'")["update_available"] == 1
        engine = UpdateEngine(client, health_wait_sec=0)
        plan = type("P", (), {"to_update": {"web"}, "affected": set(), "order": ["web"]})()
        results = engine.execute(plan)
        assert results[0].result == "updated"
        # compose_up 被调用（而非裸 docker run）
        assert "compose_up:app/web" in client.event_log
        # 配置保真：端口/挂载/标签/运行状态保留
        c = client.inspect("web")
        assert c["config"]["ports"] == {"8080/tcp": [{"HostIp": "", "HostPort": "8080"}]}
        assert c["config"]["binds"] == ["/opt/app/data:/data"]
        assert c["labels"]["com.docker.compose.project"] == "app"
        assert c["running"] is True

    def test_compose_update_rollback_keeps_config(self, monkeypatch):
        """compose 更新失败 → 自动回滚仍用完整配置重建（端口/挂载保留）。"""
        client = MockDockerClient()
        self._seed_compose_container(client)
        REGISTRY = StaticRegistrySource({
            "app:latest": {"digest": _fake_digest("app:latest"), "tags": ["latest"]},
        }, fallback=True)
        monkeypatch.setattr("backend.detect.make_registry_client", lambda: REGISTRY)
        scan(client)
        REGISTRY.specs["app:latest"]["digest"] = "sha256:" + "9" * 64
        scan(client)
        client.mark_unhealthy_once("web")  # 新版本启动失败剧本
        engine = UpdateEngine(client, health_wait_sec=5)
        plan = type("P", (), {"to_update": {"web"}, "affected": set(), "order": ["web"]})()
        results = engine.execute(plan)
        assert results[0].result == "rolled_back"
        c = client.inspect("web")
        assert c["config"]["ports"] == {"8080/tcp": [{"HostIp": "", "HostPort": "8080"}]}
        assert c["config"]["binds"] == ["/opt/app/data:/data"]
        assert c["running"] is True

    def test_plain_container_full_config_recreate(self, monkeypatch):
        """非 compose 容器：更新重建后完整配置保留（此前会丢端口/挂载）。"""
        client = MockDockerClient()
        client.seed_container("solo", "app:latest")
        client._containers["solo"]["config"] = {
            "env": ["K=V"], "cmd": [], "restart": "always",
            "ports": {"9090/tcp": [{"HostIp": "", "HostPort": "9090"}]},
            "binds": ["/data:/data"],
        }
        REGISTRY = StaticRegistrySource({
            "app:latest": {"digest": _fake_digest("app:latest"), "tags": ["latest"]},
        }, fallback=True)
        monkeypatch.setattr("backend.detect.make_registry_client", lambda: REGISTRY)
        scan(client)
        REGISTRY.specs["app:latest"]["digest"] = "sha256:" + "8" * 64
        scan(client)
        engine = UpdateEngine(client, health_wait_sec=0)
        plan = type("P", (), {"to_update": {"solo"}, "affected": set(), "order": ["solo"]})()
        results = engine.execute(plan)
        assert results[0].result == "updated"
        c = client.inspect("solo")
        assert c["config"]["ports"] == {"9090/tcp": [{"HostIp": "", "HostPort": "9090"}]}
        assert c["config"]["binds"] == ["/data:/data"]
        assert c["config"]["restart"] == "always"
        assert c["running"] is True


class TestUpdateVersionRefresh:
    def test_local_version_aligned_to_remote(self, monkeypatch):
        """更新成功后 local_version 对齐检测时解析的目标版本（修"更新后仍显示旧版本号"）。"""
        client = MockDockerClient()
        client.seed_container("vapp", "verapp:latest")
        REGISTRY = StaticRegistrySource({
            "verapp:latest": {"digest": _fake_digest("verapp:latest"), "tags": ["latest"]},
        }, fallback=True)
        monkeypatch.setattr("backend.detect.make_registry_client", lambda: REGISTRY)
        scan(client)
        REGISTRY.specs["verapp:latest"]["digest"] = "sha256:" + "7" * 64
        REGISTRY.specs["verapp:latest"]["version"] = "3.3.0"
        scan(client)
        row = db.query_one("SELECT * FROM containers WHERE name='vapp'")
        assert row["remote_version"] == "3.3.0"
        # 更新成功（compose_up/重建后镜像无版本标签 → 靠 remote_version 对齐）
        from backend.engine import run_update

        out = run_update(client, names=["vapp"])
        assert out["containers"][0]["result"] == "updated"
        row = db.query_one("SELECT * FROM containers WHERE name='vapp'")
        assert row["local_version"] == "3.3.0"  # 界面 tag 行立即显示新版本号
        assert row["remote_version"] == ""
        assert row["update_available"] == 0
