"""检测引擎：镜像引用解析 + Registry API 客户端 + 双检测模式。

双模式（PRD 3.1 / Vigil 语义）：
- Digest-Only：浮动 tag（latest/nightly/...），仅比对当前 tag 远端摘要。
- Pin-Watch：版本号 tag（8.4.5 / v3.9 / 1.2.3-alpine），比对摘要 + 巡检仓库 tag 列表，
  出现更高的版本号 tag → new-tag（可选更新）。
Registry 协议栈（PRD 3.1 / Tugtainer 语义）：
- HEAD /v2/{repo}/manifests/{tag} + If-None-Match 304 协商
- 401 → WWW-Authenticate Bearer token 流（realm 合法性校验）
- docker.io 归一化 registry-1.docker.io + library/ 前缀
- INSECURE_REGISTRIES（http 回退）+ REGISTRY_MIRROR 全局改发
"""
import re
import threading
import time
from dataclasses import dataclass
from typing import Optional
from urllib.parse import urlencode, urlparse

import httpx

from backend.config import CONFIG

FLOATING_TAGS = {"latest", "nightly", "dev", "canary", "beta", "edge", "stable", "master", "main", "test"}
# 数字开头的版本号 tag（宽松判定：8.4.5 / v3.9 / 1.2.3-alpine）
VERSION_TAG_RE = re.compile(r"^v?\d+(\.\d+)*(-[a-zA-Z0-9._]+)*$")

MANIFEST_ACCEPT = ",".join(
    [
        "application/vnd.oci.image.index.v1+json",
        "application/vnd.docker.distribution.manifest.list.v2+json",
        "application/vnd.oci.image.manifest.v1+json",
        "application/vnd.docker.distribution.manifest.v2+json",
    ]
)


def parse_image_spec(spec: str) -> tuple[str, str, str]:
    """镜像引用 → (registry, repo, tag)。规则对齐上游 Tugtainer check_util。"""
    spec = spec.strip()
    if not spec:
        raise ValueError("empty image spec")
    # 排除 @digest 引用（不支持按摘要更新）
    if "@" in spec:
        raise ValueError("digest refs are not supported for check")
    tag = "latest"
    if ":" in spec and "/" in spec and spec.rfind(":") > spec.rfind("/"):
        spec, tag = spec.rsplit(":", 1)
    elif ":" in spec and spec.count(":") == 1 and "/" not in spec:
        spec, tag = spec.rsplit(":", 1)
    parts = spec.split("/")
    if "." in parts[0] or ":" in parts[0] or parts[0] == "localhost":
        registry = parts[0]
        repo = "/".join(parts[1:])
    else:
        registry = "registry-1.docker.io"
        repo = spec
    if registry in {"docker.io", "index.docker.io"}:
        registry = "registry-1.docker.io"
    if registry == "registry-1.docker.io" and "/" not in repo:
        repo = f"library/{repo}"
    if not repo:
        raise ValueError(f"invalid image spec: {spec}")
    return registry, repo, tag


def detect_mode(tag: str, override: str = "auto") -> str:
    """auto → 按 tag 形态分流；显式覆写优先。"""
    if override in ("digest-only", "pin-watch"):
        return override
    tag_l = tag.lower()
    if tag_l in FLOATING_TAGS or not VERSION_TAG_RE.match(tag_l):
        return "digest-only"
    return "pin-watch"


def is_version_tag(tag: str) -> bool:
    return bool(VERSION_TAG_RE.match(tag.lower()))


def _version_key(tag: str) -> list:
    """语义化版本排序键：8.4.5 → [8,4,5]；非版本 tag 排最后。"""
    m = re.match(r"^v?(\d+(?:\.\d+)*)", tag)
    if not m:
        return [-1]
    return [int(x) for x in m.group(1).split(".")]


def newer_version_tags(current_tag: str, all_tags: list[str]) -> list[str]:
    """仓库 tag 列表中比当前锁定版本更高的版本号 tag（升序返回）。"""
    if not is_version_tag(current_tag):
        return []
    cur = _version_key(current_tag)
    higher = [t for t in all_tags if is_version_tag(t) and _version_key(t) > cur]
    return sorted(set(higher), key=_version_key)


def _is_insecure(registry: str) -> bool:
    insecure = CONFIG.INSECURE_REGISTRIES
    if not insecure:
        return False
    target = registry.split(":")[0].lower()
    for line in insecure.split(","):
        h = line.strip().split(":")[0].lower()
        if h and h == target:
            return True
    return False


