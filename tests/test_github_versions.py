"""GitHub 版本号溯源：commit → tag 反查、缓存语义、resolve_version 优先级。

全部外呼均 monkeypatch _fetch_tag，单元测试不发真实网络请求。
"""
from backend.github_versions import (
    github_repo,
    plausible_version,
    resolve_version,
    version_for_revision,
)


class TestPlausibleVersion:
    def test_version_like_values(self):
        assert plausible_version("1.13.0")
        assert plausible_version("v2.2.0")
        assert plausible_version("2026.09.18")
        assert plausible_version("1.0.0-beta.1")
        assert plausible_version("7")

    def test_non_version_values(self):
        assert not plausible_version("main")
        assert not plausible_version("master")
        assert not plausible_version("")
        assert not plausible_version("latest")
        assert not plausible_version("dev")


class TestGithubRepo:
    def test_parse_common_urls(self):
        assert github_repo("https://github.com/muyicn/ctyun-dashboard") == "muyicn/ctyun-dashboard"
        assert github_repo("https://github.com/muyicn/ctyun-dashboard.git") == "muyicn/ctyun-dashboard"
        assert github_repo("git@github.com:muyicn/ctyun-dashboard.git") == "muyicn/ctyun-dashboard"
        assert github_repo("https://github.com/muyicn/ctyun-dashboard/tree/main") == "muyicn/ctyun-dashboard"

    def test_non_github_or_invalid(self):
        assert github_repo("https://gitlab.com/a/b") == ""
        assert github_repo("") == ""
        assert github_repo("https://github.com/orgs/acme") == ""


class TestVersionForRevision:
    def test_lookup_then_cache_hit(self, monkeypatch):
        calls = []

        def fake_fetch(repo, rev):
            calls.append((repo, rev))
            return "v2.2.0"

        monkeypatch.setattr("backend.github_versions._fetch_tag", fake_fetch)
        src = "https://github.com/muyicn/ctyun-dashboard"
        rev = "adf9a5fc2f41a6e605daa35fc52cceaa7082d4f2"
        assert version_for_revision(src, rev) == "v2.2.0"
        assert calls == [("muyicn/ctyun-dashboard", rev)]
        # 缓存命中：不再外呼
        assert version_for_revision(src, rev) == "v2.2.0"
        assert len(calls) == 1

    def test_no_match_cached_as_empty(self, monkeypatch):
        calls = []

        def fake_fetch(repo, rev):
            calls.append(1)
            return ""

        monkeypatch.setattr("backend.github_versions._fetch_tag", fake_fetch)
        src = "https://github.com/muyicn/containerup"
        assert version_for_revision(src, "1234567890abcdef") == ""
        # 确认无匹配也缓存（空串），第二次不再外呼
        assert version_for_revision(src, "1234567890abcdef") == ""
        assert len(calls) == 1

    def test_network_error_not_cached(self, monkeypatch):
        n = {"c": 0}

        def flaky(repo, rev):
            n["c"] += 1
            if n["c"] == 1:
                raise RuntimeError("rate limited")
            return "v1.0.0"

        monkeypatch.setattr("backend.github_versions._fetch_tag", flaky)
        src = "https://github.com/muyicn/containerup"
        assert version_for_revision(src, "abcdef1234567890") == ""  # 失败不缓存
        assert version_for_revision(src, "abcdef1234567890") == "v1.0.0"  # 下轮重试成功
        assert n["c"] == 2

    def test_invalid_inputs_short_circuit(self):
        assert version_for_revision("", "adf9a5f") == ""
        assert version_for_revision("https://github.com/a/b", "") == ""
        assert version_for_revision("https://github.com/a/b", "main") == ""  # 分支名不是 sha
        assert version_for_revision("https://github.com/a/b", "xyz") == ""  # 非 hex
        assert version_for_revision("https://gitlab.com/a/b", "adf9a5f") == ""


class TestResolveVersion:
    def test_plausible_label_wins(self, monkeypatch):
        def boom(s, r):
            raise AssertionError("should not lookup")

        monkeypatch.setattr("backend.github_versions.version_for_revision", boom)
        assert resolve_version({"org.opencontainers.image.version": "1.13.0"}) == "1.13.0"
        assert resolve_version({"org.label-schema.version": "v2.0.2"}) == "v2.0.2"

    def test_branch_name_resolved_via_github(self, monkeypatch):
        monkeypatch.setattr(
            "backend.github_versions.version_for_revision", lambda s, r: "v2.2.0"
        )
        labels = {
            "org.opencontainers.image.version": "main",
            "org.opencontainers.image.source": "https://github.com/muyicn/ctyun-dashboard",
            "org.opencontainers.image.revision": "adf9a5fc2f41",
        }
        assert resolve_version(labels) == "v2.2.0"

    def test_lookup_failure_keeps_raw_label(self, monkeypatch):
        monkeypatch.setattr(
            "backend.github_versions.version_for_revision", lambda s, r: ""
        )
        labels = {
            "org.opencontainers.image.version": "main",
            "org.opencontainers.image.source": "https://github.com/muyicn/ctyun-dashboard",
            "org.opencontainers.image.revision": "adf9a5fc2f41",
        }
        assert resolve_version(labels) == "main"  # 溯源失败保留原值

    def test_empty_labels(self):
        assert resolve_version({}) == ""
        assert resolve_version(None) == ""
