"""1.3.92 表情包描述挤爆提示词预算：段落独立预算、单条上限、known_notes 封顶、日志可诊断。

用户点名的缺陷：一个表情的描述上限 120 字，随便几个表情包就把插件提示词预算吃光，
最终只有 800 字到得了模型，模型只看得到最新五六个表情。这里把"表情包段落"当成
有独立硬预算的一段，逐条封顶、按相关度从尾部砍，并把砍了几条写进注入日志。
"""
import json
import unittest
from types import SimpleNamespace

from src.plugins.image_library import (STICKER_PREFIX, STICKER_SUFFIX, STICKER_TAG_CHARS,
                                       STICKER_TAG_LIMIT, ImageLibrary, sticker_section)
from src.plugins.sticker_memory import StickerMemoryCache
from src.prompt_budget import (STICKER_MEANING_CHARS, STICKER_MAX_ITEMS, STICKER_SECTION_CAP,
                               compose_prompt, sticker_cap, sticker_cut_count,
                               sticker_max_items, sticker_meaning_chars)


class FakeStore:
    """只提供 image_library 用到的两个方法：entries() 与 path_for()。"""

    def __init__(self, entries):
        self._entries = [dict(entry) for entry in entries]

    def entries(self):
        return [dict(entry) for entry in self._entries]

    def path_for(self, key):
        # 用公开链接代替本机文件：image_source 认它，测试就不必在磁盘上造图
        return f'https://example.test/{key}.png'


class FakeManager:
    def __init__(self, library=''):
        self._settings = {'enabled': True, 'library': library, 'autoSelect': True}

    def get_settings(self, plugin_id):
        return dict(self._settings)


def library_plugin(library=''):
    return ImageLibrary(FakeManager(library))


def runtime_with(entries=(), config=None):
    return SimpleNamespace(stickers=FakeStore(entries), config=config)


def sticker_entry(label, description='', tags=(), last_used=0.0):
    return {'id': label, 'label': label, 'description': description,
            'manual_tags': list(tags), 'last_used': last_used}


