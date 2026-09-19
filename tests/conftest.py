"""pytest 全局夹具：临时数据库 + 干净状态。

关键：VT_DB_PATH 必须在 import backend.* 之前设置（backend.config 模块级读取）。
conftest 在收集阶段先于测试模块执行，天然满足该顺序。
"""
import os
import pathlib
import sys
import tempfile

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

_TMP = tempfile.mkdtemp(prefix="vt-test-")
os.environ["VT_DB_PATH"] = os.path.join(_TMP, "test.db")
os.environ["VT_DEMO_MODE"] = "0"
os.environ["VT_DEMO_SEED"] = "0"
os.environ["VT_JWT_SECRET"] = "test-secret-key-for-pytest-only-32bytes"
os.environ["VT_REGISTRY_TIMEOUT"] = "5"

import pytest  # noqa: E402


@pytest.fixture(autouse=True)
def fresh_db():
    """每个测试函数：清空全部表并重建，清空进程内状态。"""
    from backend import db as dbm
    from backend import auth as auth_m

    with dbm._LOCK:
        if dbm._CONN is not None:
            dbm._CONN.executescript(
                """
                DROP TABLE IF EXISTS settings; DROP TABLE IF EXISTS containers;
                DROP TABLE IF EXISTS watches; DROP TABLE IF EXISTS notifications;
                DROP TABLE IF EXISTS container_versions; DROP TABLE IF EXISTS tag_version_cache;
                DROP TABLE IF EXISTS channels; DROP TABLE IF EXISTS users;
                DROP TABLE IF EXISTS scans; DROP TABLE IF EXISTS jobs;
                """
            )
            dbm._CONN.commit()
    dbm.init_db()
    auth_m._FAILS.clear()
    yield


@pytest.fixture(autouse=True)
def _no_network_version_lookup(monkeypatch):
    """默认禁止版本号解析真实外呼（Hub tags）；需要网络行为的用例自行 patch 覆盖。"""
    monkeypatch.setattr("backend.registry._fetch_hub_versions", lambda repo: (_ for _ in ()).throw(
        RuntimeError("network disabled in tests")))
    monkeypatch.setattr("backend.registry.version_by_digest", lambda spec, digest: "")
    import backend.detect as _detect

    monkeypatch.setattr(_detect, "version_by_digest", lambda spec, digest: "")
