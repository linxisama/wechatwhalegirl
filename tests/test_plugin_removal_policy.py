"""风险能力下线、保留插件移除/恢复及真实本地回复行为。"""
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
import httpx
from src.plugins.manager import PluginManager,PLUGIN_MODULES
from src.plugin_policy import RETIRED_PLUGINS,CORE_PLUGINS
from src.plugin_installer import check_source,normalize_plugin_id,InstallError
from src.wechat_app import create_app
from test_wechat_app_flow import RuntimeTestCase,raw_message,GROUP


class RemovalPolicyTests(unittest.TestCase):
    def test_retired_modules_never_load_even_from_old_custom_list(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);(root/'custom_plugins.json').write_text(json.dumps(list(RETIRED_PLUGINS)))
            manager=PluginManager(root)
            self.assertTrue(RETIRED_PLUGINS.isdisjoint(PLUGIN_MODULES))
            self.assertTrue(RETIRED_PLUGINS.isdisjoint(manager._plugins))
            # 与注册表长度对齐，而不是写死 32：新增内置插件时不该让这条用例变红
            self.assertEqual(len(manager.list_plugins()),len(PLUGIN_MODULES))
            self.assertTrue(CORE_PLUGINS.issubset(manager._plugins))
            self.assertNotIn('interjectPercent',next(v for v in manager.list_plugins() if v['id']=='chat_controls')['schema'])

    def test_source_inspection_rejects_direct_wechat_and_proactive_send(self):
        base='from src.plugins import PluginBase,PluginMeta\n'
        for action in ('import wechatauto','bot.AtAll()','runtime.queue_outgoing("x")',
                       'runtime.transport.send_to("x","y")','bot.ForwardMessage("x")'):
            with self.subTest(action=action):self.assertTrue(check_source(base+action))
        for pid in RETIRED_PLUGINS:
            with self.assertRaises(InstallError):normalize_plugin_id(pid)
            self.assertTrue(check_source(base+f'x=PluginMeta(id="{pid}",name="旧插件",description="")'))

    def test_removal_persists_and_restores_original_settings_without_partial_disk_save(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);manager=PluginManager(root)
            manager.update_settings('quick_reply',{'enabled':True})
            original=manager.get_settings('quick_reply')
            manager.set_removed('quick_reply')
            self.assertFalse(manager._plugins['quick_reply'].enabled)
            manager=PluginManager(root)
            self.assertFalse(manager._plugins['quick_reply'].enabled)
            self.assertEqual(manager.get_settings('quick_reply'),original)
            with self.assertRaises(ValueError):manager.update_settings('quick_reply',{'enabled':True})
            before=manager._removed_path.read_bytes()
            with patch('src.persistence.atomic_text',side_effect=OSError('disk full')):
                with self.assertRaises(OSError):manager.set_removed('quick_reply',False)
            self.assertEqual(manager._removed_path.read_bytes(),before)
            self.assertFalse(manager._plugins['quick_reply'].enabled)
            manager.set_removed('quick_reply',False)
            self.assertTrue(manager._plugins['quick_reply'].enabled)
            self.assertEqual(manager.get_settings('quick_reply'),original)

    def test_corrupt_removal_registry_does_not_reenable_optional_plugins(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);(root/'removed_plugins.json').write_text('broken')
            manager=PluginManager(root)
            for pid,plugin in manager._plugins.items():
                if pid not in CORE_PLUGINS:self.assertFalse(plugin.enabled,pid)
            self.assertTrue(manager._plugins['conversation_memory'].enabled)


class RemovalHttpFlowTests(RuntimeTestCase):
    async def asyncSetUp(self):
        await self.start_runtime()
        app=create_app(self.runtime);app.state.bot=self.runtime
        self.client=httpx.AsyncClient(transport=httpx.ASGITransport(app),base_url='http://127.0.0.1')

    async def asyncTearDown(self):
        await self.client.aclose();await super().asyncTearDown()

    async def test_remove_restore_immediate_reply_and_reject_core_removal(self):
        bot=self.runtime;bot.plugins.update_settings('keyword_reply',{'enabled':True,'rules':'验收暗号=命中回复'})
        response=await self.client.delete('/api/plugins/keyword_reply')
        self.assertTrue(response.json()['success'])
        bot.ingest(raw_message('验收暗号',local_id=1))
        self.assertEqual(self.pending_count(),0)
        self.assertEqual(bot.bridge.sent_log,[])
        response=await self.client.post('/api/plugins/keyword_reply/restore')
        self.assertTrue(response.json()['success'])
        bot.ingest(raw_message('验收暗号',local_id=2));await self.wait_for_send()
        self.assertEqual(bot.bridge.sent_log[-1]['text'],'命中回复')
        for pid in CORE_PLUGINS:
            self.assertEqual((await self.client.delete('/api/plugins/'+pid)).status_code,403)
        self.assertFalse((await self.client.post('/api/plugins/repeat_echo/restore')).json().get('success',False))

    async def test_legacy_probability_cannot_reply_to_unaddressed_group(self):
        bot=self.runtime;bot.plugins.update_settings('chat_controls',{'interjectPercent':100})
        bot.ingest(raw_message('大家都在看这个新闻吗',local_id=1))
        bot.ingest(raw_message('？？？',local_id=2));bot.ingest(raw_message('？？？',sender='wxid_b',local_id=3))
        bot.ingest(raw_message('复读消息',local_id=4));bot.ingest(raw_message('复读消息',sender='wxid_b',local_id=5))
        self.assertEqual(self.pending_count(),0);self.assertEqual(bot.bridge.sent_log,[])
        self.install_ai('正常触发后的回复')
        bot.ingest(raw_message('鲸鱼娘 请分析这个新闻',local_id=6));await self.wait_for_send()
        self.assertEqual(bot.bridge.sent_log[-1]['text'],'正常触发后的回复')

    async def test_removal_cancels_queued_reply_from_that_plugin(self):
        bot=self.runtime;await bot.stop()
        bot.running=True;bot.bridge.online=False
        bot.plugins.update_settings('keyword_reply',{'enabled':True,'rules':'验收暗号=不应补发'})
        bot.ingest(raw_message('验收暗号',local_id=1));self.assertEqual(self.pending_count(),1)
        await self.client.delete('/api/plugins/keyword_reply')
        self.assertEqual(self.pending_count(),0)
        self.assertEqual(bot.bridge.sent_log,[])
