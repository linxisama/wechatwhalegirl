"""人设冲突、锁定与预算的回归测试（1.3.92）。

钉住的缺陷（都是上一版 src/style_manager.py 改动里真实存在或半成品的地方）：
① _load_style_file 加了 origin 参数，但调用点必须同时合法，否则就是运行时
   TypeError: takes 2 positional arguments but 3 were given（wechat_app.py:317 崩）；
② 内置与自定义同名时后加载者静默覆盖：界面上两份同名，内置那份既改不了也删不掉；
③ 名字不同但显示名只差一个「·」的人设，界面上根本分不清，同样不能静默；
④ 锁定人设被切换时必须拒绝，且锁定文件缺失时不能崩；
⑤ 显示名为空时身份锁整段不写进提示词（人设会被带成通用客服腔）；
⑥ 人设文件里的数字不可信（0、负数、字符串、NaN），不能一路传到模型调用；
⑦ 坏 YAML 只能坏自己那一份，不能让 load_all 整体失败；
⑧ 人设预算不能突破全局 speed_plan 的上限（wechat_app.py:2019-2020）。
"""
import asyncio
import pathlib
import tempfile
import unittest

import yaml

from src.config_manager import LOCKED_STYLE_NAME, ConfigManager
from src.dialogue_context import CONTINUITY
from src.prompt_budget import compose_prompt
from src.style_manager import ChatStyle, StyleManager


def write_persona(path, **fields):
    """按人设文件的口径写一份 YAML；没给的字段补最小可加载集合。"""
    data = {"name": path.stem, "display_name": fields.pop("display_name", path.stem),
            "system_prompt": "你是测试人设。"}
    data.update(fields)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.safe_dump(data, allow_unicode=True), encoding="utf-8")
    return path


class ManagerCase(unittest.TestCase):
    """每个用例一个临时仓库根：styles/ 放内置，styles/custom/ 放自定义。"""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.root = pathlib.Path(self._tmp.name)
        self.styles = self.root / "styles"
        self.custom = self.styles / "custom"
        self.styles.mkdir()

    def tearDown(self):
        self._tmp.cleanup()

    def manager(self, locked_name=None):
        return StyleManager(str(self.styles), str(self.custom), locked_name)


class LoadSignatureTests(ManagerCase):
    def test_wechat_app_call_shape_does_not_raise(self):
        """src/wechat_app.py:316-317 就是这么建的：三参构造 + load_all()。"""
        write_persona(self.styles / "one.yaml", name="one", system_prompt="内置一号")
        manager = self.manager()
        manager.load_all()          # 上一版在这里抛 TypeError
        self.assertEqual(manager.get("one").system_prompt, "内置一号")

    def test_single_argument_call_still_works(self):
        """origin 必须有默认值：只传 path 的老式调用也要能加载。"""
        path = write_persona(self.styles / "legacy.yaml", name="legacy", system_prompt="旧调用")
        manager = self.manager()
        manager._load_style_file(path)      # 只给一个位置参数
        self.assertEqual(manager.get("legacy").system_prompt, "旧调用")
        self.assertEqual(manager._origins["legacy"], "builtin")


class LockTests(ManagerCase):
    def test_locked_persona_rejects_switch(self):
        write_persona(self.styles / "locked.yaml", name="locked")
        write_persona(self.styles / "other.yaml", name="other")
        manager = self.manager("locked")
        manager.load_all()
        self.assertEqual(manager.active_name, "locked")
        self.assertFalse(manager.set_active("other"))
        self.assertEqual(manager.active_name, "locked")
        self.assertEqual(manager.get().name, "locked")

    def test_missing_locked_persona_falls_back(self):
        write_persona(self.styles / "other.yaml", name="other")
        manager = self.manager("gone")
        manager.load_all()          # 旧行为抛 RuntimeError
        self.assertIn(manager.active_name, ("default", "other"))


class ConfigLockTests(unittest.TestCase):
    """配置层的锁不再把人设钉死：锁字段被强制清空，用户选的人设原样保留。"""

    def manager(self, style):
        instance = ConfigManager.__new__(ConfigManager)
        instance._config = {"style": dict(style)}
        return instance

    def test_lock_fields_are_cleared_and_choice_kept(self):
        instance = self.manager({"active": "my_cat", "locked": True, "locked_name": "tsundere_whale_girl"})
        instance._enforce_style_lock()
        style = instance._config["style"]
        self.assertEqual(style["active"], "my_cat")
        self.assertFalse(style["locked"])
        self.assertIsNone(style["locked_name"])

    def test_missing_section_falls_back_to_default_persona(self):
        instance = self.manager({})
        instance._enforce_style_lock()
        self.assertEqual(instance._config["style"]["active"], LOCKED_STYLE_NAME)


