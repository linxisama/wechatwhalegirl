"""「关于作者」页的离线回归测试（需要 PyQt6，缺 PyQt6 时整类跳过）。

用户要求（2026-10-15）：在控制台新加一页「关于作者」——作者 林夕、头像用他给的图、
QQ 2019117802、特别感谢「你携秋水揽星河」、声明「未经作者允许，禁止贩卖、转卖」，
其余内容自行补充。

这一页有三件容易在打包后才坏的事，所以钉在测试里：
1. **头像必须随包发出去**：资源从 `BUNDLE/assets` 取，漏进 spec 时源码能跑、装完就没图；
2. **声明与作者信息必须真的显示出来**（而不是只存在于常量里）；
3. **版本号只有一个来源**（`src/runtime_identity.py`），页面上不能写死。
"""

import os
import unittest
from pathlib import Path

try:
    from PyQt6.QtWidgets import QApplication, QLabel
    HAVE_QT = True
except Exception:  # pragma: no cover - 环境缺 PyQt6 时跳过
    HAVE_QT = False

if HAVE_QT:
    from tests.gui_offline_helpers1336 import OfflineGuiIsolation

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

_APP = None


def _app():
    # 必须持有模块级引用：QApplication 被回收后再建 QWidget 会直接崩进程
    global _APP
    _APP = QApplication.instance() or QApplication([])
    return _APP


def texts_of(widget) -> str:
    """把控件树里的文字拼起来，用来断言"页面上真的显示了什么"。"""
    chunks = []
    for label in widget.findChildren(QLabel):
        chunks.append(label.text() or "")
        if label.pixmap() is not None and not label.pixmap().isNull():
            chunks.append("[图]")
    return "\n".join(chunks)


@unittest.skipUnless(HAVE_QT, "需要 PyQt6")
class AboutPageTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        _app()
        import wechat_gui
        cls.gui = wechat_gui

    def setUp(self):
        self.tab = self.gui.AboutTab()

    def test_page_shows_author_qq_thanks_and_notice(self):
        shown = texts_of(self.tab)
        self.assertIn(self.gui.AUTHOR_NAME, shown)
        self.assertIn(self.gui.AUTHOR_QQ, shown)
        self.assertIn("你携秋水揽星河", shown)
        self.assertIn("未经作者允许，禁止贩卖、转卖", shown)

    def test_thanks_and_notice_come_from_single_source(self):
        """文案集中在文件顶部的常量里，改一处就能改整页。"""
        self.assertEqual(self.gui.COPYRIGHT_NOTICE, "未经作者允许，禁止贩卖、转卖。")
        self.assertIn("你携秋水揽星河", self.gui.THANKS_USERS)
        self.assertEqual(self.gui.AUTHOR_QQ, "2019117802")

    def test_avatar_is_bundled_and_loads_as_a_circle(self):
        path = self.gui.author_avatar_path()
        self.assertTrue(path.is_file(), f"头像资源缺失：{path}")
        self.assertTrue(str(path).replace("\\", "/").endswith("assets/author/linxi.jpg"))
        pixmap = self.tab.avatar.pixmap()
        self.assertFalse(pixmap.isNull(), "头像没加载出来")
        self.assertEqual((pixmap.width(), pixmap.height()), (92, 92))
        # 四角透明 = 圆形裁切生效（否则会露出方形底）
        image = pixmap.toImage()
        for x, y in ((0, 0), (image.width() - 1, 0), (0, image.height() - 1),
                     (image.width() - 1, image.height() - 1)):
            self.assertEqual(image.pixelColor(x, y).alpha(), 0, f"({x},{y}) 不是透明的圆角")

    def test_version_row_uses_the_single_version_source(self):
        from src.runtime_identity import APP_VERSION
        shown = texts_of(self.tab)
        self.assertIn(APP_VERSION, shown)

    def test_copy_buttons_put_things_on_the_clipboard(self):
        self.tab._copy_qq()
        self.assertIn(self.gui.AUTHOR_QQ, self.tab.copy_hint.text())
        self.assertEqual(QApplication.clipboard().text(), self.gui.AUTHOR_QQ)
        self.tab._copy_version()
        copied = QApplication.clipboard().text()
        self.assertIn("WhaleGirlWeChat", copied)
        self.assertIn(self.gui.AUTHOR_NAME, copied)

    def test_page_avoids_dangerous_or_noisy_content(self):
        """这一页不该出现网页跳转/外链按钮之外的意外内容（无网也能看）。"""
        shown = texts_of(self.tab)
        for forbidden in ("http://", "https://"):
            self.assertNotIn(forbidden, shown, "关于页不应依赖任何在线内容")

    def test_missing_avatar_degrades_to_the_name_instead_of_crashing(self):
        tab = self.gui.AboutTab()
        original = self.gui.author_avatar_path
        try:
            self.gui.author_avatar_path = lambda: Path("Z:/definitely/not/here.jpg")
            tab._load_avatar()
        finally:
            self.gui.author_avatar_path = original
        self.assertTrue(tab.avatar.pixmap().isNull() or tab.avatar.text() == self.gui.AUTHOR_NAME)


@unittest.skipUnless(HAVE_QT, "需要 PyQt6")
class AboutTabIsRegisteredTests(unittest.TestCase):
    """新页必须真的挂在标签栏上（而不是只写了个类）。"""

    @classmethod
    def setUpClass(cls):
        _app()
        import wechat_gui
        cls.gui = wechat_gui

    def setUp(self):
        self._isolation = OfflineGuiIsolation(self, self.gui, _APP)

    def tearDown(self):
        self._isolation.close()

    def test_tab_bar_has_the_about_page(self):
        window = self._isolation.new_window()
        try:
            titles = [window.tabs.tabText(i) for i in range(window.tabs.count())]
            self.assertIn("关于作者", titles)
            self.assertEqual(titles[-1], "关于作者", "关于页应放在最后一页")
            index = titles.index("关于作者")
            self.assertIs(window.tabs.widget(index), window.about_tab)
        finally:
            self._isolation.destroy_window(window)


if __name__ == "__main__":
    unittest.main()
