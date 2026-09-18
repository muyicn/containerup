"""镜像版本号 GitHub 溯源。

背景：版本号的权威来源是镜像构建时写入的 OCI 标签（org.opencontainers.image.version），
但不少 CI 只写分支名（main）或干脆不写——而镜像标注里通常还有源码仓库
（org.opencontainers.image.source）与构建 commit（org.opencontainers.image.revision，
GitHub Actions metadata-action 默认写入）。本模块用这两个标注到 GitHub 反查
指向该 commit 的 tag（如 v2.2.0）作为版本号，与项目真实发布版本对应。

设计约束：
- 持久缓存（tag_version_cache 表）：commit → tag 映射不变，首次查询后零外呼；
- 确认无匹配也缓存（空串），避免每轮扫描重复查询同一 commit；
- 限流/网络异常不缓存——下轮扫描自愈重试；
- 仅支持 github.com 源；匿名 API（60 次/小时/IP），靠缓存兜底；
- revision 必须形如 commit sha（7-40 位十六进制），防止拿分支名乱查。
"""
import logging
import re
from typing import Any, Optional

import httpx

from backend import db

logger = logging.getLogger("github_versions")

_TIMEOUT = 10.0  # GitHub API 国内直连常达 7-8s，过紧会首次溯源必超时（仅首次外呼，缓存兜底）
_MAX_PAGES = 3

# github.com/owner/repo（兼容 .git 后缀、/tree/xx 等子路径、git@ 前缀）
_SOURCE_RE = re.compile(r"github\.com[/:]([A-Za-z0-9_.-]+)/([A-Za-z0-9_.-]+?)(?:\.git)?(?:[/?#].*)?$")
# revision 必须像 commit sha（7-40 位十六进制）
_REV_RE = re.compile(r"^[0-9a-f]{7,40}$", re.IGNORECASE)
# 像版本号的值：1.0 / v2.2.0 / 2026.09.18 / 1.0.0-beta.1（分支名 main/master/dev 不匹配）
_VERSION_RE = re.compile(r"^v?\d+(\.\d+){0,3}([-+][0-9A-Za-z.-]+)?$", re.IGNORECASE)


def plausible_version(value: str) -> bool:
    """版本标签值是否像真实版本号（空串/分支名等不算）。"""
    return bool(value) and bool(_VERSION_RE.match(str(value).strip()))


def github_repo(source: str) -> str:
    """从源码仓库 URL 提取 owner/repo；非 GitHub 返回空串。"""
    if not source:
        return ""
    m = _SOURCE_RE.search(source.strip())
    if not m:
        return ""
    owner, repo = m.group(1), m.group(2)
    # 形如 github.com/orgs/xxx、github.com/topics/xxx 的非仓库 URL
    if owner.lower() in {"orgs", "apps", "topics", "features", "collections"}:
        return ""
    return f"{owner}/{repo}"


def _cache_get(repo: str, rev: str) -> Optional[str]:
    """返回 None=未缓存；str=已缓存结果（空串=确认无匹配）。"""
    row = db.query_one(
        "SELECT version FROM tag_version_cache WHERE repo=? AND revision=?", (repo, rev)
    )
    return None if row is None else (row["version"] or "")


def _cache_put(repo: str, rev: str, ver: str) -> None:
    with db.tx() as conn:
        conn.execute(
            "INSERT INTO tag_version_cache(repo, revision, version, updated_at) VALUES(?,?,?,?) "
            "ON CONFLICT(repo, revision) DO UPDATE SET "
            "version=excluded.version, updated_at=excluded.updated_at",
            (repo, rev, ver, db.now_iso()),
        )


def _fetch_tag(repo: str, rev: str) -> str:
    """遍历仓库 tags（最多 3 页 × 100），返回指向该 commit 的 tag 名；无匹配返回空串。

    GitHub API 对 annotated tag 已解引用，commit.sha 即提交 sha，前缀匹配短/完整 sha。
    非 200 视为失败抛异常（限流/网络），由调用方决定不缓存。
    """
    rev = rev.lower()
    with httpx.Client(
        timeout=_TIMEOUT,
        headers={"Accept": "application/vnd.github+json", "User-Agent": "containerup"},
    ) as client:
        for page in range(1, _MAX_PAGES + 1):
            resp = client.get(
                f"https://api.github.com/repos/{repo}/tags",
                params={"per_page": 100, "page": page},
            )
            if resp.status_code != 200:
                raise RuntimeError(f"github api {resp.status_code} for {repo} tags")
            tags = resp.json() or []
            for t in tags:
                sha = ((t.get("commit") or {}).get("sha") or "").lower()
                if sha and sha.startswith(rev):
                    return str(t.get("name") or "")[:64]
            if len(tags) < 100:
                break
    return ""


def version_for_revision(source: str, revision: str) -> str:
    """commit → GitHub tag（版本号）。查到/确认无匹配走缓存；网络失败返回空串不缓存。"""
    repo = github_repo(source or "")
    rev = (revision or "").strip().lower()
    if not repo or not _REV_RE.match(rev):
        return ""
    cached = _cache_get(repo, rev)
    if cached is not None:
        return cached
    try:
        ver = _fetch_tag(repo, rev)
    except Exception as e:  # 限流/网络抖动：不缓存，下轮自愈
        logger.info("github tag lookup failed for %s@%s: %s", repo, rev[:12], e)
        return ""
    _cache_put(repo, rev, ver)
    return ver


def resolve_version(labels: Any) -> str:
    """统一入口：优先镜像版本标签（值像版本号时），否则 source+revision 溯源 GitHub。

    返回最终展示版本号；都拿不到返回空串（调用方回退摘要显示）。
    溯源失败时保留原始标签值（可能是 main 这类分支名，仍比空信息多）。
    """
    labels = labels or {}
    raw = ""
    for k in ("org.opencontainers.image.version", "org.label-schema.version"):
        v = (labels.get(k) or "").strip()
        if v:
            raw = str(v)[:64]
            break
    if plausible_version(raw):
        return raw
    gh = version_for_revision(
        labels.get("org.opencontainers.image.source"),
        labels.get("org.opencontainers.image.revision"),
    )
    return gh or raw
