"""真实微信 source 的压缩/SQLite TEXT 回归；只在内存 SQLite 和控件替身中测试。"""
import sqlite3,tempfile,unittest,time
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch, AsyncMock
import zstandard
from src.wechat_listener import ListenerDatabase
from src.wechat_send_worker import find_scoped
from src.wechat_transports import message_from_raw
from src.wechat_bridge import WeChatBridge

class SourceBridgeTests(unittest.TestCase):
 def setUp(self):
  self.conn=sqlite3.connect(':memory:');self.conn.row_factory=sqlite3.Row
  self.conn.text_factory=lambda b:b.decode('utf-8','replace')
  self.addCleanup(self.conn.close)
  self.conn.execute('CREATE TABLE msg(local_id INTEGER,sort_seq INTEGER,source TEXT)')
  self.source=b'<msgsource><atuserlist><![CDATA[wxid_bot,wxid_other]]></atuserlist></msgsource>'
  packed=zstandard.ZstdCompressor().compress(self.source)
  self.conn.execute('INSERT INTO msg VALUES(7,88,CAST(? AS TEXT))',(packed,))
  self.raw={'local_id':7,'sort_seq':88,'shard':'s','type':'文本','content':'@群内不同昵称 你好','sender_username':'wxid_user'}
  self.db=Mock()
  self.db.get_new_messages.return_value=[self.raw]
  self.db._shard_label.side_effect=lambda conn:'s'
  self.db._run_msg_query.side_effect=lambda user,query:query([(self.conn,'msg')])
 def test_compressed_text_source_reaches_normalizer_as_stable_at_ids(self):
  rows=ListenerDatabase(self.db,[]).get_new_messages('room@chatroom',since_seq=87)
  self.assertEqual(rows[0]['at_ids'],['wxid_bot','wxid_other'])
  self.assertNotIn('at_ids',self.raw)
  with tempfile.TemporaryDirectory() as folder:
   bridge=WeChatBridge(Path(folder),backend='mock');bridge.self_id='wxid_bot';bridge.account_name='另一个昵称'
   self.assertTrue(message_from_raw(dict(rows[0],username='room@chatroom'),bridge,[])['at'])
 def test_wrong_shard_is_not_borrowed(self):
  self.raw['shard']='wrong'
  self.assertNotIn('at_ids',ListenerDatabase(self.db,[]).get_new_messages('room@chatroom')[0])
 def test_wrong_sequence_is_not_borrowed(self):
  self.raw['sort_seq']=89
  self.assertNotIn('at_ids',ListenerDatabase(self.db,[]).get_new_messages('room@chatroom')[0])
 def test_other_users_mentions_never_match_self(self):
  self.conn.execute('UPDATE msg SET source=?',('<msgsource><atuserlist>wxid_else</atuserlist></msgsource>',))
  rows=ListenerDatabase(self.db,[]).get_new_messages('room@chatroom')
  self.assertEqual(rows[0]['at_ids'],['wxid_else'])
 def test_ordinary_private_or_already_annotated_messages_add_no_query(self):
  for user,content,ids in [('room@chatroom','普通消息',None),('wxid_user','@群昵称',None),('room@chatroom','@群昵称',['wxid_bot'])]:
   self.raw['content']=content
   if ids:self.raw['at_ids']=ids
   else:self.raw.pop('at_ids',None)
   ListenerDatabase(self.db,[]).get_new_messages(user)
  self.db._run_msg_query.assert_not_called()
 def test_decode_failure_does_not_drop_message_and_error_is_rate_limited(self):
  self.db._run_msg_query.side_effect=OSError('test metadata inaccessible')
  note=Mock();adapter=ListenerDatabase(self.db,[],note=note)
  self.assertEqual(len(adapter.get_new_messages('room@chatroom')),1)
  self.assertEqual(len(adapter.get_new_messages('room@chatroom')),1)
  self.assertEqual(note.call_count,1)
 def test_public_api_fallback_can_read_uncompressed_source(self):
  self.db._run_msg_query=None
  self.db.get_message_row.return_value={'source':self.source,'shard':'s','sort_seq':88}
  self.assertEqual(ListenerDatabase(self.db,[]).get_new_messages('room@chatroom')[0]['at_ids'],['wxid_bot','wxid_other'])
 def test_xml_entities_and_oversized_decompressed_source_are_rejected(self):
  bad=[b'<!DOCTYPE msgsource [<!ENTITY x "wxid_bot">]><msgsource><atuserlist>&x;</atuserlist></msgsource>',
       zstandard.ZstdCompressor().compress(b'<msgsource>'+b'x'*70000+b'</msgsource>'),b'\x28\xb5\x2f\xfdjunk']
  for data in bad:
   self.conn.execute('UPDATE msg SET source=?',(data,))
   self.assertNotIn('at_ids',ListenerDatabase(self.db,[]).get_new_messages('room@chatroom')[0])

class ScopedPriorityTests(unittest.TestCase):
 def node(self,children=(),field=False):return SimpleNamespace(ClassName='input' if field else 'pane',AutomationId='',GetChildren=lambda:children)
 def test_shallow_input_not_starved_by_large_chat_history_branch(self):
  field=self.node(field=True)
  history=self.node([self.node() for _ in range(2300)])
  root=self.node([field,history])
  self.assertIs(find_scoped(root,lambda c:c.ClassName=='input'),field)

 def test_deep_supported_input_is_still_found(self):
  field=self.node(field=True);root=field
  for _ in range(30):root=self.node([root])
  self.assertIs(find_scoped(root,lambda c:c.ClassName=='input'),field)

 def test_budget_and_rich_tree_are_not_reported_as_empty_shell(self):
  from src.wechat_window import inspect_window
  access=Mock()
  access.candidates.return_value=[{'hwnd':1,'pid':1,'minimized':False}]
  access.control.return_value=self.node([self.node()])
  info=inspect_window(access=access,desktop_status=lambda:(True,''))
  self.assertNotIn('只提供窗口外壳',info['reason'])
  self.assertEqual(info['input_lookup']['search_status'],'exhausted')
  access.control.return_value=self.node([self.node() for _ in range(2100)])
  info=inspect_window(access=access,desktop_status=lambda:(True,''))
  self.assertIn('预算上限',info['reason'])
  self.assertEqual(info['input_lookup']['visited'],2000)


class WaitingStatusTests(unittest.IsolatedAsyncioTestCase):
 async def test_expiry_clears_existing_waiting_status(self):
  from src.wechat_app import WeChatRuntime
  runtime=SimpleNamespace(bridge=SimpleNamespace(backend='wechatauto',send_environment={}),running=True,
                          _stage=Mock(),note=Mock())
  with patch('src.wechat_app.time.time',side_effect=[1,3]), \
       patch('src.interactive_desktop.interactive_desktop_status',return_value=(False,'锁屏')), \
       patch('src.wechat_app.asyncio.sleep',new_callable=AsyncMock):
   self.assertTrue(await WeChatRuntime._wait_for_desktop(runtime,'test',expires_at=2))
  self.assertEqual(runtime._stage.call_args.args,('idle',))
