"""重启真实运行时验证保存；密码经入站链路验证不进入数据库或模型。"""
import asyncio
import base64
import io
import hashlib
import json
import time
import unittest
from pathlib import Path
from unittest.mock import patch
import httpx
from PIL import Image

from test_wechat_app_flow import RuntimeTestCase
from src.wechat_app import WeChatRuntime,create_app
from src.wechat_management import bind_owner
from src.persona_files import save_image,import_persona,export_persona
from src.gui.config import save_gui_config,load_gui_config
from src.admin_sessions import AdminSessions


class RestartTests(RuntimeTestCase):
    async def test_persona_model_plugins_and_appearance_survive_restart(self):
        bot=await self.start_runtime()
        bot.style.save_persona({'name':'wizard','display_name':'法师','keywords':['法师大人','魔法师'],'system_prompt':'你是法师','image_note':'蓝色斗篷'})
        bot.activate_persona('wizard')
        bot.config.set('ai.model','my-model')
        bot.config.set('wechat_only.groups',['one@chatroom'])
        bot.config.save()
        bot.plugins.update_settings('private_replies',{'mode':'allow','contacts':'wxid_yes'})
        cfg=self.root/'data/gui_config.json'
        save_gui_config(cfg,{'panel_transparency':.78,'background_image':'data/backgrounds/example.png'})
        path=bot.config.config_path
        await bot.close()
        self.runtime=WeChatRuntime(root=self.root,config_path=path)
        self.assertEqual(self.runtime.style.active_name,'wizard')
        self.assertEqual(self.runtime.ai.model,'my-model')
        self.assertEqual(self.runtime._configured_groups(),['one@chatroom'])
        self.assertEqual(self.runtime.plugins.get_settings('private_replies')['mode'],'allow')
        self.assertEqual(self.runtime.aliases(),['法师大人','魔法师'])
        self.assertIn('蓝色斗篷',self.runtime.style.get().prompt_text())
        self.assertEqual(load_gui_config(cfg)['panel_transparency'],.78)

    async def test_activation_write_failure_does_not_change_active_persona(self):
        bot=await self.start_runtime()
        bot.style.save_persona({'name':'other','system_prompt':'另一人设'})
        before=bot.style.active_name
        with patch('src.persistence.os.replace',side_effect=OSError('disk full')):
            with self.assertRaises(OSError):
                bot.activate_persona('other')
        self.assertEqual(bot.style.active_name,before)
        self.assertNotEqual(bot.config.get('style.active'),'other')

    async def test_persona_words_do_not_reuse_global_aliases_for_group_wakeup(self):
        from test_wechat_app_flow import raw_message
        bot=await self.start_runtime()
        bot.style.save_persona({'name':'wizard','system_prompt':'法师','keywords':['魔法']})
        bot.activate_persona('wizard')
        bot.plugins.update_settings('chat_controls',{'aliases':'旧称呼'})
        self.install_ai(boom=True)
        bot.ingest(raw_message('旧称呼 帮我分析'))
        self.assertEqual(self.pending_count(),0)

    async def test_backup_recovers_corrupt_config_and_plugin_settings(self):
        bot=await self.start_runtime()
        bot.config.set('ai.model','saved-a')
        bot.config.save()
        bot.config.set('ai.model','saved-b')
        bot.config.save()
        bot.plugins.update_settings('keyword_reply',{'enabled':True,'rules':'一号=已保存'})
        bot.plugins.update_settings('keyword_reply',{'rules':'二号=最新值'})
        path=bot.config.config_path
        await bot.close()
        path.write_text('ai: [broken','utf-8')
        (self.root/'data/wechat-only/plugin_settings.json').write_text('{broken','utf-8')
        self.runtime=WeChatRuntime(root=self.root,config_path=path)
        self.assertEqual(self.runtime.ai.model,'saved-a')
        self.assertEqual(self.runtime.plugins.get_settings('keyword_reply')['rules'],'一号=已保存')

    async def test_persona_rename_activation_is_saved_by_http(self):
        bot=await self.start_runtime()
        bot.style.save_persona({'name':'old','system_prompt':'旧人设'})
        bot.activate_persona('old')
        app=create_app(bot);app.state.bot=bot
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app),base_url='http://127.0.0.1') as client:
            response=await client.post('/api/personas',json={'name':'new','original_name':'old','system_prompt':'新人设','keywords':'新的关键词'})
            self.assertEqual(response.status_code,200)
        path=bot.config.config_path
        await bot.close()
        self.runtime=WeChatRuntime(root=self.root,config_path=path)
        self.assertEqual(self.runtime.style.active_name,'new')
        self.assertEqual(self.runtime.aliases(),['新的关键词'])

    async def test_persona_export_import_preserves_image_and_keywords(self):
        bot=await self.start_runtime()
        image=Image.new('RGB',(24,24),'blue')
        stream=io.BytesIO();image.save(stream,format='PNG')
        name=save_image(bot,base64.b64encode(stream.getvalue()).decode())
        bot.style.save_persona({'name':'source','system_prompt':'人设正文','keywords':['星星'],'image':name,'image_note':'蓝色头发'})
        bundle=export_persona(bot,'source')
        imported=import_persona(bot,bundle,'imported')
        persona=bot.style.get_for_edit(imported)
        self.assertEqual(persona.keywords,['星星'])
        self.assertEqual(persona.image,name)
        self.assertEqual(persona.image_note,'蓝色头发')
        self.assertTrue((self.root/'data/persona_images'/name).is_file())

    async def test_import_cannot_read_local_image_paths_or_escape_name(self):
        bot=await self.start_runtime()
        imported=import_persona(bot,json.dumps({'name':'safe','system_prompt':'正文','image':'C:/secret.png'}))
        self.assertEqual(bot.style.get_for_edit(imported).image,'')
        with self.assertRaises(ValueError):
            import_persona(bot,'name: ../../outside\nsystem_prompt: x')
        with self.assertRaises(ValueError):
            bot.style.save_persona({'name':'safe2','original_name':'../../outside','system_prompt':'x'})
        with self.assertRaises(Exception):
            save_image(bot,base64.b64encode(b'not an image').decode())

    async def test_notes_are_persona_scoped_and_persisted(self):
        bot=await self.start_runtime()
        app=create_app(bot);app.state.bot=bot
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app),base_url='http://127.0.0.1') as client:
            result=await client.post('/api/dialogue/notes',json={'content':'先关心对方状态'})
            self.assertTrue(result.json()['success'])
            current=(await client.get('/api/dialogue')).json()
            self.assertEqual(current['notes'][0]['content'],'先关心对方状态')
            bot.style.save_persona({'name':'other','system_prompt':'其他人设'})
            bot.activate_persona('other')
            self.assertEqual((await client.get('/api/dialogue')).json()['notes'],[])
        self.assertEqual(bot.db.execute('SELECT count(*) FROM persona_notes').fetchone()[0],1)

    async def test_personal_facts_can_be_isolated_or_shared_only_when_enabled(self):
        bot=await self.start_runtime()
        memory=bot.plugins._plugins['conversation_memory']
        message={'id':'test','kind':'private','target':'wxid_one','sender_id':'wxid_one','text':'/记住 喜欢蓝色'}
        self.assertIn('记住了',memory.on_ingest(message,bot))
        bot.style.save_persona({'name':'wizard','system_prompt':'法师'})
        bot.activate_persona('wizard')
        self.assertIn('还没有',memory.on_ingest(dict(message,text='/记忆'),bot))
        bot.plugins.update_settings('conversation_memory',{'crossChatPreferences':True,'privateToGroup':True})
        memory.on_ingest(dict(message,id='second',text='/记住 喜欢红色'),bot)
        other_chat=dict(message,kind='group',target='other@chatroom',text='/记忆')
        self.assertIn('喜欢红色',memory.on_ingest(other_chat,bot))
        self.assertNotIn('喜欢蓝色',memory.on_ingest(other_chat,bot))


