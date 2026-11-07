# -*- coding: utf-8 -*-
"""1.3.101：请求体是合法 JSON 但不是对象时，不能 500，也不能把异常类名当文案。

``b'[1,2]'`` / ``b'"hi"'`` / ``b'42'`` / ``b'true'`` / ``b'null'`` 都能被 json 解析，
所以 1.3.100 的 ``JSONDecodeError`` handler 接不到它们；路由里紧接着的
``body.get(...)`` 会在这些值上抛 ``AttributeError`` —— 两处直接逃成 500，六处被
``safe_error`` 包装成「操作失败（AttributeError）」。用例钉住这两条路都被挡住。
"""
import tempfile
import unittest
from pathlib import Path

import yaml
from fastapi.testclient import TestClient

from src.wechat_app import WeChatRuntime, create_app


# 8 个在 body 上直接 .get() 的路由：2 个原本 500，6 个原本泄漏异常类名。
TARGET_ROUTES = (
    '/api/persona-files/image',
    '/api/management/password',
    '/api/plugins/inspect',
    '/api/plugins/install',
    '/api/plugins/batch-inspect',
    '/api/plugins/batch-install',
    '/api/plugins/inspect-local',
    '/api/plugins/install-local',
)

PLUGIN_ROUTES = TARGET_ROUTES[2:]

# 合法 JSON、但不是对象。null 解析成 None，其余是 list / str / int / bool。
NON_OBJECT_BODIES = (b"[1,2]", b'"hi"', b"42", b"true", b"null")

PLUGIN_MESSAGE = "请求内容必须是 JSON 对象"


class JsonBodyShapeTests(unittest.TestCase):
    """非对象 JSON 一律 400 + 中文，且不泄漏异常类名。"""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        (self.root / "styles").mkdir()
        config = {"ai": {"api_url": "https://api.example.com/v1", "api_key": "k",
                         "model": "m", "api_style": "auto"},
                  "wechat": {"backend": "mock", "send_interval": 0},
                  "wechat_only": {"backend": "mock"}}
        path = self.root / "config.wechat.yaml"
        path.write_text(yaml.safe_dump(config, allow_unicode=True), encoding="utf-8")
        self.runtime = WeChatRuntime(root=self.root, config_path=path,
                                     db_path=self.root / "m.sqlite3")
        # raise_server_exceptions=False：异常若再逃出去，要拿到 500 这个状态码。
        self.client_context = TestClient(create_app(self.runtime),
                                         raise_server_exceptions=False)
        self.client = self.client_context.__enter__()

    def tearDown(self):
        self.client_context.__exit__(None, None, None)
        self.runtime.db.close()
        self._tmp.cleanup()

    def _post_raw(self, path, body):
        return self.client.post(path, content=body,
                                headers={"Content-Type": "application/json"})

    def test_non_object_bodies_never_500(self):
        """5 种非对象 JSON × 8 个路由 = 40 个组合，全部 400。"""
        for body in NON_OBJECT_BODIES:
            for path in TARGET_ROUTES:
                with self.subTest(body=body, path=path):
                    response = self._post_raw(path, body)
                    self.assertNotEqual(response.status_code, 500,
                                        f"{path} {body!r} -> {response.status_code}")
                    self.assertEqual(response.status_code, 400,
                                     f"{path} {body!r} -> {response.text[:200]}")

    def test_no_exception_class_name_leaks(self):
        """detail 里不能出现异常类名或 repr 片段。"""
        for body in NON_OBJECT_BODIES:
            for path in TARGET_ROUTES:
                with self.subTest(body=body, path=path):
                    detail = str(self._post_raw(path, body).json().get("detail") or "")
                    self.assertNotIn("AttributeError", detail)
                    self.assertNotIn("'list' object", detail)
                    self.assertNotIn("NoneType", detail)

    def test_plugin_routes_share_one_message(self):
        """六个 plugins 路由的文案必须完全一致，用户看到的是同一句话。"""
        for body in NON_OBJECT_BODIES:
            for path in PLUGIN_ROUTES:
                with self.subTest(body=body, path=path):
                    detail = str(self._post_raw(path, body).json().get("detail") or "")
                    self.assertEqual(detail, PLUGIN_MESSAGE)

    def test_former_500_routes_speak_chinese(self):
        """原本 500 的两个路由现在给中文提示，而不是异常类名。"""
        cases = (("/api/persona-files/image", "人设图片数据格式不对，请重新选择图片"),
                 ("/api/management/password", "请输入要设置的管理员密码"))
        for path, expected in cases:
            with self.subTest(path=path):
                response = self._post_raw(path, b"[1,2]")
                self.assertEqual(response.status_code, 400, response.text[:200])
                detail = str(response.json().get("detail") or "")
                self.assertEqual(detail, expected)
                self.assertNotIn("AttributeError", detail)

    def test_bad_json_still_400(self):
        """回归护栏：1.3.100 的 JSONDecodeError handler 没被这批改动打坏。"""
        for path in TARGET_ROUTES:
            with self.subTest(path=path):
                response = self._post_raw(path, b"{not json")
                self.assertEqual(response.status_code, 400, response.text[:200])
                self.assertIn("JSON", str(response.json().get("detail") or ""))

    def test_valid_object_requests_still_work(self):
        """回归护栏：合法对象不能被新加的 isinstance 判断误伤。"""
        password = self.client.post("/api/management/password", json={"password": ""})
        self.assertNotEqual(password.status_code, 500, password.text[:200])
        self.assertIn(password.status_code, (200, 400))
        inspect = self.client.post("/api/plugins/inspect", json={"url": ""})
        self.assertNotEqual(inspect.status_code, 500, inspect.text[:200])


if __name__ == "__main__":
    unittest.main()
