"""原因码要同时出现在控制台标题与「测试微信」输出里。"""
import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


class SurfaceTests(unittest.TestCase):
    def test_console_shows_code_in_card_title(self):
        code = (ROOT / 'wechat_gui.py').read_text('utf-8')
        window = code[code.index('发送未就绪'):code.index('发送未就绪') + 700]
        self.assertIn("conn.get('send_code')", window)
        self.assertIn('connection_label +=', window)

    def test_test_wechat_output_includes_code(self):
        code = (ROOT / 'src/wechat_app.py').read_text('utf-8')
        self.assertIn('发送原因码：', code)

    def test_docs_cover_every_code(self):
        docs = (ROOT / '微信版使用说明.md').read_text('utf-8')
        for key in ('accessibility_unavailable', 'minimized', 'chat_not_open',
                    'input_unavailable', 'draft'):
            self.assertIn(key, docs, key)


if __name__ == '__main__':
    unittest.main()
