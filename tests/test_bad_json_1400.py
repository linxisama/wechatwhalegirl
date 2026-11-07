# -*- coding: utf-8 -*-
"""1.3.100：请求体不是合法 JSON 时，写接口必须回 400 而不是 500。

这些路由都直接 ``await request.json()``，坏 JSON 抛 ``JSONDecodeError``、非法 UTF-8
抛 ``UnicodeDecodeError``，两者都不在 HTTPException 体系里，不在应用级接住就会
逃出路由变成 500。用例只碰 HTTP 表面，不关心各路由自己的业务校验细节。
"""
import tempfile
import unittest
from pathlib import Path

import yaml
from fastapi.testclient import TestClient

from src.wechat_app import WeChatRuntime, create_app


BAD_JSON_ROUTES = (
    '/api/dialogue/rules',
    '/api/dialogue/notes',
    '/api/dialogue/facts',
    '/api/persona-files/image',
    '/api/stickers/annotation-jobs',
    '/api/stickers/import',
    '/api/stickers/abc123',
    '/api/personas',
    '/api/plugins/inspect',
    '/api/plugins/install',
    '/api/plugins/batch-inspect',
    '/api/plugins/batch-install',
    '/api/plugins/inspect-local',
    '/api/plugins/install-local',
    '/api/management/owner',
    '/api/management/password',
    '/api/plugins/robot_admin',
)


class BadJsonTests(unittest.TestCase):
    """坏请求体一律 400 + 中文，且不能把英文原文漏给用户。"""

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
        # raise_server_exceptions=False：异常若再逃出去，要拿到 500 这个状态码，
        # 而不是让 TestClient 直接把异常抛到用例里（那样只能看到 traceback）。
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

    def test_bad_json_returns_400_with_chinese_detail(self):
        """17 个直接 await request.json() 的写接口逐个打坏 JSON。"""
        for path in BAD_JSON_ROUTES:
            with self.subTest(path=path):
                response = self._post_raw(path, b"{not json")
                self.assertEqual(response.status_code, 400, response.text[:200])
                detail = str(response.json().get("detail") or "")
                self.assertIn("JSON", detail)
                # 中文提示：不能把 json 模块的英文原文回给用户
                self.assertIn("请求体不是有效的", detail)

    def test_empty_body_returns_400(self):
        """空请求体在 json 模块里也是 JSONDecodeError，走同一条路。"""
        for path in ("/api/dialogue/notes", "/api/stickers/import",
                     "/api/plugins/inspect", "/api/management/owner"):
            with self.subTest(path=path):
                response = self._post_raw(path, b"")
                self.assertEqual(response.status_code, 400, response.text[:200])
                self.assertIn("JSON", str(response.json().get("detail") or ""))

    def test_invalid_utf8_returns_400(self):
        """非法 UTF-8 抛的是 UnicodeDecodeError，另一个 handler 接住。"""
        for path in ("/api/dialogue/notes", "/api/stickers/import",
                     "/api/plugins/inspect"):
            with self.subTest(path=path):
                response = self._post_raw(path, b'{"a":"\xff"}')
                self.assertEqual(response.status_code, 400, response.text[:200])
                self.assertIn("JSON", str(response.json().get("detail") or ""))

    def test_json_array_still_validated_by_route(self):
        """合法 JSON 数组不能被我这两个 handler 吞掉，仍由路由自己校验。"""
        import_response = self._post_raw("/api/stickers/import", b"[1,2]")
        self.assertEqual(import_response.status_code, 400)
        self.assertIn("1～20", str(import_response.json().get("detail") or ""))
        rules_response = self._post_raw("/api/dialogue/rules", b"[1,2]")
        self.assertEqual(rules_response.status_code, 400)
        rules_detail = str(rules_response.json().get("detail") or "")
        # 业务文案，不是「请求体不是有效的 JSON」
        self.assertNotIn("请求体不是有效的", rules_detail)

    def test_valid_notes_request_still_succeeds(self):
        """回归护栏：正常请求不受影响。"""
        response = self.client.post("/api/dialogue/notes", json={"content": "测试记忆"})
        self.assertEqual(response.status_code, 200, response.text[:200])
        self.assertTrue(response.json().get("success"))

    def test_annotation_jobs_empty_keys_is_not_500(self):
        """空 keys 是合法请求（没有需要补全的图），不能变成 500。"""
        response = self.client.post("/api/stickers/annotation-jobs", json={"keys": []})
        self.assertNotEqual(response.status_code, 500)
        self.assertEqual(response.status_code, 200, response.text[:200])

    def test_persona_import_hides_english_parser_message(self):
        """/api/persona-files/import 曾把 json 模块的英文原文当 detail 返回。"""
        response = self._post_raw("/api/persona-files/import", b"{not json")
        self.assertEqual(response.status_code, 400, response.text[:200])
        detail = str(response.json().get("detail") or "")
        self.assertNotIn("Expecting property name", detail)
        self.assertIn("JSON", detail)

    def test_persona_import_invalid_utf8_is_chinese(self):
        """非法 UTF-8 在 persona-files/import 上也要是中文提示。"""
        response = self._post_raw("/api/persona-files/import", b'{"a":"\xff"}')
        self.assertEqual(response.status_code, 400, response.text[:200])
        detail = str(response.json().get("detail") or "")
        self.assertIn("JSON", detail)
        self.assertNotIn("Expecting", detail)

    def test_handlers_are_registered_on_the_app(self):
        """钉住「用一处注册」这个修法本身：以后有人删掉 handler 会立刻红。"""
        handlers = self.client_context.app.exception_handlers
        names = {getattr(value, "__name__", "") for value in handlers.values()}
        self.assertIn("JSONDecodeError", {key.__name__ for key in handlers if isinstance(key, type)})
        self.assertIn("UnicodeDecodeError", {key.__name__ for key in handlers if isinstance(key, type)})
        self.assertIn("bad_json_handler", names)
        self.assertIn("bad_utf8_handler", names)


if __name__ == "__main__":
    unittest.main()