def _validate_bearer_realm(realm: str, insecure: bool) -> None:
    parsed = urlparse(realm)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise ValueError(f"invalid bearer realm: {realm}")
    if parsed.scheme == "http" and not insecure:
        raise ValueError("http bearer realm only allowed for insecure registries")


@dataclass
class RegistryResult:
    digest: Optional[str]
    newer_tags: list[str]


class RegistryClient:
    """同步 Registry API 客户端（连接池复用 + Bearer token 缓存，支持多线程并发检测）。"""

    _TOKEN_TTL_CAP = 240.0  # Docker Hub expires_in 一般 300s，留余量

    def __init__(self, timeout: Optional[float] = None):
        self.timeout = timeout or CONFIG.REGISTRY_TIMEOUT_SEC
        self._http: Optional[httpx.Client] = None
        self._token_cache: dict[tuple[str, str, str], tuple[str, float]] = {}
        self._token_lock = threading.Lock()

    def _client(self) -> httpx.Client:
        """进程内复用连接池：省去每容器/每请求的 TCP+TLS 握手开销。"""
        if self._http is None:
            self._http = httpx.Client(timeout=self.timeout)
        return self._http

    def close(self) -> None:
        if self._http is not None:
            self._http.close()
            self._http = None

    # ---------- 对外主入口 ----------

    def check(self, spec: str, local_digest: Optional[str], mode: str) -> RegistryResult:
        """按检测模式检查镜像，返回远端摘要 + 更高的新版本 tag 列表。"""
        registry, repo, tag = parse_image_spec(spec)
        digest = self.remote_digest(registry, repo, tag, local_digest)
        newer: list[str] = []
        if mode == "pin-watch":
            try:
                tags = self.list_tags(registry, repo)
                newer = newer_version_tags(tag, tags)
            except Exception:
                # tag 列表失败不影响摘要检测结果（继承上游 best-effort 语义）
                newer = []
        return RegistryResult(digest=digest, newer_tags=newer)

    # 版本标签（OCI 标准 + label-schema 约定）
    _VERSION_LABELS = ("org.opencontainers.image.version", "org.label-schema.version")

    def remote_version(self, spec: str) -> str:
        """取远端镜像 config 中的版本标签（org.opencontainers.image.version 等）。

        流程：GET manifest（multi-arch 时按 amd64/linux 选子 manifest）
        → config.digest → GET config blob → Labels。
        blob 常见 307 重定向到 CDN（Docker Hub）：手动跟随且不带 Authorization
        （预签名地址带 Authorization 反而会被拒）。失败一律返回空串。
        """
        registry, repo, tag = parse_image_spec(spec)
        insecure = _is_insecure(registry)
        base = self._base_url(registry, insecure)
        client = self._client()
        headers = {"Accept": MANIFEST_ACCEPT}
        url = f"{base}/v2/{repo}/manifests/{tag}"
        resp = client.get(url, headers=headers)
        if resp.status_code in (401, 403):
            auth_header = resp.headers.get("www-authenticate", "")
            if "Bearer" in auth_header:
                headers["Authorization"] = f"Bearer {self._bearer_token(client, auth_header, repo, insecure)}"
            resp = client.get(url, headers=headers)
        if resp.status_code != 200:
            return ""
        try:
            manifest = resp.json() or {}
            # multi-arch（manifest list / image index）：按本机平台选子 manifest 再拉
            if "manifests" in manifest:
                target = None
                for m in manifest.get("manifests") or []:
                    p = m.get("platform") or {}
                    if p.get("architecture") == "amd64" and p.get("os") == "linux":
                        target = m
                        break
                if not target:
                    return ""
                sub_url = f"{base}/v2/{repo}/manifests/{target['digest']}"
                sresp = client.get(sub_url, headers=headers)
                if sresp.status_code != 200:
                    return ""
                manifest = sresp.json() or {}
            cfg_digest = (manifest.get("config") or {}).get("digest") or ""
            if not cfg_digest:
                return ""  # schemaVersion 1 或异常 manifest：无 config
            blob_url = f"{base}/v2/{repo}/blobs/{cfg_digest}"
            bresp = client.get(blob_url, headers=headers)
            if bresp.status_code in (301, 302, 303, 307, 308):
                loc = bresp.headers.get("location", "")
                if loc:
                    bresp = client.get(loc)  # CDN 预签名地址：不带认证头
            if bresp.status_code != 200:
                return ""
            labels = ((bresp.json() or {}).get("config") or {}).get("Labels") or {}
            for key in self._VERSION_LABELS:
                v = labels.get(key)
                if v:
                    return str(v)[:64]
        except Exception:
            return ""
        return ""

    # ---------- 协议层 ----------

    def _base_url(self, registry: str, insecure: bool) -> str:
        scheme = "http" if insecure else "https"
        host = registry
        if CONFIG.REGISTRY_MIRROR:
            host = CONFIG.REGISTRY_MIRROR.rstrip("/")
            scheme = "https"
        return f"{scheme}://{host}"

    def _do_head(self, client: httpx.Client, url: str, headers: dict) -> tuple[int, dict]:
        resp = client.head(url, headers=headers, follow_redirects=False)
        return resp.status_code, dict(resp.headers)

    def remote_digest(
        self, registry: str, repo: str, tag: str, local_digest: Optional[str]
    ) -> Optional[str]:
        """HEAD manifest，支持 Bearer token 流。返回当前远端摘要或 None。"""
        insecure = _is_insecure(registry)
        base = self._base_url(registry, insecure)
        url = f"{base}/v2/{repo}/manifests/{tag}"
        headers = {"Accept": MANIFEST_ACCEPT}
        # 不使用 If-None-Match 304 协商：不同 registry 实现对 ETag 格式处理不一致，
        # 可能导致旧 digest 误命中 304 而漏检更新；始终 GET 完整摘要确保正确性
        client = self._client()
        status, resp_headers = self._do_head(client, url, headers)
        if status in (401, 403):
            auth_header = resp_headers.get("www-authenticate", "")
            if "Bearer" in auth_header:
                headers["Authorization"] = f"Bearer {self._bearer_token(client, auth_header, repo, insecure)}"
            status, resp_headers = self._do_head(client, url, headers)
        if status == 304:
            return local_digest  # 不应到达（未发 If-None-Match），保留安全回退
        if status != 200:
            raise RuntimeError(f"registry {registry} returned {status} for {repo}:{tag}")
        return (
            resp_headers.get("docker-content-digest")
            or resp_headers.get("etag")
            or None
        )

    def list_tags(self, registry: str, repo: str) -> list[str]:
        """GET /v2/{repo}/tags/list（Pin-Watch 巡检）。"""
        insecure = _is_insecure(registry)
        base = self._base_url(registry, insecure)
        url = f"{base}/v2/{repo}/tags/list"
        headers: dict[str, str] = {}
        client = self._client()
        status, resp_headers = self._do_head(client, url, headers)
        if status in (401, 403):
            auth_header = resp_headers.get("www-authenticate", "")
            if "Bearer" in auth_header:
                headers["Authorization"] = f"Bearer {self._bearer_token(client, auth_header, repo, insecure)}"
        resp = client.get(url, headers=headers)
        if resp.status_code != 200:
            raise RuntimeError(f"registry {registry} tags list returned {resp.status_code}")
        return resp.json().get("tags", [])

    def _bearer_token(self, client: httpx.Client, auth_header: str, repo: str, insecure: bool) -> str:
        """解析 WWW-Authenticate → realm 获取 token（带缓存：同 scope 复用，省去每容器重复鉴权）。"""
        parts = auth_header.replace("Bearer ", "")
        items: dict[str, str] = {}
        for item in parts.replace('"', "").split(","):
            k, _, v = item.partition("=")
            items[k.strip()] = v.strip()
        realm = items.get("realm")
        if not realm:
            raise ValueError("bearer realm missing from www-authenticate")
        _validate_bearer_realm(realm, insecure)
        params = {
            "service": items.get("service", ""),
            "scope": items.get("scope") or f"repository:{repo}:pull",
        }
        cache_key = (realm, params["service"], params["scope"])
        now = time.time()
        with self._token_lock:
            hit = self._token_cache.get(cache_key)
            if hit and hit[1] > now:
                return hit[0]
        resp = client.get(f"{realm}?{urlencode(params)}")
        resp.raise_for_status()
        data = resp.json()
        token = data.get("token") or data.get("access_token") or ""
        if token:
            try:
                ttl = min(float(data.get("expires_in") or 300), self._TOKEN_TTL_CAP)
            except (TypeError, ValueError):
                ttl = self._TOKEN_TTL_CAP
            with self._token_lock:
                self._token_cache[cache_key] = (token, now + ttl)
        return token

    def sleep_throttle(self) -> None:
        if CONFIG.REGISTRY_REQ_DELAY_SEC > 0:
            time.sleep(CONFIG.REGISTRY_REQ_DELAY_SEC)
