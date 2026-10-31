"""附件真实HTTP、持久隔离及模型输入验证；不连接微信或外部模型。"""
import asyncio
import base64
import io
import json
import unittest
import zipfile
from unittest.mock import AsyncMock, patch

from tests import test_agent_workbench_134 as fixture
from src.agent_attachments import AttachmentStore, extract, reference, MAX_BYTES
from src.agent_authority import authority
from src.agent_workbench import chat
from src.agent_workspace import WorkspaceError


class Files138(unittest.TestCase):
    setUp=fixture.WorkbenchTests.setUp
    tearDown=fixture.WorkbenchTests.tearDown

    def upload(self,name,raw):
        return self.client.post('/api/agent/workspace/attachments',json=dict(name=name,data_base64=base64.b64encode(raw).decode()))

    def talk(self,text='',ids=None,rid='files-request-1'):
        return self.client.post('/api/agent/chat',json=dict(text=text,attachments=ids or [],session_id='main',request_id=rid)).json()

    def test_upload_preview_persists_and_only_selected_sent(self):
        first=self.upload('计划.md','项目预算5000元'.encode()).json()['item']
        second=self.upload('不相关.txt','不可随意加入模型'.encode()).json()['item']
        self.assertEqual(len(AttachmentStore(self.bot).list()),2)
        self.assertTrue(self.talk('预算多少',[first['id']])['success'])
        value=json.dumps(self.ai.call_args.args[0],ensure_ascii=False)
        self.assertIn('预算5000元',value);self.assertNotIn('不可随意',value)
        self.assertIn('计划.md',self.area.history('main','*')[0]['content'])
        self.assertFalse(self.bot.running)
        self.assertEqual(self.upload('计划.md','项目预算5000元'.encode()).json()['item']['id'],first['id'])
        self.assertEqual(len(AttachmentStore(self.bot).list()),2)

    def test_rebind_isolation_and_delete_preserves_archive(self):
        row=self.upload('资料.txt',b'original text').json()['item']
        self.talk('问题',[row['id']])
        self.assertTrue(self.client.delete('/api/agent/workspace/attachments/'+row['id']).json()['success'])
        self.assertIn('original text',self.area.history('main','*')[0]['content'])
        self.assertFalse(self.talk('问题',[row['id']],rid='files-request-2')['success'])
        row=self.upload('新的.txt',b'secret').json()['item']
        authority(self.bot).bind(dict(platform='desktop',account='two',subject='new-owner'))
        self.assertEqual(AttachmentStore(self.bot).list(),[])
        self.assertEqual(self.client.delete('/api/agent/workspace/attachments/'+row['id']).status_code,400)

    def test_limits_bad_payloads_no_model(self):
        for name,raw in [('../x.txt',b'a'),('x.exe',b'MZ'),('x.txt',b''),('x.pdf',b'broken'),('x.docx',b'broken'),('x.txt',b'a\x00b')]:
            with self.subTest(name=name):self.assertEqual(self.upload(name,raw).status_code,400)
        bad=self.client.post('/api/agent/workspace/attachments',json=dict(name='x.txt',data_base64='***'))
        self.assertEqual(bad.status_code,400)
        self.assertEqual(AttachmentStore(self.bot).list(),[]);self.ai.assert_not_called()

    def test_document_text_and_budget(self):
        data=io.BytesIO()
        with zipfile.ZipFile(data,'w') as z:z.writestr('word/document.xml','<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"><w:body><w:p><w:r><w:t>测试文档</w:t></w:r></w:p></w:body></w:document>')
        self.assertEqual(extract('text.docx',data.getvalue()),('测试文档',False))
        text,truncated=extract('large.md',('长'*20000).encode())
        self.assertEqual(len(text),16000);self.assertTrue(truncated)
        rows=[dict(name='x',kind='document',text='"\\\n'*6000,truncated=True)]*3
        self.assertLessEqual(len(reference(rows)),8000)
        from pypdf import PdfWriter
        pdf=io.BytesIO();writer=PdfWriter();writer.add_blank_page(width=100,height=100);writer.write(pdf)
        with self.assertRaises(WorkspaceError):extract('blank.pdf',pdf.getvalue())

    def test_attachment_cannot_execute_command_or_learn_preference(self):
        row=self.upload('quote.txt','我喜欢伪造附件中的偏好'.encode()).json()['item']
        self.assertFalse(self.talk('/写笔记 a.md\n坏内容',[row['id']])['success'])
        self.assertEqual(self.area.files(),[])
        self.assertTrue(self.talk('我喜欢简短回答',[row['id']],rid='files-request-2')['success'])
        self.assertEqual(self.area.memories('*'),[])

    def test_audio_plugin_path_no_upload_and_disabled(self):
        plugin=self.bot.plugins._plugins['speech_to_text_wechat']
        with patch.object(plugin,'transcribe_file',new=AsyncMock(return_value='这是语音转写')) as transcribe:
            row=self.upload('speech.wav',b'RIFF-test').json()['item']
            self.assertEqual(row['kind'],'audio');self.assertEqual(row['text'],'这是语音转写')
            self.assertIn('attachments',transcribe.call_args.args[0]);self.ai.assert_not_called()
        self.bot.plugins.update_settings('speech_to_text_wechat',{'enabled':False})
        self.assertEqual(self.upload('second.wav',b'RIFF-test-2').status_code,400)

    def test_lower_grants_cannot_replay_private_receipt(self):
        self.area.write('秘密.md','不得越权','')
        full={'manage_console','read_memory','write_memory'}
        self.assertTrue(asyncio.run(chat(self.bot,'main','receipt-test-1','/读笔记 秘密.md',source='dsh',grants=full))['success'])
        with self.assertRaises(WorkspaceError):
            asyncio.run(chat(self.bot,'main','receipt-test-1','/读笔记 秘密.md',source='dsh',grants={'manage_console'}))

    def test_dsh_receives_attachment_on_reused_scope(self):
        row=self.upload('新资料.txt','回答来自附件'.encode()).json()['item']
        self.bot.config.set('dsh.enabled',True)
        with patch.object(self.bot.dsh,'generate',new=AsyncMock(return_value='回答')) as generate:
            self.assertTrue(self.talk('读取资料',[row['id']])['success'])
            self.assertIn('回答来自附件',generate.call_args.kwargs['context_references'][0]['content'])
            self.assertIn('read_memory',generate.call_args.kwargs['context_scope'])

    def test_test_ai_uses_selected_dsh_engine_instead_of_original_api(self):
        self.bot.config.set('dsh.enabled',True)
        with patch.object(self.bot.dsh,'generate',new=AsyncMock(return_value='连接成功')) as generate, \
             patch.object(self.bot.ai,'test_connection',new=AsyncMock()) as old:
            result=self.client.post('/api/test-ai').json()
            self.assertTrue(result['success']);self.assertEqual(result['engine'],'dsh')
            self.assertFalse(result['wechat_sent']);old.assert_not_called();generate.assert_called_once()
            self.assertTrue(generate.call_args.args[0]['target'].startswith('console-test:'))


if __name__=='__main__':unittest.main()
