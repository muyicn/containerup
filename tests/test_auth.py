"""认证测试：bcrypt + JWT + 登录限流（5 次失败锁 15 分钟）。"""
import time

import jwt as pyjwt
import pytest

from backend import auth


class TestPassword:
    def test_hash_and_verify(self):
        h = auth.hash_password("s3cret-pass")
        assert h != "s3cret-pass"
        assert auth.verify_password("s3cret-pass", h)
        assert not auth.verify_password("wrong", h)

    def test_verify_garbage_hash(self):
        assert auth.verify_password("x", "not-a-hash") is False

    def test_set_password_updates_existing_user(self):
        auth.set_password("admin", "first-password")
        auth.set_password("admin", "second-password")
        u = auth.get_user()
        assert u["username"] == "admin"
        assert auth.verify_password("second-password", u["password_hash"])


class TestToken:
    def test_create_and_verify(self):
        auth.set_password("admin", "password123")
        token = auth.create_token("admin")
        assert auth.verify_token(token)

    def test_tampered_token_rejected(self):
        token = auth.create_token("admin")
        assert not auth.verify_token(token + "x")

    def test_expired_token_rejected(self):
        token = pyjwt.encode(
            {"sub": "admin", "type": "access", "iat": 0, "exp": 1},
            auth.secret_key(),
            algorithm="HS256",
        )
        assert not auth.verify_token(token)

    def test_wrong_type_rejected(self):
        token = pyjwt.encode(
            {"sub": "admin", "type": "refresh", "iat": int(time.time()), "exp": int(time.time()) + 9999},
            auth.secret_key(),
            algorithm="HS256",
        )
        assert not auth.verify_token(token)


class TestLoginThrottle:
    """PRD 3.9：连续 LOGIN_MAX_FAILS 次失败锁定 LOGIN_LOCK_SEC 秒。"""

    def test_lock_after_max_fails(self, monkeypatch):
        monkeypatch.setattr(auth.CONFIG, "LOGIN_MAX_FAILS", 3)
        monkeypatch.setattr(auth.CONFIG, "LOGIN_LOCK_SEC", 60)
        ip = "10.0.0.9"
        for _ in range(3):
            auth.login_fail(ip)
        assert auth.login_locked(ip) > 0  # 已锁定
        assert auth.login_locked("10.0.0.8") == 0  # 其它 IP 不受影响

    def test_success_clears_fails(self, monkeypatch):
        monkeypatch.setattr(auth.CONFIG, "LOGIN_MAX_FAILS", 3)
        ip = "10.0.0.7"
        auth.login_fail(ip)
        auth.login_fail(ip)
        auth.login_success(ip)
        assert auth.login_locked(ip) == 0

    def test_old_fails_expire(self, monkeypatch):
        monkeypatch.setattr(auth.CONFIG, "LOGIN_MAX_FAILS", 2)
        monkeypatch.setattr(auth.CONFIG, "LOGIN_LOCK_SEC", 1)
        ip = "10.0.0.6"
        auth.login_fail(ip)
        time.sleep(1.1)
        auth.login_fail(ip)  # 第一条已过期
        assert auth.login_locked(ip) == 0
