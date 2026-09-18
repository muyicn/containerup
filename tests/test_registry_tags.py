"""Docker Hub tags digest 匹配：浮动 tag 版本号解析、缓存语义、多 tag 择新。

全部外呼均 monkeypatch _fetch_hub_versions，单元测试不发真实网络请求。
"""
from backend.registry import (
    _parse_hub_tags,
    _ver_key,
    plausible_version,
    version_by_digest,
)

A = "a" * 64
B = "b" * 64

# 模拟 hermes-web-ui 实测数据：latest ≡ v0.7.22（同 digest），v0.7.21 是旧版本
HUB_MAP = {
    A: ("v0.7.22", "2026-09-16T02:33:50Z"),
    B: ("v0.7.21", "2026-09-12T14:43:33Z"),
}


class TestPlausibleVersion:
    def test_version_like(self):
        assert plausible_version("1.13.0")
        assert plausible_version("v2.2.0")
        assert plausible_version("2026.09.18")
        assert plausible_version("1.0.0-beta.1")
        assert plausible_version("7")

    def test_non_version(self):
        assert not plausible_version("main")
        assert not plausible_version("latest")
        assert not plausible_version("beta")
        assert not plausible_version("")
        assert not plausible_version("dev")


class TestVerKey:
    def test_ordering(self):
        assert _ver_key("v0.7.22") > _ver_key("v0.7.21")
        assert _ver_key("1.13.0") > _ver_key("1.9.9")  # 数值比较非字符串
        assert _ver_key("2.0.0") > _ver_key("1.0.3")
        assert _ver_key("1.0.0") > _ver_key("1.0.0-beta.1")  # 正式版 > 预发布


class TestParseHubTags:
    def test_only_version_tags_mapped(self):
        payload = {"results": [
            {"name": "latest", "digest": "sha256:" + A, "last_updated": "2026-09-16T02:33:44Z"},
            {"name": "v0.7.22", "digest": "sha256:" + A, "last_updated": "2026-09-16T02:33:50Z"},
            {"name": "8d964022d700", "digest": "sha256:" + A, "last_updated": "2026-09-16T02:33:47Z"},
            {"name": "v0.7.21", "digest": "sha256:" + B, "last_updated": "2026-09-12T14:43:33Z"},
        ]}
        out = _parse_hub_tags(payload)
        assert out == {A: ("v0.7.22", "2026-09-16T02:33:50Z"), B: ("v0.7.21", "2026-09-12T14:43:33Z")}

    def test_same_digest_latest_push_wins(self):
        payload = {"results": [
            {"name": "1.0", "digest": "sha256:" + A, "last_updated": "2026-01-01T00:00:00Z"},
            {"name": "v1.0.1", "digest": "sha256:" + A, "last_updated": "2026-01-01T00:00:00Z"},
        ]}
        out = _parse_hub_tags(payload)
        assert out[A][0] == "v1.0.1"  # 同推送时间取版本号更大的


class TestVersionByDigest:
    def test_match_and_cache(self, monkeypatch):
        calls = []

        def fake_fetch(repo):
            calls.append(repo)
            return dict(HUB_MAP)

        monkeypatch.setattr("backend.registry._fetch_hub_versions", fake_fetch)
        spec = "ekkoye8888/hermes-web-ui:latest"
        assert version_by_digest(spec, "sha256:" + B) == "v0.7.21"
        assert version_by_digest(spec, "sha256:" + A) == "v0.7.22"
        assert len(calls) == 1  # 第二次命中缓存不再拉列表
        # 重复查询缓存命中
        assert version_by_digest(spec, "sha256:" + A) == "v0.7.22"
        assert len(calls) == 1

    def test_no_match_cached_with_ttl(self, monkeypatch):
        calls = []

        def fake_fetch(repo):
            calls.append(1)
            return dict(HUB_MAP)

        monkeypatch.setattr("backend.registry._fetch_hub_versions", fake_fetch)
        spec = "ekkoye8888/hermes-web-ui:latest"
        unknown = "c" * 64
        assert version_by_digest(spec, "sha256:" + unknown) == ""
        assert version_by_digest(spec, "sha256:" + unknown) == ""  # 1h 内空缓存命中
        assert len(calls) == 1

    def test_fetch_error_not_cached(self, monkeypatch):
        n = {"c": 0}

        def flaky(repo):
            n["c"] += 1
            if n["c"] == 1:
                raise RuntimeError("network")
            return dict(HUB_MAP)

        monkeypatch.setattr("backend.registry._fetch_hub_versions", flaky)
        spec = "ekkoye8888/hermes-web-ui:latest"
        assert version_by_digest(spec, "sha256:" + A) == ""  # 失败不缓存
        assert version_by_digest(spec, "sha256:" + A) == "v0.7.22"  # 下轮自愈
        assert n["c"] == 2

    def test_non_docker_hub_skipped(self, monkeypatch):
        def boom(repo):
            raise AssertionError("should not fetch")

        monkeypatch.setattr("backend.registry._fetch_hub_versions", boom)
        assert version_by_digest("ghcr.io/owner/app:latest", "sha256:" + A) == ""
        assert version_by_digest("registry.example.com/app:latest", "sha256:" + A) == ""

    def test_invalid_digest(self, monkeypatch):
        monkeypatch.setattr("backend.registry._fetch_hub_versions", lambda r: {})
        assert version_by_digest("nginx:latest", "") == ""
        assert version_by_digest("nginx:latest", None) == ""
        assert version_by_digest("nginx:latest", "sha256:short") == ""