class ConflictTests(ManagerCase):
    def test_custom_shadows_builtin_and_records_conflict(self):
        write_persona(self.styles / "dup.yaml", name="dup", system_prompt="内置正文", max_tokens=300)
        write_persona(self.custom / "dup.yaml", name="dup", system_prompt="自定义正文", max_tokens=900)
        manager = self.manager()
        manager.load_all()
        # 运行时仍用自定义那份（用户自定义优先）
        self.assertEqual(manager.get("dup").system_prompt, "自定义正文")
        # 但不能静默：必须留痕，并指出两份文件分别是哪个
        self.assertEqual(len(manager._conflicts), 1)
        conflict = manager._conflicts[0]
        self.assertEqual((conflict["kind"], conflict["name"]), ("same_name", "dup"))
        self.assertEqual((conflict["previous"], conflict["current"]), ("builtin", "custom"))
        self.assertEqual(pathlib.Path(conflict["previous_file"]), self.styles / "dup.yaml")
        self.assertEqual(pathlib.Path(conflict["current_file"]), self.custom / "dup.yaml")

    def test_near_identical_display_name_is_recorded(self):
        """名字不同、显示名只差一个「·」：界面上分不清，也要留痕（不能静默）。"""
        write_persona(self.styles / "whale_a.yaml", name="whale_a", display_name="鲸鱼娘")
        write_persona(self.styles / "whale_b.yaml", name="whale_b", display_name="鲸鱼娘·")
        manager = self.manager()
        manager.load_all()
        kinds = [(item["kind"], item["name"]) for item in manager._conflicts]
        self.assertIn(("near_display", "whale_b"), kinds)
        # 留痕归留痕，两份都还在，谁也不许顶掉谁
        self.assertEqual(manager.get("whale_a").display_name, "鲸鱼娘")
        self.assertEqual(manager.get("whale_b").display_name, "鲸鱼娘·")

    def test_broken_yaml_does_not_break_other_personas(self):
        write_persona(self.styles / "good.yaml", name="good", system_prompt="好的")
        (self.styles / "bad.yaml").write_text("name: [没有闭合\n", encoding="utf-8")
        manager = self.manager()
        manager.load_all()
        self.assertEqual(manager.get("good").system_prompt, "好的")
        self.assertNotIn("bad", manager._styles)


class NumericSanitizeTests(ManagerCase):
    def test_bad_numbers_fall_back_or_clamp(self):
        """人设文件里的数字不可信：0/负数/字符串回默认，超界钳到区间内。"""
        write_persona(self.styles / "zero.yaml", name="zero", max_tokens=0, temperature="不是数字")
        write_persona(self.styles / "huge.yaml", name="huge", max_tokens=99999, temperature=9)
        write_persona(self.styles / "nan.yaml", name="nan", max_tokens=500, temperature=".nan")
        manager = self.manager()
        manager.load_all()
        zero = manager.get("zero")
        self.assertEqual((zero.max_tokens, zero.temperature), (500, 0.7))
        huge = manager.get("huge")
        self.assertEqual((huge.max_tokens, huge.temperature), (8000, 2.0))
        self.assertEqual(manager.get("nan").temperature, 0.7)


class EmptyNameTests(ManagerCase):
    def test_blank_name_is_rejected(self):
        manager = self.manager()
        manager.load_all()
        for bad in ("", "   ", None):
            with self.subTest(name=bad):
                with self.assertRaises(ValueError) as raised:
                    manager.save_persona({"name": bad, "system_prompt": "x"})
                self.assertIn("不能为空", str(raised.exception))
        self.assertNotIn("", manager._styles)

    def test_traversal_name_is_rejected(self):
        manager = self.manager()
        manager.load_all()
        for bad in ("../evil", "a/b", "名字"):
            with self.subTest(name=bad):
                with self.assertRaises(ValueError):
                    manager.save_persona({"name": bad, "system_prompt": "x"})
        self.assertEqual(list(self.custom.glob("*.yaml")), [])


class IdentityLockTests(ManagerCase):
    def test_identity_lock_follows_display_name_and_leaves_no_residue(self):
        """换人设后身份锁段必须换成新人设，旧人设的名字一个字都不能留。"""
        write_persona(self.styles / "a.yaml", name="alpha", display_name="阿甲", system_prompt="甲正文")
        write_persona(self.styles / "b.yaml", name="beta", display_name="阿乙", system_prompt="乙正文")
        manager = self.manager()
        manager.load_all()

        before = manager.get("alpha").prompt_text()
        self.assertIn("阿甲", before)
        self.assertNotIn("阿乙", before)

        self.assertTrue(manager.set_active("beta"))
        after = manager.get().prompt_text()
        self.assertIn("阿乙", after)
        self.assertIn("乙正文", after)
        self.assertNotIn("阿甲", after)
        self.assertNotIn("甲正文", after)

    def test_identity_lock_falls_back_to_name_when_display_name_empty(self):
        """显示名为空时必须回退到标识，否则整段身份锁不会写进提示词。"""
        style = ChatStyle(name="solo", display_name="", description="", system_prompt="正文")
        text = style.prompt_text()
        self.assertIn("身份锁", text)
        self.assertIn("「solo」", text)


