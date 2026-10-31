"""用户补报：触发原因、长冷却、加密表情、失败后仍附图。"""
import base64
import hashlib
import io
import json
import time
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from PIL import Image
from src.image_input import unread_image_reply
from src.wechat_transports import message_from_raw
from test_wechat_app_flow import RuntimeTestCase, raw_message, GROUP


def picture():
    output=io.BytesIO();Image.new('RGB',(24,24),'orange').save(output,'PNG')
    return output.getvalue()


class TriggerEmotion153Tests(RuntimeTestCase):
    async def asyncSetUp(self):
        self.bot=await self.start_runtime()
        self.controls=self.bot.plugins._plugins['chat_controls']

    def message(self,**extra):
        message=dict(id='probe',kind='group',target=GROUP,text='普通讨论',at=False,sender_id='wxid_a')
        message.update(extra);return message

    async def test_no_trigger_is_distinguished_from_rate_limit(self):
        allowed,reason=self.controls.reply_decision(self.message(),self.bot)
        self.assertFalse(allowed);self.assertIn('0%',reason);self.assertNotIn('每分钟上限',reason)

    async def test_mention_other_person_does_not_wake_bot(self):
        allowed,reason=self.controls.reply_decision(self.message(text='@别人 吃饭吗'),self.bot)
        self.assertFalse(allowed);self.assertIn('未确认是 @ 当前机器人',reason)

    async def test_real_at_is_not_blocked_by_probability_zero(self):
        self.bot.plugins.update_settings('chat_controls',{'replyProbability':0})
        allowed,reason=self.controls.reply_decision(self.message(at=True),self.bot)
        self.assertTrue(allowed);self.assertIn('@',reason)

    async def test_processing_reply_counts_towards_rate_limit(self):
        self.bot.plugins.update_settings('chat_controls',{'perMinute':1})
        self.bot._queue(self.message(id='seed'),'测试')
        self.bot.db.execute("UPDATE inbox SET state='processing'")
        allowed,reason=self.controls.reply_decision(self.message(at=True),self.bot)
        self.assertFalse(allowed);self.assertIn('1/1',reason)

    async def test_cooldown_longer_than_minute_uses_complete_window(self):
        self.bot.plugins.update_settings('chat_controls',{'cooldownSeconds':300})
        self.bot._queue(self.message(id='seed'),'测试')
        self.bot.db.execute("UPDATE inbox SET state='sent',created=?",(time.time()-90,))
        allowed,reason=self.controls.reply_decision(self.message(at=True),self.bot)
        self.assertFalse(allowed);self.assertIn('冷却',reason)
        self.bot.db.execute('UPDATE inbox SET created=?',(time.time()-301,))
        self.assertTrue(self.controls.reply_decision(self.message(at=True),self.bot)[0])

    async def test_diagnostic_and_log_contain_specific_trigger_reason(self):
        self.bot.ingest(raw_message('普通讨论',local_id=53201))
        reason=self.bot.db.execute('SELECT reason FROM dialogue_decisions ORDER BY created DESC LIMIT 1').fetchone()[0]
        self.assertIn('0%',reason)
        self.assertFalse(self.bot.bridge.sent_log)

    async def test_unread_emotion_does_not_call_text_model_or_send_auto_picture(self):
        ai=self.install_ai(boom=True)
        library=self.bot.plugins._plugins['image_library']
        # 即便后处理插件错误地附图，最终发送闸前也必须去掉它。
        with patch.object(library,'on_post_reply',AsyncMock(return_value=('内部错误 cdnurl',
                {'media':[{'type':'image','url':'https://example.com/irrelevant.png'}],'library_ids':['fake']}))):
            raw=raw_message('<msg><emoji md5="'+'a'*32+'"/></msg>',at=True,local_id=53202)
            raw['type']='动画表情'
            self.bot.ingest(raw)
            await self.wait_for_send()
        self.assertEqual(ai.calls,[])
        reply=self.bot.bridge.sent_log[-1]['text']
        self.assertIn('表情暂时无法读取',reply)
        self.assertNotIn('cdnurl',reply)
        payload=json.loads(self.bot.db.execute('SELECT payload FROM inbox').fetchone()[0])
        self.assertFalse((payload.get('_reply_meta') or {}).get('media'))
        self.assertFalse((payload.get('_reply_meta') or {}).get('library_ids'))

    async def test_matching_registered_original_makes_no_cdn_emotion_readable(self):
        data=picture();self.bot.stickers.put(data,'橙色原图')
        self.bot.ai.describe_images=AsyncMock(return_value='橙色方块')
        plugin=self.bot.plugins._plugins['image_understanding']
        message=self.message(text='[表情]',media=[{'type':'emotion','md5':hashlib.md5(data).hexdigest()}])
        with patch.object(self.bot.transport,'call',AsyncMock(side_effect=AssertionError('不应下载加密文件'))):
            await plugin.describe(message,self.bot)
        self.assertEqual(message['vision_status'],'ok')
        self.assertIn('橙色方块',message['vision_description'])
        self.bot.ai.describe_images.assert_awaited_once()

    async def test_md5_match_checks_sha256_and_handles_legacy_index(self):
        data=picture();entry,_=self.bot.stickers.put(data,'匹配校验')
        self.bot.stickers._entries[entry['id']].pop('md5',None)
        digest=hashlib.md5(data).hexdigest()
        self.assertEqual(self.bot.stickers.matching_md5(digest),data)
        self.bot.stickers.path(entry['id']).write_bytes(data+b'tampered')
        self.assertEqual(self.bot.stickers.matching_md5(digest),b'')
        self.assertEqual(self.bot.stickers.matching_md5('../secret'),b'')

    async def test_image_library_declines_auto_image_on_failed_vision(self):
        library=self.bot.plugins._plugins['image_library']
        message=self.message(vision_status='失败：请求超时')
        result=await library.on_post_reply(message,'这次没读出 [[表情:开心]]',self.bot)
        self.assertIsInstance(result,str);self.assertNotIn('[[',result)
        self.assertIn('不附图',library.on_pre_ai(message,[],self.bot)['extra_system'])


class EmotionText153Tests(unittest.TestCase):
    def test_error_does_not_expose_internal_details_in_chat(self):
        reply=unread_image_reply({'text':'[表情]','vision_status':'失败：本地加密，无地址 cdnurl；内部路径 api_key=secret'})
        self.assertLess(len(reply),60)
        for detail in ('cdnurl','secret','本地加密','内部路径'):self.assertNotIn(detail,reply)

    def test_text_question_is_still_handled_by_model(self):
        self.assertEqual(unread_image_reply({'text':'这个猫什么意思 [表情]',
            'vision_status':'失败：本地加密，无地址','_unavailable_emotion':True}),'')

    def test_single_quoted_emoji_xml_preserves_download_and_hash(self):
        bridge=SimpleNamespace(self_id='wxid_bot',account_name='鲸鱼娘',display_name=lambda value,*args:value)
        raw=raw_message("<msg><emoji md5='"+'a'*32+"' cdnurl='https://cdn.test/original.gif'/></msg>")
        raw['type']='动画表情'
        message=message_from_raw(raw,bridge,[])
        self.assertEqual(message['media'][0]['md5'],'a'*32)
        self.assertEqual(message['media'][0]['url'],'https://cdn.test/original.gif')


if __name__=='__main__':unittest.main()
