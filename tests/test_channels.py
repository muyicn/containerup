"""通知渠道投递格式测试：企业微信（wecom）markdown 格式与 errcode 判定。"""
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from backend import db, notify


class EchoHandler(BaseHTTPRequestHandler):
    """记录请求体并按 kind 返回企业微信式响应。"""

    received: list[dict] = []

    def do_POST(self):
        length = int(self.headers.get("Content-Length", 0))
        body = json.loads(self.rfile.read(length) or b"{}")
        EchoHandler.received.append(body)
        # 模拟企业微信响应：errcode 0 = 成功
        resp = json.dumps({"errcode": 0, "errmsg": "ok"}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(resp)))
        self.end_headers()
        self.wfile.write(resp)

    def log_message(self, *args):
        pass


@pytest.fixture()
def wecom_server():
    EchoHandler.received = []
    server = ThreadingHTTPServer(("127.0.0.1", 0), EchoHandler)
    t = threading.Thread(target=server.serve_forever, daemon=True)
    t.start()
    yield f"http://127.0.0.1:{server.server_address[1]}"
    server.shutdown()


class TestWecomChannel:
    def _add_channel(self, url):
        with db.tx() as conn:
            conn.execute(
                "INSERT INTO channels(kind, name, url, enabled) VALUES('wecom','test',?,1)",
                (url,),
            )

    def test_wecom_sends_markdown(self, wecom_server):
        self._add_channel(wecom_server)
        ok = notify.deliver("测试消息内容", {"count": 1})
        assert list(ok.values()) == [True]
        assert len(EchoHandler.received) == 1
        body = EchoHandler.received[0]
        assert body["msgtype"] == "markdown"
        assert "测试消息内容" in body["markdown"]["content"]
        assert "容器守望者" in body["markdown"]["content"]

    def test_wecom_errcode_nonzero_is_failure(self, wecom_server):
        """errcode != 0 时投递应判失败（企业微信语义）。"""
        self._add_channel(wecom_server + "?fail=1")

        # 覆盖响应：注入失败剧本
        orig = EchoHandler.do_POST

        def fail_post(self):
            length = int(self.headers.get("Content-Length", 0))
            self.rfile.read(length)
            resp = json.dumps({"errcode": 93000, "errmsg": "invalid webhook"}).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(resp)))
            self.end_headers()
            self.wfile.write(resp)

        EchoHandler.do_POST = fail_post
        try:
            ok = notify.deliver("x", {})
            assert list(ok.values()) == [False]
        finally:
            EchoHandler.do_POST = orig

    def test_wecom_test_endpoint(self, wecom_server):
        assert notify.test_channel(wecom_server, "wecom") is True

    def test_wecom_markdown_v2_with_logo(self, wecom_server):
        """配置 public_base_url 后：切 markdown_v2 并内嵌应用 logo 图片地址。"""
        db.setting_set("public_base_url", "http://192.168.1.10:9412/")
        self._add_channel(wecom_server)
        ok = notify.deliver("测试消息内容", {"count": 1})
        assert list(ok.values()) == [True]
        body = EchoHandler.received[0]
        assert body["msgtype"] == "markdown_v2"
        content = body["markdown_v2"]["content"]
        assert "![logo](http://192.168.1.10:9412/api/public/logo.png)" in content
        assert "容器守望者通知" in content
        assert "测试消息内容" in content
        db.setting_set("public_base_url", "")


class TestDingtalkChannel:
    def _add_channel(self, url):
        with db.tx() as conn:
            conn.execute(
                "INSERT INTO channels(kind, name, url, enabled) VALUES('dingtalk','test-dt',?,1)",
                (url,),
            )

    def test_dingtalk_sends_text(self, wecom_server):
        self._add_channel(wecom_server)
        ok = notify.deliver("钉钉消息内容", {"count": 1})
        assert list(ok.values()) == [True]
        body = EchoHandler.received[0]
        assert body["msgtype"] == "text"
        assert body["text"]["content"] == "钉钉消息内容"

    def test_dingtalk_errcode_nonzero_fails(self, wecom_server):
        self._add_channel(wecom_server)
        orig = EchoHandler.do_POST

        def fail_post(self):
            length = int(self.headers.get("Content-Length", 0))
            self.rfile.read(length)
            resp = json.dumps({"errcode": 300001, "errmsg": "token is not valid"}).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(resp)))
            self.end_headers()
            self.wfile.write(resp)

        EchoHandler.do_POST = fail_post
        try:
            ok = notify.deliver("x", {})
            assert list(ok.values()) == [False]
        finally:
            EchoHandler.do_POST = orig


class TestFeishuChannel:
    def _add_channel(self, url):
        with db.tx() as conn:
            conn.execute(
                "INSERT INTO channels(kind, name, url, enabled) VALUES('feishu','test-fs',?,1)",
                (url,),
            )

    def test_feishu_sends_msg_type_and_content(self, wecom_server):
        self._add_channel(wecom_server)
        # 模拟飞书成功响应
        orig = EchoHandler.do_POST

        def fs_post(self):
            length = int(self.headers.get("Content-Length", 0))
            body = json.loads(self.rfile.read(length) or b"{}")
            EchoHandler.received.append(body)
            resp = json.dumps({"code": 0, "msg": "success"}).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(resp)))
            self.end_headers()
            self.wfile.write(resp)

        EchoHandler.do_POST = fs_post
        try:
            ok = notify.deliver("飞书消息内容", {"count": 1})
            assert list(ok.values()) == [True]
            body = EchoHandler.received[0]
            assert body["msg_type"] == "text"
            assert body["content"]["text"] == "飞书消息内容"
        finally:
            EchoHandler.do_POST = orig

    def test_feishu_code_nonzero_fails(self, wecom_server):
        self._add_channel(wecom_server)
        orig = EchoHandler.do_POST

        def fail_post(self):
            length = int(self.headers.get("Content-Length", 0))
            self.rfile.read(length)
            resp = json.dumps({"code": 19001, "msg": "bad request"}).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(resp)))
            self.end_headers()
            self.wfile.write(resp)

        EchoHandler.do_POST = fail_post
        try:
            ok = notify.deliver("x", {})
            assert list(ok.values()) == [False]
        finally:
            EchoHandler.do_POST = orig

