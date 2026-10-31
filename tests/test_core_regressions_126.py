"""针对核心故障的最小复现；测试不发送真实微信消息。"""
import asyncio
import json
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch
import httpx
from src.ai_engine import AIEngine
from src.plugins.manager import PluginManager
from src.wechat_transports import message_from_raw
from src.wechat_bridge import WeChatBridge
from tests.test_wechat_app_flow import RuntimeTestCase, raw_message

class AtAndMediaTests(unittest.TestCase):
 def setUp(self):
  self.tmp=tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
  self.bridge=WeChatBridge(Path(self.tmp.name), backend='mock')
  self.bridge.account_name='鲸鱼娘';self.bridge.self_id='wxid_bot'
 def raw(self,**kw):
  value=dict(username='room@chatroom',sender_username='wxid_user',local_id=9,shard='s',type='文本',content='')
  value.update(kw);return value
 def test_image_caption_at_is_not_erased(self):
  r=self.raw();r.update(type='图片',content='@鲸鱼娘 看看这张图')
  m=message_from_raw(r,self.bridge,[])
  self.assertTrue(m['at']);self.assertIn('看看这张图',m['text']);self.assertEqual(m['media'][0]['type'],'image')
 def test_at_to_longer_name_is_not_at_bot(self):
  self.assertFalse(message_from_raw(self.raw(content='@鲸鱼娘测试号 你好'),self.bridge,[])['at'])
 def test_structured_at_matches_self_without_guessing_group_nickname(self):
  r=self.raw(content='@群内昵称 你好');r['at_ids']=['wxid_bot']
  m=message_from_raw(r,self.bridge,[])
  self.assertTrue(m['at']);self.assertEqual(m['at_ids'],['wxid_bot'])
 def test_other_users_at_metadata_does_not_trigger(self):
  r=self.raw(content='@小明 你好');r['at_ids']=['wxid_other']
  self.assertFalse(message_from_raw(r,self.bridge,[])['at'])
 def test_xml_attributes_are_not_caption_or_text_mentions(self):
  r=self.raw();r.update(type='图片',content='wxid_user:\n<msg><img comment="@鲸鱼娘 " /></msg>')
  m=message_from_raw(r,self.bridge,[])
  self.assertEqual(m['text'],'[图片]');self.assertFalse(m['at'])
 def test_full_width_at_and_space_are_supported(self):
  self.assertTrue(message_from_raw(self.raw(content='＠鲸鱼娘\u2005你好'),self.bridge,[])['at'])

class CompletionBlocksTests(unittest.TestCase):
 def test_openai_text_blocks_are_text_not_python_repr_or_reasoning(self):
  e=AIEngine('https://example.test/v1','fake')
  data={'choices':[{'message':{'content':[{'type':'thinking','thinking':'内部分析'},{'type':'text','text':'蓝色'},{'type':'text','text':'鲸鱼'}]}}]}
  answer,_=e._read_completion(data)
  self.assertEqual(answer,'蓝色\n鲸鱼')
 def test_empty_or_nontext_blocks_do_not_become_fake_answers(self):
  e=AIEngine('https://example.test/v1','fake')
  for content in (None,{},[{'type':'reasoning','text':'分析'}],[{'type':'text','text':None}]):
   with self.subTest(content=content):
    self.assertEqual(e._read_completion({'choices':[{'message':{'content':content}}]})[0],'')

class PluginIsolationTests(unittest.IsolatedAsyncioTestCase):
 async def asyncSetUp(self):
  self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
  self.manager=PluginManager(Path(self.tmp.name))
 async def test_bad_enabled_property_cannot_break_post_reply_or_tick(self):
  class Broken:
   META=SimpleNamespace(id='broken')
   @property
   def enabled(self):raise ValueError('坏配置')
  class Healthy:
   META=SimpleNamespace(id='healthy');enabled=True
   async def on_post_reply(self,msg,text,rt):return text+' 正常'
   async def tick(self,rt):rt.append('tick')
  self.manager._plugins={'broken':Broken(),'healthy':Healthy()}
  self.assertEqual(await self.manager.run_on_post_reply({},'回答',None),'回答 正常')
  calls=[];await self.manager.run_tick(calls);self.assertEqual(calls,['tick'])
 async def test_successful_vision_survives_optional_description_storage_error(self):
  from PIL import Image
  p=Path(self.tmp.name)/'picture.png';Image.new('RGB',(32,32),'blue').save(p)
  runtime=SimpleNamespace(mode='wechat',account=lambda:'bot',note=Mock(),
   ai=SimpleNamespace(api_url='https://example.test/v1',model='vision',describe_images=AsyncMock(return_value='蓝色方块')),
   transport=SimpleNamespace(call=AsyncMock(return_value={'ok':True,'path':str(p)})),
   stickers=SimpleNamespace(record_description=Mock(side_effect=OSError('只读磁盘'))))
  msg={'kind':'private','target':'wxid_user','text':'[图片]','media':[{'type':'image','local_id':9,'shard':'s'}]}
  await self.manager.describe_images(msg,runtime)
  self.assertEqual(msg['vision_status'],'ok');self.assertIn('蓝色方块',msg['vision_description'])

class PipelineRegressionTests(RuntimeTestCase):
 async def until_done(self):
  async with asyncio.timeout(2):
   while True:
    states=[v[0] for v in self.runtime.db.execute('SELECT state FROM inbox')]
    if states and all(v in {'sent','expired','failed'} for v in states):return states
    await asyncio.sleep(.01)
 async def test_unavailable_window_does_not_hold_expired_queue_forever(self):
  runtime=await self.start_runtime();ai=self.install_ai()
  runtime.bridge.backend='wechatauto'
  runtime.bridge.send_environment={'ready':False,'reason':'没有输入控件'}
  with patch('src.interactive_desktop.interactive_desktop_status',return_value=(True,'')), patch.object(runtime.bridge,'inspect_send_environment',return_value=runtime.bridge.send_environment):
   runtime.ingest(raw_message('@鲸鱼娘 你好',at=True))
   runtime.db.execute('UPDATE inbox SET created=?',(time.time()-1000,));runtime.db.commit()
   self.assertEqual(await self.until_done(),['expired'])
  self.assertEqual(ai.calls,[])
 async def test_ocr_and_vision_complete_concurrently_and_both_reach_chat_once(self):
  runtime=await self.start_runtime();ai=self.install_ai('图片里有红色圆形。')
  a,b=asyncio.Event(),asyncio.Event()
  async def prepare(msg,rt):
   a.set();await asyncio.wait_for(b.wait(),.5)
   msg['text']+='\n[本地OCR] 四位数字';msg['vision_text']='四位数字'
  async def vision(msg,rt):
   b.set();await asyncio.wait_for(a.wait(),.5)
   msg.update(vision_status='ok',vision_description='红色圆形')
  with patch.object(runtime.plugins,'run_on_prepare',side_effect=prepare), patch.object(runtime.plugins,'describe_images',side_effect=vision):
   raw=raw_message('[图片]',kind='private',chat='wxid_user');raw['type']='图片'
   runtime.ingest(raw)
   self.assertEqual(await self.until_done(),['sent'])
  body=ai.calls[0][0][-1]['content']
  self.assertEqual(body.count('红色圆形'),1);self.assertEqual(body.count('四位数字'),1)
