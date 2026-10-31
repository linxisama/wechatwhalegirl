"""插件钩子签名必须与 manager 的调用方式一致。

真实事故：联网现查的 `on_prepare` 写成了 `(message, conversation, runtime)`，
而 manager 只传 `(message, runtime)` → 每次都被 except 吞掉，只留一行"读取附加消息失败"，
功能等于没上线，测试却因为直接调插件而全绿。

1.3.47 补齐：CALLS 原来只列了 6 个钩子，漏掉 on_block / on_observed /
validate_settings / post_load；而 describe / generate 不是基类钩子，是宿主按插件 id
硬编码调用的两个入口（src/plugin_dispatch.py:96-105 describe_images、
src/plugin_dispatch.py:121-147 generate_reply），单列 HOST_HARDCODED。

每个新钩子都配一条"签名写错就出事"的负例：断言宿主真的产生了后果
（隔离记录 / 失败文案 / 抛错 / fail-closed），而不是断言"没有异常"——因为宿主
到处用 except 吞异常，只断言不抛错的话，把这些钩子删掉测试照样全绿。
"""
import inspect
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from src.plugins import FieldSchema, PluginBase, PluginMeta
from src.plugins.manager import PluginManager

# manager 按位置传参的基类钩子：名字和顺序都必须逐字一致。
CALLS = {
    'on_prepare': ('message', 'runtime'),
    'on_pre_ai': ('message', 'conversation', 'runtime'),
    'on_post_reply': ('message', 'reply', 'runtime'),
    'on_ingest': ('message', 'runtime'),
    'on_sent': ('message', 'reply', 'runtime'),
    'tick': ('runtime',),
    'on_block': ('message', 'runtime'),
    'on_observed': ('message', 'runtime'),
    'validate_settings': ('settings',),
    'post_load': (),
}

# 宿主不看基类、直接按插件 id 调的两个入口：(插件 id, 方法名) -> 位置参数名。
HOST_HARDCODED = {
    ('image_understanding', 'describe'): ('message', 'runtime'),
    ('chat_controls', 'generate'): ('engine', 'conversation'),
}

ALL_HOOKS = frozenset(CALLS) | {name for _, name in HOST_HARDCODED}

# 装进 custom_plugins_dir 的最小插件源码；__PID__ / __BODY__ 由 install() 替换。
CUSTOM_PLUGIN = """from . import FieldSchema, PluginBase, PluginMeta


class Demo(PluginBase):
    META = PluginMeta(id=__PID__, name='测试插件', description='',
        config_schema={'enabled': FieldSchema(type='bool', label='启用')},
        default_settings={'enabled': False})

__BODY__
"""


def positional(method):
    """只取宿主按位置传入的那部分参数名（**kwargs 不算签名错位）。"""
    params = [p for p in inspect.signature(method).parameters.values() if p.name != 'self']
    return tuple(p.name for p in params
                 if p.kind in (p.POSITIONAL_OR_KEYWORD, p.POSITIONAL_ONLY))


class RecordingEngine:
    """替身模型引擎：记录每次调用用的模型名，用来分辨"直连"与"备用模型"。"""

    model = 'main-model'
    last_error = ''

    def __init__(self, reply='直连回答'):
        self.reply = reply
        self.calls = []

    async def chat(self, conversation, **kwargs):
        self.calls.append(kwargs.get('model'))
        return self.reply


class SignatureTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.manager = PluginManager(Path(self.tmp.name) / 'plugins')

    def tearDown(self):
        self.tmp.cleanup()

    def test_every_plugin_hook_matches_the_manager_call(self):
        problems = []
        for plugin_id, plugin in self.manager._plugins.items():
            for name, expected in CALLS.items():
                method = getattr(plugin, name, None)
                if method is None or not callable(method):
                    continue
                names = positional(method)
                if names != expected:
                    problems.append(f'{plugin_id}.{name}{names} != {expected}')
        self.assertEqual(problems, [], '钩子签名与 manager 调用不一致：' + '; '.join(problems))

    def test_web_lookup_really_runs_through_the_manager(self):
        """走 manager 的调用路径（而不是直接调插件），防止签名错位再次被吞掉。"""
        import asyncio
        plugin = self.manager._plugins['web_lookup']
        plugin._fetch = lambda query: ('<ul><li class="b_algo"><h2><a href="https://a.example">微信输入法 4.0 发布</a></h2>'
                                       '<div class="b_caption"><p>微信输入法发布新版本，支持多端同步</p></div></li></ul>')
        notes = []
        runtime = type('R', (), {'note': staticmethod(notes.append)})()
        message = {'text': '搜一下 微信', 'target': 'room', 'kind': 'group'}
        asyncio.run(self.manager.run_on_prepare(message, runtime))
        failed = [note for note in notes if '失败' in note]
        self.assertEqual(failed, [], '钩子抛异常被吞掉了：' + str(notes))
        self.assertIn('_web_lookup', message)
        self.assertTrue(message['_web_lookup'].get('items'))

    def test_awaited_hooks_are_really_async(self):
        """管理器用 `await` 调这些钩子；写成同步函数会被 TypeError 吞掉，
        表现就是"整形/联网全都没生效，但测试全绿"（1.2.67 与 1.2.69 各踩一次）。
        """
        must_be_async = ('on_prepare', 'on_post_reply', 'tick')
        problems = []
        for plugin_id, plugin in self.manager._plugins.items():
            for name in must_be_async:
                method = getattr(plugin, name, None)
                if method is None:
                    continue
                if not inspect.iscoroutinefunction(method):
                    problems.append(f'{plugin_id}.{name} 不是 async')
        self.assertEqual(problems, [], '; '.join(problems))

    def test_hook_coverage_is_twelve_and_host_hardcoded_calls_match(self):
        """钩子名单必须覆盖全部 12 个入口，且 describe / generate 按宿主调用方式核对。"""
        self.assertEqual(ALL_HOOKS, frozenset({
            'on_prepare', 'on_pre_ai', 'on_post_reply', 'on_ingest', 'on_sent', 'tick',
            'on_block', 'on_observed', 'validate_settings', 'post_load',
            'describe', 'generate'}))
        problems = []
        for (plugin_id, name), expected in HOST_HARDCODED.items():
            plugin = self.manager._plugins.get(plugin_id)
            if plugin is None:
                problems.append(f'{plugin_id} 未加载，无法核对 {name}')
                continue
            method = getattr(plugin, name, None)
            if method is None:
                problems.append(f'{plugin_id}.{name} 不存在')
                continue
            names = positional(method)
            if names != expected:
                problems.append(f'{plugin_id}.{name}{names} != {expected}')
            if not inspect.iscoroutinefunction(method):
                problems.append(f'{plugin_id}.{name} 不是 async（宿主直接 await）')
        self.assertEqual(problems, [], '宿主硬编码调用与插件签名不一致：' + '; '.join(problems))


