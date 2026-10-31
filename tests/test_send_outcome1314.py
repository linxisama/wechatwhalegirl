"""发送证据穿过子进程、桥、传输与队列；所有发送使用替身。"""
import asyncio
import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, Mock, patch

from src.send_outcome import SendFailure, SendOutcome, failure_state, not_submitted
from src.wechat_sender import IsolatedWeChatSender, SendResult, interrupted_diagnostic
from src.wechat_transports import WeChatTransport
from tests.test_wechat_app_flow import RuntimeTestCase, raw_message


class SubmissionEvidenceTests(unittest.TestCase):
    def test_tuple_interface_preserves_old_unpacking(self):
        result = not_submitted('未发送')
        ok, text = result
        self.assertEqual((ok, text), (False, '未发送'))
        self.assertIsInstance(result, tuple)
        self.assertEqual(result.diagnostic['submission_state'], 'not_submitted')

    def test_missing_or_corrupt_status_is_uncertain(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'status.json'
            self.assertEqual(interrupted_diagnostic(path)['submission_state'], 'uncertain')
            path.write_text('{broken', encoding='utf-8')
            self.assertEqual(interrupted_diagnostic(path)['submission_state'], 'uncertain')

    def test_timeout_keeps_pre_submit_evidence_without_resending(self):
        with tempfile.TemporaryDirectory() as folder:
            process = Mock()
            process.wait.side_effect = [subprocess.TimeoutExpired('worker', 24), 0]
            process.poll.return_value = 0
            def spawn(command, **kwargs):
                Path(command[-1]).with_suffix('.status.json').write_text(
                    json.dumps({'stage': 'readback', 'send_key': False}), encoding='utf-8')
                return process
            with patch('src.wechat_sender.subprocess.Popen', side_effect=spawn) as popen, \
                 patch('src.wechat_sender.log_send_failure'):
                result = IsolatedWeChatSender(Path(folder)).SendMsg('秘密正文', '测试群')
            self.assertEqual(result.diagnostic['submission_state'], 'not_submitted')
            self.assertFalse(result.diagnostic['send_key'])
            self.assertEqual(popen.call_count, 1)
            self.assertNotIn('秘密正文', str(result))

    def test_inspection_timeout_does_not_log_a_send_failure(self):
        with tempfile.TemporaryDirectory() as folder:
            process = Mock()
            process.wait.side_effect = [subprocess.TimeoutExpired('worker', 24), 0]
            process.poll.return_value = 0
            with patch('src.wechat_sender.subprocess.Popen', return_value=process), \
                 patch('src.wechat_sender.log_send_failure') as failure:
                result = IsolatedWeChatSender(Path(folder)).inspect_environment()
            failure.assert_not_called()
            self.assertFalse(result.requires_reconnect)
            self.assertEqual(result.diagnostic['submission_state'], 'not_submitted')

    def test_process_creation_failure_is_explicitly_not_submitted(self):
        with tempfile.TemporaryDirectory() as folder:
            with patch('src.wechat_sender.subprocess.Popen', side_effect=OSError('cannot launch')):
                result = IsolatedWeChatSender(Path(folder)).SendMsg('秘密正文', '测试群')
            self.assertEqual(result.diagnostic['submission_state'], 'not_submitted')
            self.assertFalse(result.requires_reconnect)
            self.assertFalse(list((Path(folder) / 'send_jobs').iterdir()))

    def test_partial_bubbles_or_media_remain_uncertain(self):
        error = SendFailure('未按键', {'submission_state': 'not_submitted'})
        self.assertEqual(failure_state(error, sending=True), 'failed')
        self.assertEqual(failure_state(error, sending=True, chunks_sent=1), 'uncertain')
        self.assertEqual(failure_state(error, sending=False, chunks_sent=1), 'uncertain')
        partial = SendFailure('图片未发', {'submission_state': 'not_submitted', 'partial_content': True})
        self.assertEqual(failure_state(partial, sending=True), 'uncertain')
        self.assertEqual(failure_state(RuntimeError('旧接口无证据'), sending=True), 'uncertain')


class TransportEvidenceTests(unittest.IsolatedAsyncioTestCase):
    async def test_preflight_early_exits_keep_not_submitted_evidence(self):
        bridge = Mock(online=False)
        transport = WeChatTransport(bridge, send_interval=0)
        try:
            outcomes = [await transport.send_to('target', ''),
                        await transport.send_to('target', '正文'),
                        await transport.send_files('target', []),
                        await transport.send_files('target', ['file.png'])]
            self.assertTrue(all(r.diagnostic['submission_state'] == 'not_submitted' for r in outcomes))
            bridge.send_text.assert_not_called()
            bridge.send_files.assert_not_called()
        finally:
            transport._sender.shutdown(wait=True)

    async def test_text_failure_keeps_evidence(self):
        bridge = Mock()
        bridge.send_text.return_value = not_submitted('当前会话不是目标')
        transport = WeChatTransport(bridge, send_interval=0)
        try:
            result = await transport.send({'target': 'wxid_target'}, '答复')
            self.assertEqual(result.diagnostic['submission_state'], 'not_submitted')
        finally:
            transport._sender.shutdown(wait=True)

    async def test_sent_text_followed_by_failed_image_records_partial_content(self):
        bridge = Mock(background_send=False)
        bridge.send_text.return_value = SendOutcome(True, '已提交')
        bridge.send_files.return_value = not_submitted('图片格式不支持')
        transport = WeChatTransport(bridge, send_interval=0)
        try:
            result = await transport.send({'target': 'wxid_target'}, '答复',
                {'media': [{'type': 'image', 'data': {'file': 'image.png'}}]})
            self.assertFalse(result[0])
            self.assertTrue(result.diagnostic['partial_content'])
        finally:
            transport._sender.shutdown(wait=True)


class QueueEvidenceTests(RuntimeTestCase):
    async def test_archive_error_does_not_erase_successful_send(self):
        bot = await self.start_runtime()
        self.install_ai('已提交的答复。')
        with patch.object(bot.conversations, 'sent', side_effect=OSError('disk full')):
            bot.ingest(raw_message('鲸鱼娘 解释月球潮汐', local_id=1314002))
            await self.wait_for_inbox_done()
        self.assertEqual(self.states(), ['sent'])
        error = bot.db.execute('SELECT error FROM inbox').fetchone()[0]
        self.assertIn('回复已发送，本地后续处理失败', error)
        self.assertEqual(len(bot.bridge.sent_log), 1)

    async def test_not_submitted_is_failed_instead_of_uncertain(self):
        bot = await self.start_runtime()
        self.install_ai('可以看到这条答复')
        bot.transport.send = AsyncMock(return_value=not_submitted('目标未打开，本次未发送'))
        bot.ingest(raw_message('鲸鱼娘 解释海洋环流', local_id=1314001))
        await self.wait_for_inbox_done()
        self.assertEqual(self.states(), ['failed'])
        self.assertEqual(bot.transport.send.await_count, 1)

    async def test_dsh_start_does_not_require_the_unused_api_key(self):
        bot = await self.start_runtime()
        await bot.stop()
        bot.config._config['ai']['api_key'] = ''
        bot.ai.api_key = ''
        bot.config._config['dsh'] = {'enabled': True, 'base_url': 'http://127.0.0.1:3080',
                                     'model': 'deepseek-flash', 'provider': 'deepseek-official'}
        self.assertFalse(bot.ai.configured)
        with patch.object(bot.dsh, 'generate', AsyncMock()):
            await bot.start()
        self.assertTrue(bot.running)