class RenameCleanupTests(ManagerCase):
    def test_rename_leaves_no_stale_origin(self):
        """改名只动 _styles 会留下指向旧文件的来历：诊断会指到空处。"""
        manager = self.manager()
        manager.load_all()
        manager.save_persona({"name": "old_name", "system_prompt": "旧"})
        manager.save_persona({"name": "new_name", "original_name": "old_name", "system_prompt": "新"})
        self.assertIsNone(manager.get_for_edit("old_name"))
        self.assertNotIn("old_name", manager._origins)
        self.assertNotIn("old_name", manager._sources)
        self.assertEqual(manager._origins["new_name"], "custom")

    def test_delete_custom_persona_leaves_no_stale_origin(self):
        manager = self.manager()
        manager.load_all()
        manager.save_persona({"name": "temp_one", "system_prompt": "x"})
        self.assertTrue(manager.remove_style("temp_one"))
        self.assertNotIn("temp_one", manager._origins)
        self.assertNotIn("temp_one", manager._sources)


class RemoveShadowTests(ManagerCase):
    def test_deleting_custom_copy_restores_builtin(self):
        """同名副本顶掉内置人设后，删副本必须能删，并把内置那份恢复回来。"""
        write_persona(self.styles / "dup.yaml", name="dup", system_prompt="内置正文")
        write_persona(self.custom / "dup.yaml", name="dup", system_prompt="自定义正文")
        manager = self.manager()
        manager.load_all()
        self.assertEqual(manager.get("dup").system_prompt, "自定义正文")

        self.assertTrue(manager.remove_style("dup"))
        self.assertFalse((self.custom / "dup.yaml").exists())
        self.assertEqual(manager.get("dup").system_prompt, "内置正文")
        self.assertEqual(manager._origins["dup"], "builtin")
        self.assertEqual(manager._conflicts, [])

    def test_traversal_name_cannot_delete_outside_custom_dir(self):
        """人设名会拼成文件名：越界名不许删到 styles/ 外面的文件。"""
        victim = self.styles / "victim.yaml"
        write_persona(victim, name="victim")
        manager = self.manager()
        manager.load_all()
        self.assertFalse(manager.remove_style("../styles/victim"))
        self.assertTrue(victim.exists())


class ContinuityTests(ManagerCase):
    def test_continuity_block_carries_no_persona_residue(self):
        """换人设只换 persona 块；通用承接规则里不许残留上一个人设的内容。"""
        write_persona(self.styles / "a.yaml", name="alpha", display_name="阿甲", system_prompt="甲正文")
        write_persona(self.styles / "b.yaml", name="beta", display_name="阿乙", system_prompt="乙正文")
        manager = self.manager()
        manager.load_all()
        first = compose_prompt(persona=manager.get("alpha").prompt_text(), continuity=CONTINUITY)
        manager.set_active("beta")
        second = compose_prompt(persona=manager.get().prompt_text(), continuity=CONTINUITY)

        self.assertIn("阿甲", first.text)
        self.assertNotIn("阿乙", first.text)
        self.assertIn("阿乙", second.text)
        for residue in ("阿甲", "甲正文"):
            self.assertNotIn(residue, second.text)
        # 通用承接规则本身是常量，与人设无关
        self.assertNotIn("阿甲", CONTINUITY)
        self.assertNotIn("阿乙", CONTINUITY)
        self.assertIn(CONTINUITY, second.text)


try:
    from test_wechat_app_flow import GROUP, RuntimeTestCase
    HAS_RUNTIME = True
except Exception:       # 缺运行时依赖时只跳过这两条，不让整份文件红
    HAS_RUNTIME = False


@unittest.skipUnless(HAS_RUNTIME, "需要运行时测试骨架")
class MaxTokensBudgetTests(RuntimeTestCase):
    """wechat_app.py:2019-2022：人设预算与全局 speed_plan 冲突时取更小的那个。"""

    async def reply_budget(self, speed, max_tokens):
        bot = await self.start_runtime(wechat={"reply_speed": speed})
        bot.style.save_persona({"name": "budget_persona", "display_name": "预算人设",
                                "system_prompt": "你是预算人设。", "max_tokens": max_tokens})
        bot.activate_persona("budget_persona")
        ai = self.install_ai("在的～")
        await asyncio.to_thread(bot.bridge.inject_mock_message, GROUP,
                                "@鲸鱼娘 在吗", sender="阿明", at=True)
        await self.wait_for_send(1)
        self.assertTrue(ai.calls, "模型没被调用，预算断言没有意义")
        return ai.calls[0][1]["max_tokens"]

    async def test_global_cap_wins_when_persona_asks_for_more(self):
        # fast 档上限 260；人设要 1200 → 取 260
        self.assertEqual(await self.reply_budget("fast", 1200), 260)

    async def test_persona_wins_when_global_plan_is_unlimited(self):
        # normal 档 max_tokens=0 表示不限；人设要 140 → 取 140
        self.assertEqual(await self.reply_budget("normal", 140), 140)


if __name__ == "__main__":
    unittest.main()