class HookContractTests(unittest.IsolatedAsyncioTestCase):
    """每个新钩子的正例 + 负例；负例断言"宿主产生了后果"，不是"没抛异常"。"""

    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.manager = PluginManager(Path(self.tmp.name) / 'plugins')
        self.runtime = SimpleNamespace(reply_meta=None, note=lambda value: None)

    async def asyncTearDown(self):
        self.tmp.cleanup()

    def install(self, plugin_id, body):
        """把插件真正装进 custom_plugins_dir 再 reload —— 走宿主的真实加载路径。"""
        self.manager.custom_plugin_path(plugin_id).write_text(
            CUSTOM_PLUGIN.replace('__PID__', repr(plugin_id)).replace('__BODY__', body), 'utf-8')
        self.manager.register_custom(plugin_id)
        self.manager.reload()

    def fault_of(self, plugin_id):
        rows = [row for row in self.manager.health() if row['id'] == plugin_id]
        self.assertEqual(len(rows), 1, f'{plugin_id} 应当恰好有一条隔离记录：{self.manager.health()}')
        return rows[0]

    def fakes(self, **methods):
        """构造一批最小插件实例（只实现被点名的那一个钩子）。"""
        values = {}
        for plugin_id, hooks in methods.items():
            cls = type(plugin_id, (PluginBase,), dict(META=PluginMeta(plugin_id, plugin_id, ''), **hooks))
            values[plugin_id] = cls(self.manager)
        self.manager._plugins = values
        return values

    def test_post_load_must_be_synchronous_or_the_plugin_is_not_registered(self):
        """post_load 返回协程时必须整只隔离：半注册会留下一个没有迁移过设置的插件。"""
        self.install('async_post_load', (
            "    async def _noop(self):\n"
            "        return None\n"
            "    def post_load(self):\n"
            "        return self._noop()\n"
            "    def on_sent(self, message, reply, runtime):\n"
            "        message['sent_by_async_post_load'] = True\n"))
        self.assertNotIn('async_post_load', self.manager._plugins)
        self.assertTrue(self.manager.is_quarantined('async_post_load'))
        fault = self.fault_of('async_post_load')
        self.assertEqual((fault['stage'], fault['error_type']), ('post_load', 'TypeError'))
        message = {}
        self.manager.run_on_sent(message, '好的', self.runtime)
        self.assertNotIn('sent_by_async_post_load', message)

    def test_post_load_can_change_settings_because_it_runs_after_registration(self):
        """post_load 存在的理由：__init__ 早于注册，改设置会 Unknown plugin；这里必须能落盘。"""
        self.install('migrates_after_register', (
            "    def post_load(self):\n"
            "        self._manager.update_settings('migrates_after_register', {'enabled': True})\n"
            "        self.migrated = True\n"))
        self.assertIn('migrates_after_register', self.manager._plugins)
        self.assertFalse(self.manager.is_quarantined('migrates_after_register'))
        self.assertTrue(self.manager._plugins['migrates_after_register'].migrated)
        self.assertTrue(self.manager.get_settings('migrates_after_register')['enabled'])

    def test_validate_settings_receives_the_merged_dict_and_rejects_atomically(self):
        """签名错位与规则拒绝都必须让整次保存失败，且旧设置分毫不动。"""
        bad = type('bad_validate', (PluginBase,), dict(
            META=PluginMeta('bad_validate', 'bad_validate', '',
                config_schema={'enabled': FieldSchema(type='bool', label='启用')},
                default_settings={'enabled': False}),
            validate_settings=lambda self: None))(self.manager)
        self.manager._plugins['bad_validate'] = bad
        before = self.manager.get_settings('bad_validate')
        with self.assertRaises(TypeError):
            self.manager.update_settings('bad_validate', {'enabled': True})
        self.assertEqual(self.manager.get_settings('bad_validate'), before,
                         '校验函数签名错位时不能把一半设置写进去')
        # 真实插件规则：单字唤醒词必须被拦下，拦下后旧值不变
        with self.assertRaisesRegex(ValueError, '至少需要 2 个字符'):
            self.manager.update_settings('keyword_trigger', {'keywords': '鲸'})
        self.assertEqual(self.manager.get_settings('keyword_trigger')['keywords'], '')
        self.manager.update_settings('keyword_trigger', {'keywords': '鲸鱼娘'})
        self.assertEqual(self.manager.get_settings('keyword_trigger')['keywords'], '鲸鱼娘')

    async def test_wrong_on_block_and_on_observed_signatures_are_quarantined(self):
        """这两个钩子原来完全没被签名单覆盖：写错参数只会静默失效。"""
        def broken_block(self):
            return True

        def good_block(self, message, runtime):
            return True

        def broken_observe(self):
            raise AssertionError('签名错位的 on_observed 不该被调用成功')

        def good_observe(self, message, runtime):
            message['observed'] = True

        self.fakes(broken_block={'on_block': broken_block},
                   good_block={'on_block': good_block},
                   broken_observe={'on_observed': broken_observe},
                   good_observe={'on_observed': good_observe})
        message = {'text': '在吗'}
        self.assertTrue(self.manager.run_on_block(message, self.runtime))
        self.manager.run_on_observed(message, self.runtime)
        self.assertEqual(self.fault_of('broken_block')['stage'], 'on_block')
        self.assertEqual(self.fault_of('broken_observe')['stage'], 'on_observed')
        for plugin_id in ('broken_block', 'broken_observe'):
            self.assertEqual(self.fault_of(plugin_id)['error_type'], 'TypeError', plugin_id)
        self.assertFalse(self.manager.is_quarantined('good_block'))
        self.assertTrue(message.get('observed'), '坏插件不能拖住后面的正常插件')

    async def test_broken_or_missing_describe_marks_the_picture_unrecognized(self):
        """describe 是宿主硬编码的 await 入口：坏了必须隔离并如实标注，不能假装看过图。"""
        def broken_describe(self):
            return None

        self.manager._plugins['image_understanding'] = type(
            'image_understanding', (PluginBase,),
            dict(META=PluginMeta('image_understanding', '图片识别', ''),
                 describe=broken_describe))(self.manager)
        message = {'media': [{'type': 'image'}], 'vision_description': '上一轮的旧描述'}
        await self.manager.describe_images(message, self.runtime)
        self.assertTrue(self.manager.is_quarantined('image_understanding'))
        self.assertEqual(self.fault_of('image_understanding')['stage'], 'describe')
        self.assertEqual(self.fault_of('image_understanding')['error_type'], 'TypeError')
        self.assertNotIn('vision_description', message, '识别失败必须擦掉旧结果')
        self.assertIn('图片识别插件未加载或已隔离', message['vision_status'])
        # 插件整个不存在时，宿主同样要如实标注
        self.manager._plugins.pop('image_understanding')
        second = {'media': [{'type': 'image'}]}
        await self.manager.describe_images(second, self.runtime)
        self.assertIn('图片识别插件未加载或已隔离', second['vision_status'])
        untouched = {'text': '纯文字消息'}
        await self.manager.describe_images(untouched, self.runtime)
        self.assertNotIn('vision_status', untouched)

    async def test_generate_signature_mismatch_fails_closed_instead_of_retrying(self):
        """generate 签名错位不能被当成"模型故障"再问一次模型，也不能静默改用直连。"""
        async def broken_generate(self, engine):
            return '签名错位：宿主传不进 conversation'

        self.manager._plugins['chat_controls'] = type(
            'chat_controls', (PluginBase,),
            dict(META=PluginMeta('chat_controls', '对话控制', ''),
                 generate=broken_generate))(self.manager)
        engine = RecordingEngine()
        reply = await self.manager.generate_reply(engine, [{'role': 'user', 'content': '在吗'}])
        self.assertEqual(reply, '', '策略插件坏了必须 fail-closed')
        self.assertEqual(engine.calls, [], '签名错位不应该落成一次模型调用')
        fault = self.fault_of('chat_controls')
        self.assertEqual((fault['stage'], fault['error_type']), ('generate', 'TypeError'))
        self.assertEqual(self.manager.blocked_policies(), ['chat_controls'])

    async def test_real_chat_controls_generate_uses_fallback_model_and_missing_plugin_direct(self):
        """真实 generate：空回复 + HTTP 429 时换备用模型；插件缺席时宿主直连引擎。"""
        self.manager.update_settings('chat_controls', {'fallbackModels': 'backup-model'})
        conversation = [{'role': 'user', 'content': '在吗'}]
        engine = RecordingEngine()

        async def chat(turns, **kwargs):
            engine.calls.append(kwargs.get('model'))
            return '' if len(engine.calls) == 1 else '备用模型给的话'

        engine.chat = chat
        engine.last_error = 'HTTP 429 Too Many Requests'
        self.assertEqual(await self.manager.generate_reply(engine, conversation), '备用模型给的话')
        self.assertEqual(engine.calls, [None, 'backup-model'])
        self.assertFalse(self.manager.is_quarantined('chat_controls'))
        self.manager._plugins.pop('chat_controls')
        direct = RecordingEngine('直连回答')
        self.assertEqual(await self.manager.generate_reply(direct, conversation), '直连回答')
        self.assertEqual(direct.calls, [None])


if __name__ == '__main__':
    unittest.main()