class PasswordAdminTests(RuntimeTestCase):
    async def prepare(self):
        bot=await self.start_runtime()
        bot.plugins.update_settings('private_replies',{'mode':'off'})
        bind_owner(bot,'wxid_admin',manual=True)
        bot.plugins.update_settings('robot_admin',{'enabled':True})
        bot.admin_sessions.set_password('VeryPrivate9381')
        self.install_ai(boom=True)
        self.counter=0
        return bot

    def message(self,text,target='wxid_admin',kind='private'):
        return {'kind':kind,'target':target,'sender_id':target,'text':text}

    async def send(self,text):
        from test_wechat_app_flow import raw_message
        self.counter+=1
        self.runtime.ingest(raw_message(text,chat='wxid_admin',sender='wxid_admin',local_id=300+self.counter))
        await self.wait_for_send(self.counter)

    async def test_password_is_hashed_and_never_reaches_inbox_context_or_model(self):
        bot=await self.prepare()
        await self.send('进入管理员模式')
        await self.send('VeryPrivate9381')
        self.assertTrue(bot.admin_sessions.logged_in(self.message('')))
        values=[row[0] for row in bot.db.execute('SELECT payload FROM inbox')]
        values.extend(row[0] for row in bot.db.execute('SELECT text FROM chat_ambient'))
        values+=[json.dumps(bot.recent),json.dumps(bot.events),bot.admin_sessions.path.read_text('utf-8')]
        self.assertNotIn('VeryPrivate9381','\n'.join(values))
        self.assertIn('[管理员验证消息已隐藏]','\n'.join(values))

    async def test_one_character_password_authenticates_after_reload(self):
        bot=await self.prepare()
        bot.admin_sessions.set_password('7')
        bot.admin_sessions=AdminSessions(bot)
        await self.send('进入管理员模式')
        await self.send('7')
        self.assertTrue(bot.admin_sessions.logged_in(self.message('')))
        self.assertIn('已进入管理员模式',self.inbox_rows()[-1][2])

    async def test_queries_and_pause_require_password_when_configured(self):
        bot=await self.prepare()
        await self.send('机器人暂停回复')
        self.assertFalse(bot.plugins.get_settings('robot_admin')['paused'])
        self.assertIn('验证密码',self.inbox_rows()[0][2])
        message=self.message('机器人日志')
        self.assertTrue(bot.admin_sessions.handles(message))
        self.assertIn('验证密码',bot.admin_sessions.handle(message))

    async def test_login_can_switch_persona_toggle_plugin_and_read_records(self):
        bot=await self.prepare()
        bot.style.save_persona({'name':'wizard','system_prompt':'法师','keywords':['魔法']})
        await self.send('进入管理员模式')
        await self.send('VeryPrivate9381')
        await self.send('机器人 切换人设 wizard')
        self.assertEqual(bot.style.active_name,'wizard')
        self.assertEqual(bot.config.get('style.active'),'wizard')
        await self.send('机器人 关闭插件 quick_reply')
        self.assertFalse(bot.plugins.get_settings('quick_reply')['enabled'])
        await self.send('机器人 聊天记录')
        self.assertIn('本账号最近记录',self.inbox_rows()[-1][2])

    async def test_group_and_unbound_users_cannot_authenticate(self):
        bot=await self.prepare()
        self.assertFalse(bot.admin_sessions.handles(self.message('进入管理员模式',kind='group')))
        self.assertFalse(bot.admin_sessions.handles(self.message('进入管理员模式',target='wxid_other')))

    async def test_password_whitespace_is_not_changed_during_verification(self):
        bot=await self.prepare()
        # 旧版允许空格与符号；升级不得改变已存摘要的验证语义。
        credential={'salt':'0123456789abcdef0123456789abcdef'}
        credential['digest']=hashlib.pbkdf2_hmac('sha256',b'  private-password  ',bytes.fromhex(credential['salt']),300000).hex()
        bot.admin_sessions.path.write_text(json.dumps(credential),'utf-8')
        bot.admin_sessions=AdminSessions(bot)
        bot.admin_sessions.handle(self.message('进入管理员模式'))
        response=bot.admin_sessions.handle(self.message('  private-password  '))
        self.assertIn('已进入管理员模式',response)

    async def test_failed_passwords_lock_and_sessions_expire(self):
        bot=await self.prepare()
        service=bot.admin_sessions
        for _ in range(5):
            service.handle(self.message('进入管理员模式'))
            service.handle(self.message('wrong-password'))
        self.assertIn('锁定',service.handle(self.message('进入管理员模式')))
        service.authenticated[service.identity(self.message(''))]=time.monotonic()-1
        self.assertFalse(service.logged_in(self.message('')))
        restarted=AdminSessions(bot)
        self.assertEqual(restarted.authenticated,{})
        self.assertTrue(restarted.credential)

    async def test_unbind_or_password_change_redacts_outstanding_password(self):
        bot=await self.prepare()
        bot.admin_sessions.handle(self.message('进入管理员模式'))
        bind_owner(bot,'wxid_admin',False)
        message=self.message('VeryPrivate9381')
        self.assertTrue(bot.admin_sessions.handles(message))
        bot.admin_sessions.handle(message)
        self.assertEqual(message['text'],'[管理员验证消息已隐藏]')

    async def test_corrupt_password_file_does_not_allow_legacy_bypass(self):
        bot=await self.prepare()
        bot.admin_sessions.path.write_text('{broken','utf-8')
        bot.admin_sessions=AdminSessions(bot)
        self.assertIn('验证密码',bot.admin_sessions.handle(self.message('机器人暂停回复')))
        self.assertFalse(bot.plugins.get_settings('robot_admin')['paused'])
