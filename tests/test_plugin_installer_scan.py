"""安装预检：注释/文档字符串里的字眼不算违规，真调用照样拦。"""
import unittest

from src.plugin_installer import check_source

PLUGIN_SHELL = '''
from . import PluginBase, PluginMeta

class Plugin(PluginBase):
    META = PluginMeta(id="demo", name="演示", description="d")
    pass
'''


class ScanTests(unittest.TestCase):
    def _problems(self, body):
        return [p for p in check_source(body + PLUGIN_SHELL) if '禁止' in p]

    def test_comment_mentioning_wechatauto_is_not_a_violation(self):
        self.assertEqual(self._problems('# 参考 wechatauto.media.MediaDownloader 的做法\n'), [])

    def test_docstring_mentioning_wechatauto_is_not_a_violation(self):
        self.assertEqual(self._problems('"""说明：wechatauto 只用来读库，不直接发消息。"""\n'), [])

    def test_real_import_is_still_blocked(self):
        self.assertTrue(self._problems('import wechatauto\n'))

    def test_real_shell_call_is_still_blocked(self):
        self.assertTrue(self._problems('import os\nos.system("calc")\n'))

    def test_real_proactive_call_is_blocked(self):
        self.assertTrue(self._problems('def f(runtime):\n    runtime.queue_outgoing("k", "group", "g", "hi")\n'))


if __name__ == '__main__':
    unittest.main()