def extreme_entries(count=20, chars=120):
    """20 个表情、每个描述 120 字：正是用户描述的极端场景。"""
    entries = [sticker_entry(f'表情{i:02d}', '描述' * (chars // 2)) for i in range(count)]
    # 第一条与查询词相关：120 字里以"开心"开头，用来验证"优先保留相关度最高的那条"
    entries.append(sticker_entry('表情开心', '开心' + '描述' * ((chars - 2) // 2)))
    return entries


def notes_of(text):
    """从段落文案里取回 JSON 列表（前缀之后、数组闭合方括号为止）。"""
    assert text.startswith(STICKER_PREFIX), text[:40]
    body = text[len(STICKER_PREFIX):]
    return json.loads(body[:body.rindex(']') + 1])


class ExtremeSectionTests(unittest.TestCase):
    def test_twenty_stickers_with_120_char_descriptions_fit_800(self):
        plugin = library_plugin()
        runtime = runtime_with(extreme_entries())
        text = plugin.on_pre_ai({'text': '来点图'}, [], runtime)['extra_system']
        self.assertLessEqual(len(text), STICKER_SECTION_CAP)
        self.assertLessEqual(len(text), 800)
        notes = notes_of(text)
        self.assertGreaterEqual(len(notes), 1)

    def test_most_relevant_sticker_survives_the_cut(self):
        plugin = library_plugin()
        runtime = runtime_with(extreme_entries())
        text = plugin.on_pre_ai({'text': '开心'}, [], runtime)['extra_system']
        notes = notes_of(text)
        self.assertEqual(notes[0]['label'], '表情开心')

    def test_normal_library_is_not_truncated(self):
        entries = [sticker_entry(f'表情{i}', '一只在笑的鲸鱼', ['开心']) for i in range(3)]
        text = library_plugin().on_pre_ai({'text': '笑一个'}, [], runtime_with(entries))['extra_system']
        self.assertLessEqual(len(text), STICKER_SECTION_CAP)
        self.assertNotIn('…', text)
        self.assertNotIn('已按相关度省略', text)
        self.assertEqual(len(notes_of(text)), 3)


class SingleNoteTests(unittest.TestCase):
    def test_long_description_is_clipped_to_40_chars_with_ellipsis(self):
        entry = sticker_entry('长描述', '很' * 120)
        text = library_plugin().on_pre_ai({'text': '图'}, [], runtime_with([entry]))['extra_system']
        meaning = notes_of(text)[0]['meaning']
        self.assertEqual(len(meaning), STICKER_MEANING_CHARS)
        self.assertTrue(meaning.endswith('…'))

    def test_tags_are_three_of_eight_chars(self):
        entry = sticker_entry('多标签', '一张图', ['一二三四五六七八九十', '甲乙丙丁戊己庚辛壬癸',
                                                 '子丑寅卯辰巳午未申酉', '春夏秋冬', '东西南北'])
        text = library_plugin().on_pre_ai({'text': '图'}, [], runtime_with([entry]))['extra_system']
        tags = notes_of(text)[0]['tags']
        self.assertEqual(len(tags), STICKER_TAG_LIMIT)
        for tag in tags:
            self.assertLessEqual(len(tag), STICKER_TAG_CHARS)


class SectionRenderTests(unittest.TestCase):
    def test_section_drops_from_the_tail_but_keeps_one(self):
        # 12 条按相关度降序的说明：超预算时只能从尾部砍，留下的第一条必须还是最相关的
        notes = [{'label': f'a{i}', 'meaning': '描' * 40, 'tags': []} for i in range(12)]
        text, cut = sticker_section(notes, 300)
        self.assertLessEqual(len(text), 300)
        self.assertEqual(cut, 11)
        self.assertEqual(notes_of(text)[0]['label'], 'a0')

    def test_section_keeps_at_least_one_item_even_when_cap_is_tiny(self):
        notes = [{'label': '标签', 'meaning': '描' * 40, 'tags': []}]
        text, cut = sticker_section(notes, 10)
        self.assertEqual(cut, 0)
        self.assertEqual(len(notes_of(text)), 1)
        self.assertTrue(text.endswith(STICKER_SUFFIX))


class KnownNotesTests(unittest.TestCase):
    def setUp(self):
        self.cache = StickerMemoryCache()
        self.digests = []
        for index in range(20):
            digest = f'd{index:02d}'
            self.cache.remember(digest, f'第{index}张：' + '描' * 500, 'group:1', now=0.0)
            self.digests.append(digest)

    def test_twenty_huge_meanings_stay_within_200_chars(self):
        notes = self.cache.known_notes(self.digests, 'group:1', 0, 0, now=0.0)
        self.assertLessEqual(len(notes), STICKER_MAX_ITEMS)
        self.assertLessEqual(len(''.join(notes)), 200)
        for note in notes:
            self.assertTrue(note.startswith('[已知表情包：') and note.endswith(']'))

    def test_explicit_limits_override_defaults(self):
        notes = self.cache.known_notes(self.digests, 'group:1', 0, 0, now=0.0,
                                       meaning_chars=6, max_items=1)
        self.assertEqual(len(notes), 1)
        self.assertEqual(len(notes[0]), len('[已知表情包：]') + 6)
        self.assertTrue(notes[0].endswith('…]'))


class ConfigOverrideTests(unittest.TestCase):
    def test_three_caps_come_from_config(self):
        config = {'prompt_budget': {'stickerCap': 300, 'stickerMeaningChars': 10,
                                    'stickerMaxItems': 1}}
        self.assertEqual(sticker_cap(config), 300)
        self.assertEqual(sticker_meaning_chars(config), 10)
        self.assertEqual(sticker_max_items(config), 1)

    def test_overrides_reach_the_section(self):
        config = {'prompt_budget': {'stickerCap': 300, 'stickerMeaningChars': 10,
                                    'stickerMaxItems': 1}}
        text = library_plugin().on_pre_ai({'text': '图'}, [],
                                          runtime_with(extreme_entries(), config))['extra_system']
        self.assertLessEqual(len(text), 300)
        notes = notes_of(text)
        self.assertEqual(len(notes), 1)
        self.assertEqual(len(notes[0]['meaning']), 10)

    def test_invalid_overrides_fall_back_to_defaults(self):
        config = {'prompt_budget': {'stickerCap': 'broken', 'stickerMeaningChars': -5,
                                    'stickerMaxItems': True}}
        self.assertEqual(sticker_cap(config), STICKER_SECTION_CAP)
        self.assertEqual(sticker_meaning_chars(config), STICKER_MEANING_CHARS)
        self.assertEqual(sticker_max_items(config), STICKER_MAX_ITEMS)
        self.assertEqual(sticker_cap({'prompt_budget': {'stickerCap': 0}}), STICKER_SECTION_CAP)


class DegenerateLibraryTests(unittest.TestCase):
    def test_empty_library_says_so_without_json(self):
        text = library_plugin().on_pre_ai({'text': '图'}, [], runtime_with())['extra_system']
        self.assertIn('没有可发送的图片', text)
        self.assertNotIn('[', text)

    def test_only_unlabeled_images_says_so_without_json(self):
        # 自动收集的条目没有说明、标签，也不该被当成"知道图意"喂给模型
        entries = [{'id': 'abcdef012345', 'label': '', 'description': '', 'manual_tags': []}]
        text = library_plugin().on_pre_ai({'text': '图'}, [], runtime_with(entries))['extra_system']
        self.assertIn('只有未标注图片', text)
        self.assertNotIn('[[表情', text)


class PromptPlanDiagnosticsTests(unittest.TestCase):
    def setUp(self):
        text = library_plugin().on_pre_ai({'text': '来点图'}, [],
                                          runtime_with(extreme_entries()))['extra_system']
        self.section = text

    def test_cut_count_is_readable_from_the_section(self):
        # 21 个候选只留 3 条：砍掉 18 条，条数就写在段落文案里
        self.assertEqual(sticker_cut_count(self.section), 18)
        self.assertEqual(sticker_cut_count('没有省略'), 0)

    def test_plan_reports_the_cut_in_log_line(self):
        plan = compose_prompt(persona='P' * 20, plugins='\n' + self.section,
                              plugin_parts=[('image_library', self.section)])
        self.assertEqual(plan.sticker_cut, 18)
        self.assertIn('表情包段落砍掉 18 条', plan.log_line())
        self.assertEqual(plan.log_line().count('\n'), 0)

    def test_plan_without_cut_says_nothing_about_it(self):
        section = library_plugin().on_pre_ai(
            {'text': '图'}, [], runtime_with([sticker_entry('表情', '一只鲸鱼')]))['extra_system']
        plan = compose_prompt(persona='P', plugins='\n' + section,
                              plugin_parts=[('image_library', section)])
        self.assertEqual(plan.sticker_cut, 0)
        self.assertNotIn('表情包段落砍掉', plan.log_line())


if __name__ == '__main__':
    unittest.main()
