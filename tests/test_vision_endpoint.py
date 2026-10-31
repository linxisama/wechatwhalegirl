"""识图可以单独指向另一个接口（很多中转不支持看图）。"""
import base64
import unittest

import httpx

from src.ai_engine import AIEngine


class DescribeEndpointTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.engine = AIEngine(api_url='https://chat.example/v1/chat/completions',
                               api_key='main-key', model='chat-model')
        self.calls = []

    async def test_override_endpoint_is_used(self):
        async def fake_post(url, key, payload):
            self.calls.append((url, key, payload['model']))
            return '一只蓝色的鲸鱼'

        self.engine._post_vision = fake_post
        text = await self.engine.describe_images(
            ['data:image/png;base64,' + base64.b64encode(bytes.fromhex('89504e470d0a1a0a0000000d49484452000000010000000108060000001f15c489'
                                                       '0000000d49444154789c63600000020001e221bc330000000049454e44ae426082')).decode()], model='vl-model',
            api_url='http://127.0.0.1:11434/v1/chat/completions', api_key='vl-key')
        self.assertEqual(text, '一只蓝色的鲸鱼')
        self.assertEqual(self.calls, [('http://127.0.0.1:11434/v1/chat/completions', 'vl-key', 'vl-model')])

    async def test_empty_key_falls_back_to_main_key(self):
        async def fake_post(url, key, payload):
            self.calls.append(key)
            return 'ok'

        self.engine._post_vision = fake_post
        await self.engine.describe_images(['data:image/png;base64,' + base64.b64encode(bytes.fromhex('89504e470d0a1a0a0000000d49484452000000010000000108060000001f15c489'
                                                       '0000000d49444154789c63600000020001e221bc330000000049454e44ae426082')).decode()],
                                          api_url='http://x/v1/chat/completions')
        self.assertEqual(self.calls, ['main-key'])

    async def test_http_error_surfaces_status_for_mapping(self):
        transport = httpx.MockTransport(lambda request: httpx.Response(
            400, json={'error': {'message': '这次请求被模型拒收了'}}))
        engine = AIEngine(api_url='https://chat.example/v1/chat/completions',
                          api_key='k', model='m')
        engine._vision_transport = transport

        real_client = httpx.AsyncClient

        def patched(*args, **kwargs):
            kwargs['transport'] = transport
            return real_client(*args, **kwargs)

        httpx.AsyncClient = patched
        try:
            with self.assertRaises(httpx.HTTPStatusError) as ctx:
                await engine._post_vision('https://chat.example/v1/chat/completions', 'k',
                                          {'model': 'm', 'messages': []})
            self.assertEqual(ctx.exception.response.status_code, 400)
            self.assertIn('HTTP 400', engine.last_error)
        finally:
            httpx.AsyncClient = real_client


if __name__ == '__main__':
    unittest.main()
