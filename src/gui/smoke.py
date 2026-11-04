"""离线自检：不连微信、不连模型接口，把整条链路跑一遍。

用法：`python wechat_gui.py --self-test out/smoke.json`

覆盖四件事：
1. 后端身份与路由（`/api/runtime` 必须返回本项目的 APP_ID 与数据目录，否则控制台的每次
   调用都会被身份校验拦掉 —— 这是移植时最容易踩的坑）。
2. 插件集基线（数量、必须存在的、必须**不存在**的）。
3. 端到端：用模拟微信后端喂一条群消息，跑完「触发判定 → 插件 → 模型 → 发送」，
   并断言回复真的落到了发送记录里。
4. 控制台渲染：窗口、外观滑块、插件列表搜索、插件配置对话框（含图片上传）与关键词
   规则编辑器都能正常工作，并截图留证。

模拟后端不会触碰真实微信，`ai.chat` 被替换成固定回复，所以整个自检是离线且确定的。
"""
from __future__ import annotations

import asyncio
import json
import os
import shutil
import sys
import tempfile
import time
from pathlib import Path
from unittest.mock import patch

from PyQt6.QtCore import Qt
from PyQt6.QtWidgets import QLabel


def _write_config(root: Path) -> Path:
    """写一份自检用的最小配置：只求"已配置"，不联网。"""
    config = root / "config.wechat.yaml"
    config.write_text(
        "ai:\n"
        "  api_url: https://api.deepseek.com/v1/chat/completions\n"
        "  api_key: self-test-key\n"
        "  model: deepseek-chat\n"
        "  api_style: auto\n"
        "wechat:\n"
        "  backend: mock\n"
        "  send_interval: 0\n"
        "  max_message_age: 300\n"
        "wechat_only:\n"
        "  backend: mock\n"
        "  groups: []\n"
        "  listen_private: true\n",
        encoding="utf-8")
    return config


def _pump(app, predicate, timeout: float = 10.0) -> bool:
    """等到 predicate 成立，期间不断让 Qt 处理事件。

    为什么需要：界面的网络请求现在跑在后台线程里，结果通过 Qt 信号排队回主线程
    （见 wechat_gui.call_api）。自检如果像以前那样"调一次就断言"，断言会跑在结果
    回来之前。这里把等结果这件事显式化 —— 断言本身一个都没少。
    """
    deadline = time.time() + timeout
    while time.time() < deadline:
        app.processEvents()
        if predicate():
            return True
        time.sleep(0.02)
    app.processEvents()
    return predicate()


def _run_backend_checks(root: Path) -> dict:
    """后端 + 插件 + 端到端（不启动 uvicorn，不起真实 HTTP）。"""
    from src.plugins.manager import PluginManager
    from src.runtime_identity import APP_ID, APP_VERSION, backend_identity_error
    from src.wechat_app import WeChatRuntime, create_app
    from src.wechat_bridge import WeChatBridge
    from src.wechat_transports import message_from_raw
    from src.wechat_sender import IsolatedWeChatSender

    # 冻结入口必须转发到子进程工作模式，不能再次开控制台窗口。
    sender_probe = IsolatedWeChatSender(root / "sender-probe")
    sender_result = sender_probe._run({"action": "self-test"})
    assert sender_result, str(sender_result)
    # 冻结环境也验证严格后台优先分流，不创建会唤醒/激活窗口的UIA驱动。
    from src.wechat_send_worker import execute
    with patch('wechatauto.uia_driver.WeChatUIA', side_effect=AssertionError('unexpected foreground driver')), \
         patch('src.wechat_background.send_background', return_value=(False, 'offline wait')):
        assert execute({'action': 'text', 'background_send': True, 'send_compat': True}) == (False, 'offline wait')

    # 冻结包也要走目录层级与备注解析，不能只在源码测试里通过。
    from types import SimpleNamespace
    from unittest.mock import Mock
    from src.wechat_paths import resolve_database_dir
    storage = root / '中文 数据目录' / 'xwechat_files' / 'test_account' / 'db_storage'
    storage.mkdir(parents=True)
    for selected in (storage, storage.parent, storage.parent.parent, storage.parents[2]):
        assert resolve_database_dir(str(selected)) == str(storage.parent.parent)
    target_probe = WeChatBridge(root / 'target-probe', backend='wechatauto')
    target_probe.backend, target_probe.online = 'wechatauto', True
    target_probe._db = SimpleNamespace(nickname_map=lambda refresh=False: {'test@chatroom': '最新群备注'})
    target_probe._wx = Mock()
    target_probe._wx.SendMsg.return_value = True
    assert target_probe.send_text('test@chatroom', '离线自检', '过期群名')[0]
    assert target_probe._wx.SendMsg.call_args.kwargs['who'] == '最新群备注'
    target_probe._wx.SendMsg.reset_mock()
    assert not target_probe.send_text('unknown@chatroom', '离线自检')[0]
    target_probe._wx.SendMsg.assert_not_called()

    config = _write_config(root)
    runtime = WeChatRuntime(root=root, config_path=config, db_path=root / "messages.sqlite")
    # 真正加载损坏的外置源码，冻结版也必须跳过它并能继续启动。
    fault_id = 'smoke_fault_probe'
    fault_path = runtime.plugins.custom_plugin_path(fault_id)
    fault_path.write_text('raise SystemExit(7)\n', encoding='utf-8')
    runtime.plugins.register_custom(fault_id)
    runtime.plugins.reload()
    assert runtime.plugins.is_quarantined(fault_id)
    assert 'chat_controls' in runtime.plugins._plugins
    assert any(item.get('faulted') and item['id'] == fault_id for item in runtime.plugins.list_plugins())
    fault_path.write_text(
        'from . import PluginBase, PluginMeta\nclass Repaired(PluginBase):\n'
        '    META = PluginMeta(id="smoke_fault_probe", name="修复探针", description="")\n', encoding='utf-8')
    assert not runtime.plugins.retry_failed()
    assert fault_id in runtime.plugins._plugins
    runtime.plugins.unregister_custom(fault_id)
    fault_path.unlink()
    runtime.plugins.reload()
    app = create_app(runtime)
    # 这里不走 lifespan（不起 uvicorn），所以手动把 runtime 挂到 app.state 上，
    # 否则调用路由函数时 app.state.bot 不存在。
    app.state.bot = runtime
    async def control_probe():
        import httpx
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://127.0.0.1') as client:
            denied = await client.post('/api/agent/control/execute', json={
                'action': 'console.stop', 'request_id': 'smoke-unbound-stop'})
            assert denied.status_code == 403
            binding = await client.post('/api/agent/authority', json={
                'platform': 'desktop', 'account': 'smoke_machine', 'subject': 'smoke_admin'})
            assert binding.json()['bound']
            response = await client.post('/api/agent/control/execute', json={'action': 'plugins.list'})
            assert response.json()['success'] and response.json()['data']
            from src.agent_authority import authority
            key = authority(runtime).issue('smoke_dsh', ['read_console'])
            handshake = await client.get('/api/agent/bridge/handshake', headers={'Authorization': 'Bearer '+key['token']})
            assert handshake.json()['protocol'] == 'whale-agent-bridge/1'
            # 源码与冻结包都执行新工作区真实路由；模型为离线替身。
            note = await client.post('/api/agent/workspace/note', json={'name':'自检.md','content':'本地记忆','version':''})
            assert note.json()['content']=='本地记忆'
            from unittest.mock import AsyncMock
            with patch.object(runtime.ai,'chat',new=AsyncMock(return_value='继续上轮提议。')) as model:
                for rid,text in [('smoke-chat-0001','请整理角色配置'),('smoke-chat-0002','也行')]:
                    talked = await client.post('/api/agent/chat',json={'session_id':'smoke','request_id':rid,'text':text})
                    assert talked.json()['success'], talked.json()
                assert '继续上轮提议' in model.call_args.args[0][-2]['content']
                configured = await client.post('/api/agent/workspace/context',json={'history_turns':1,'max_chars':1000,'recall_limit':1})
                assert configured.json()['success']
                recalled = await client.post('/api/agent/chat',json={'session_id':'smoke','request_id':'smoke-chat-0003','text':'角色配置怎么整理'})
                assert recalled.json()['context']['recalled']==1
                assert recalled.json()['context']['chars']<=1000
                assert 'smoke-chat-0001' in model.call_args.args[0][0]['content']
                diary=await client.post('/api/agent/workspace/diary',json={})
                assert diary.json()['status']=='done' and diary.json()['content']=='继续上轮提议。'
                count=model.call_count
                again=await client.post('/api/agent/workspace/diary',json={})
                assert again.json()['replayed'] and model.call_count==count
            from src.dsh_discovery import candidates
            assert candidates([{'pid':1,'parent':0,'name':'DeepSeek Harness.exe'}],
                [{'pid':1,'address':'127.0.0.1','port':5947}])[0]['base_url']=='http://127.0.0.1:5947'
            connector = await client.post('/api/agent/workspace/connector')
            assert Path(connector.json()['script']).is_file()
            from src.wechat_chat_name import clean_chat_name
            assert clean_chat_name('一群鱼 按住 Ctrl + Win 使用','一群鱼')=='一群鱼'
            authority(runtime).unbind()
            assert (await client.get('/api/agent/bridge/handshake', headers={'Authorization': 'Bearer '+key['token']})).status_code == 401
    asyncio.run(control_probe())
    # 冻结包验证DSH收集器与依赖，但不接触本机真实DSH或群消息。
    from src.dsh_bridge import TurnCollector, PRESET, install_preset
    from websockets.asyncio.client import connect as dsh_connect
    collector = TurnCollector()
    collector.push({'type': 'turn/start', 'data': {'turn': 'offline'}})
    collector.push({'type': 'assistant/message', 'data': {'turn': 'offline', 'message': {
        'content': [{'type': 'thinking', 'thinking': '不输出'}, {'type': 'text', 'text': '离线答复'}]}}})
    assert collector.push({'type': 'turn/end', 'data': {'turn': 'offline', 'reason': {'kind': 'completed'}}}) == '离线答复'
    profile_home = root / 'smoke_dsh_home'
    (profile_home / 'profiles' / 'desktop').mkdir(parents=True, exist_ok=True)
    # 按本机DSH真实包名（单数）造依赖目录，才能验到cordis.patch.yml这条分支。
    (profile_home / 'profiles/desktop/node_modules/@deepseek-ai/dsh-agent-preset').mkdir(parents=True, exist_ok=True)
    installed_dsh = install_preset(root, profile_home)
    assert installed_dsh['success'] and PRESET in Path(installed_dsh['path']).read_text('utf-8')
    # 导入只保存，不自动启用；冻结包也必须带上格式兼容与同名副本分配逻辑。
    from src.persona_files import import_details
    old_persona = runtime.style.active_name
    imported = import_details(runtime, 'name: 小蓝\nprompt: 你是小蓝，先回答当前问题。',
                              filename='中文 人设.yaml', auto_name=True)
    duplicate = import_details(runtime, 'name: 小蓝\nprompt: 你是小蓝，先回答当前问题。',
                               filename='中文 人设.yaml', auto_name=True)
    assert imported['name'] != duplicate['name']
    assert runtime.style.active_name == old_persona
    assert runtime.style.get_for_edit(imported['name']).system_prompt == '你是小蓝，先回答当前问题。'
    identity = asyncio.run(next(route.endpoint for route in app.routes
                                if route.path == "/api/runtime")())
    assert identity["app"] == APP_ID, identity
    assert identity["version"] == APP_VERSION, identity
    assert not backend_identity_error(identity, root)
    assert backend_identity_error(identity, root / "other")

    # 插件基线：微信版应该有的、以及**必须没有**的（有封号风险的那批）
    manager = runtime.plugins
    plugins = {item["id"]: item for item in manager.list_plugins()}
    # 覆盖升级必须保留受支持旧功能；源码和正式安装包核对同一完整集合。
    from ..plugin_catalog import BASIC_PLUGINS, MARKET_PLUGINS
    expected_plugin_count = len(BASIC_PLUGINS) + len(MARKET_PLUGINS)
    frozen = bool(getattr(sys, "frozen", False))
    expected = len(BASIC_PLUGINS) + len(MARKET_PLUGINS)
    assert set(BASIC_PLUGINS+MARKET_PLUGINS) <= set(plugins), '覆盖升级不能丢失支持中的旧插件'
    assert len(plugins) == expected, (
        f"插件数量异常：{len(plugins)}，期望 {expected}"
        f"（{'冻结安装包' if frozen else '源码环境'}均应完整）")
    for plugin_id in ("conversation_memory", "chat_controls", "image_library",
                      "image_understanding", "group_summary",
                      "quick_reply", "speech_to_text_wechat", "image_ocr"):
        assert plugin_id in plugins, f"缺少插件：{plugin_id}"
    for plugin_id in ('robot_admin','private_replies','keyword_trigger','group_monitor'):
        assert plugin_id in plugins, f'缺少管理插件：{plugin_id}'
    assert 'reply_preferences' in plugins
    preferences=manager._plugins['reply_preferences']
    assert preferences.fast()
    assert preferences.model_options()=={'thinking':'auto_fast'}
    assert preferences.clean_reply('@测试昵称 主人，在的。',{'sender_name':'测试昵称'})=='主人，在的。'
    for route_path in ('/api/management/sessions','/api/management/owner'):
        assert any(route.path == route_path for route in app.routes), route_path
    for route_path in ('/api/ai/probe','/api/ai/models'):
        assert any(route.path == route_path for route in app.routes), route_path
    for plugin_id in ("poke_counter", "probability_reply", "face_decode", "proactive_chat",
                      "scheduled_announcer", "silence_ping", "icebreaker", "affinity_proactive",
                      "typing_indicator", "speech_to_text_qq"):
        assert plugin_id not in plugins, f"有风险/平台不存在的插件不该出现：{plugin_id}"
    for plugin_id in ('context_chime','repeat_echo','question_relay','at_mention',
                      'send_pacing','typo_sim','emotion_state','mood_guidance','computer_helper',
                      'turn_complete','quote_reply','mention_nudge_guard','chatter_guard'):
        assert plugin_id not in plugins, '已退役插件不应出现在安装版：'+plugin_id
        if getattr(sys,'frozen',False):
            import importlib.util
            assert importlib.util.find_spec('src.plugins.'+plugin_id) is None,'安装包仍携带退役模块：'+plugin_id

    # 消息转换：群聊 @ 机器人
    bridge = WeChatBridge(data_dir=root / "bridge", backend="mock")
    bridge.self_id = "wxid_bot"
    bridge.account_name = "鲸鱼娘"
    raw = {"username": "12345@chatroom", "type": "文本", "sender_username": "wxid_a",
           "sender_id": "wxid_a", "content": "@鲸鱼娘 在吗", "local_id": 7, "shard": "message_0",
           "create_time": time.time()}
    message = message_from_raw(raw, bridge, ["鲸鱼娘"])
    assert message and message["kind"] == "group" and message["at"] is True, message
    assert message["id"] == "message_0:7"
    # 自己发的消息必须被丢掉，否则机器人会对着自己的话回复
    assert message_from_raw(dict(raw, sender_username="wxid_bot"), bridge, ["鲸鱼娘"]) is None
    # 系统消息不进管线
    assert message_from_raw(dict(raw, type="系统消息"), bridge, []) is None
    # 图片消息应产生 media 段与占位符
    image_message = message_from_raw(dict(raw, type="图片", content=""), bridge, [])
    assert image_message["media"][0]["type"] == "image" and "[图片]" in image_message["text"]

    # 端到端：模拟后端 + 假模型
    async def fake_chat(messages, **kwargs):
        return "在的，怎么啦～"

    async def flow():
        from unittest.mock import Mock
        with patch.object(runtime.bridge, 'connect', wraps=runtime.bridge.connect) as connect:
            await asyncio.gather(*(runtime.start() for _ in range(3)))
            assert connect.call_count == 1, '连续启动创建了多个微信连接'
        runtime.ai.chat = fake_chat          # 不联网，直接给固定回复
        # 真 GIF 字节贯通冻结后的共享下载、Pillow 抽帧、视觉结果与图库回填。
        import io
        from PIL import Image
        from unittest.mock import AsyncMock, Mock
        output = io.BytesIO()
        Image.new('RGB', (32, 32), 'red').save(output, 'GIF', save_all=True,
            append_images=[Image.new('RGB', (32, 32), 'blue')], duration=100)
        data = output.getvalue()
        downloaded = root / 'media-check.gif'
        downloaded.write_bytes(data)
        entry, _ = runtime.stickers.put(data, '冻结识图验证')
        import hashlib
        assert runtime.stickers.matching_md5(hashlib.md5(data).hexdigest())==data, '冻结本地表情原文件匹配失败'
        image_probe = dict(image_message, media=[{'type': 'image', 'local_id': 910,
                                                  'shard': 'media-check'}])
        download = Mock(return_value=str(downloaded))
        describe = AsyncMock(return_value='红蓝色方块动图')
        with patch.object(runtime.bridge, 'download_media', download), patch.object(runtime.ai, 'describe_images', describe):
            results = await asyncio.gather(*(runtime.transport.download_media(
                image_probe['target'], 910, 'image', 'media-check') for _ in range(3)))
            assert all(results) and download.call_count == 1, '冻结媒体重复下载'
            await manager.describe_images(image_probe, runtime)
            assert image_probe['vision_status'] == 'ok', image_probe
            assert download.call_count == 1 and describe.await_count == 1
            saved=Path(image_probe['_image_storage'][0]['path'])
            assert saved.parent==root/'data/received_images' and saved.read_bytes()==data
            frames = describe.await_args.args[0]
            assert len(frames) == 2 and all(s.startswith('data:image/png;') for s in frames)
            assert '红蓝色' in runtime.stickers.get(entry['id'])['description']
            encrypted = dict(image_probe, media=[{'type': 'emotion', 'local_id': 911}])
            await manager.describe_images(encrypted, runtime)
            assert '加密' in encrypted['vision_status'] and 'vision_description' not in encrypted
            assert describe.await_count == 1, '加密表情不应编造图片来源'
        from src.received_images import ReceivedImageStore
        received=ReceivedImageStore(root/'data/received_images')
        for index in range(50):
            output=io.BytesIO()
            Image.new('RGB',(8,8),(index,11,12)).save(output,'PNG')
            result,saved_bytes=received.save_and_read(output.getvalue())
            assert saved_bytes==output.getvalue()
        assert result['count']==26 and result['deleted']==25, '冻结图片容量清理未执行'
        assert downloaded.read_bytes()==data and runtime.stickers.matching_md5(hashlib.md5(data).hexdigest())==data
        from src.ai_engine import AIEngine
        tagged, _ = runtime.stickers.put(output.getvalue(), '冻结标签验证')
        with patch.object(AIEngine, 'describe_images', new=AsyncMock(return_value=json.dumps({
                'description': '微笑的小猫', 'features': {'subject': ['猫'], 'emotion': ['开心']}}))):
            runtime.image_chat.enqueue([tagged['id']])
            await asyncio.wait_for(runtime.image_chat.task, 5)
        assert runtime.image_chat.status()['counts'].get('done') == 1
        assert '开心' in runtime.stickers.get(tagged['id'])['auto_tags']
        # 实验通道也只用假窗口离线验证，绝不向真实微信投递消息。
        from src.wechat_background import send_background
        value = SimpleNamespace(Value='', IsReadOnly=False)
        value.SetValue = lambda text: setattr(value, 'Value', text)
        field = SimpleNamespace(ClassName='mmui::ChatInputField', AutomationId='', Name='离线测试',
            ControlTypeName='EditControl', GetChildren=lambda: [], GetValuePattern=lambda: value)
        button = SimpleNamespace(ClassName='mmui::XButton', AutomationId='', Name='发送', IsEnabled=True,
            ControlTypeName='ButtonControl', GetChildren=lambda: [])
        anchor = SimpleNamespace(ClassName='mmui::MainWindow', AutomationId='', Name='微信',
            ControlTypeName='WindowControl', GetChildren=lambda: [field, button])
        access = SimpleNamespace(candidates=lambda: [{'hwnd': 1, 'pid': 2, 'minimized': False}],
            matches_account=lambda pid,path: True, control=lambda hwnd: anchor,
            post_enter=Mock(side_effect=lambda hwnd: setattr(value, 'Value', '') or True))
        assert send_background({'account_path': 'offline', 'who': '离线测试', 'text': '测试',
            'background_submit': 'posted_enter'}, lambda stage: None, access, lambda: (True,''))[0]
        access.post_enter.assert_called_once()
        # 用一句"必须走模型"的话：太短太常见的问候会被「寒暄秒回」插件本地拦截，
        # 那样就验证不到模型链路了（这不是 bug，是插件的预期行为）。
        runtime.bridge.inject_mock_message("12345@chatroom", "@鲸鱼娘 你昨天说的那个电影叫什么",
                                           sender="阿明", at=True)
        sent, row = [], None
        # 等到"真的发出去了"为止：发送记录是在发送动作里写的，而数据库状态要到发送
        # 返回之后才改成 sent，只等 sent_log 会读到 sending（之前就因此偶发失败）。
        for _ in range(120):
            await asyncio.sleep(0.1)
            sent = list(runtime.bridge.sent_log)
            row = runtime.db.execute(
                "SELECT state,reply FROM inbox ORDER BY created DESC LIMIT 1").fetchone()
            if sent and row and row[0] == "sent":
                break
        # 新管理插件也必须进入冻结自检，避免源码可用而安装包缺少动态模块。
        from src.wechat_management import bind_owner, sessions
        runtime.plugins.update_settings('private_replies',{'mode':'off'})
        runtime.ingest(dict(raw,username='wxid_selftest_owner',sender_username='wxid_selftest_owner',
                            sender_id='wxid_selftest_owner',content='自检观察',local_id=90))
        bind_owner(runtime,'wxid_selftest_owner')
        runtime.plugins.update_settings('robot_admin',{'enabled':True})
        assert sessions(runtime)['owners'] == ['wxid_selftest_owner']
        runtime.ingest(dict(raw,username='wxid_selftest_owner',sender_username='wxid_selftest_owner',
                            sender_id='wxid_selftest_owner',content='机器人 状态',local_id=91))
        for _ in range(50):
            if len(runtime.bridge.sent_log) >= len(sent)+1:
                break
            await asyncio.sleep(.1)
        assert len(runtime.bridge.sent_log) == len(sent)+1, '冻结管理员回复链路未完成'
        assert '微信' in runtime.bridge.sent_log[-1]['text']
        # 同一段检索/节流必须进冻结包；只用生成的本机记录，不读取真实微信。
        from src.plugins.token_saver import compact
        old=dict(id='archive-selfcheck',kind='group',target='selfcheck-memory-room',
                 text='云杉会议预算为五千元',sender_id='wxid_selfcheck',sender_name='离线自检',created=time.time()-100)
        runtime.conversations.observe(old,runtime)
        current=dict(old,id='archive-current',text='之前云杉会议预算是多少',created=time.time())
        recalled=runtime.conversations.recall(current,runtime)
        assert recalled and '五千元' in recalled[0]['user'], '冻结本地旧记录检索失败'
        packed,stats=compact([{'role':'system','content':'测试人设'},
            {'role':'user','content':'旧消息'*1000},{'role':'user','content':'当前问题'}],
            max_content_chars=100,max_prompt_chars=300)
        assert packed[0]['content']=='测试人设' and packed[-1]['content']=='当前问题'
        assert stats['saved_chars']>0, '冻结 Token 字符预算未生效'
        runtime.plugins.update_settings('keyword_trigger',{'mode':'replace','includePersona':False,'keywords':'DeepSeek'})
        assert runtime.aliases() == ['DeepSeek']
        runtime.plugins.update_settings('group_monitor',{'mode':'selected','groups':''})
        await runtime.refresh_monitoring()
        assert not runtime.bridge.listen_groups
        assert not runtime.reply_allowed({'kind':'group','target':'12345@chatroom'})
        runtime.style.save_persona({'name':'restart_probe','display_name':'重启验证','system_prompt':'测试人设','keywords':['验证唤醒']})
        runtime.activate_persona('restart_probe')
        runtime.admin_sessions.set_password('OfflineTestPasswordOnly')
        await runtime.stop()
        restarted=WeChatRuntime(root=root,config_path=config,db_path=root/'restart-probe.sqlite')
        try:
            assert restarted.style.active_name=='restart_probe'
            assert restarted.aliases()==['验证唤醒']
            assert restarted.admin_sessions.credential and not restarted.admin_sessions.authenticated
        finally:
            await restarted.close()
        return sent, row

    try:
        sent, row = asyncio.run(flow())
        assert sent, "模拟发送记录为空：入站→回复链路没有跑通"
        assert "在的" in sent[-1]["text"], f"回复不是模型给的固定文本，实际为：{sent}"
        assert row and row[0] == "sent", row
    finally:
        # 一定要关库：否则 Windows 上临时目录删不掉，会用一个"权限错误"把
        # 真正的断言失败盖掉（排查时看不到根因）。
        try:
            runtime.db.close()
        except Exception:
            pass
    from src.wechat_safety import SafetyHold, RESET_PHRASE, service_target
    safety = SafetyHold(root/'data/wechat_safety_hold.json')
    safety.activate('offline-smoke')
    probe = WeChatBridge(data_dir=root/'data/wechat-only')
    with patch.object(probe, 'detect_backend', side_effect=AssertionError('must not access live backend')):
        assert not probe.connect()[0]
    safety.clear(RESET_PHRASE)
    assert service_target({'target':'weixin'}) and not service_target({'target':'wxid_friend','target_name':'微信团队'})
    return {"plugins": len(plugins), "wechat_safety_hold": True, "process_write_blocked": True, "dsh_bridge_offline": True, "image_chat_hub": True, "posted_enter_offline": True, "background_no_foreground_fallback": True, "plugin_fault_isolation": True, "agent_authority_control": True, "management_checks": True, "media_checks": True, "single_start_check": True,
            "archive_retrieval":True,"token_budget":True,"received_image_storage":True,
            "sent": len(sent), "reply": sent[-1]["text"],
            "routes": len(app.routes)}


def run(gui, output):
    os.environ["QT_QPA_PLATFORM"] = "offscreen"
    from PyQt6.QtGui import QColor, QFontDatabase, QPixmap
    from PyQt6.QtWidgets import QApplication

    from src.gui.config import load_gui_config, save_gui_config
    from src.plugins.manager import PluginManager

    real_api_get = gui.api_get
    app = QApplication.instance() or QApplication([])
    windir = Path(os.environ.get("WINDIR", "C:/Windows"))
    for font in ("msyh.ttc", "seguiemj.ttf", "segoeui.ttf"):
        QFontDatabase.addApplicationFont(str(windir / "Fonts" / font))

    output = Path(output).resolve()
    output.parent.mkdir(parents=True, exist_ok=True)
    summary: dict = {"ok": True, "frozen": gui.FROZEN}
    from src.native_dependencies import check_native_dependencies
    summary['native_dependencies'] = check_native_dependencies()

    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        shutil.copytree(gui.BUNDLE / "styles", root / "styles")
        summary["backend"] = _run_backend_checks(root)

        # ── 控制台渲染 ──────────────────────────────────────────────────────
        config = {"transparency": 0.0, "panel_transparency": 0.65, "blur": 0.0}
        pixmap = QPixmap(1200, 760)
        pixmap.fill(QColor("#366fa1"))
        background = root / "background.png"
        pixmap.save(str(background))
        config["background_image"] = "background.png"
        save_gui_config(root / "gui.json", config)

        manager = PluginManager(root / "plugins")
        with patch.object(gui, "ROOT", root), patch.object(gui, "GUI_CFG", root / "gui.json"), \
                patch.object(gui, "api_get", return_value={}), \
                patch.object(sys, "argv", ["wechat_gui", "--no-backend"]):
            window = gui.MainWindow()
            window._timer.stop()
            window.resize(1200, 900)
            window.tabs.setCurrentWidget(window.appearance_tab)
            window.show()
            app.processEvents()
            # 冻结包同样检查托盘恢复入口；隐藏窗口不触发启停或新增机器人。
            with patch.object(window, '_action') as tray_action:
                tray = window._tray
                labels = [action.text() for action in tray.menu.actions() if not action.isSeparator()]
                assert labels == ['显示控制台', '开启机器人', '停止机器人', '退出程序']
                window.hide()
                tray.show_action.trigger()
                assert window.isVisible(), '托盘菜单没有恢复控制台'
                tray_action.assert_not_called()
                tray.menu.ensurePolished()
                tray.menu.adjustSize()
                assert tray.menu.grab().toImage().pixelColor(3, 3).alpha() == 255, '托盘菜单背景不可透明'
                tray.menu.grab().save(str(output.with_suffix('.tray.png')))
                summary['tray_actions'] = labels
                summary['tray_restore_check'] = True
            window.appearance_tab.panel_transparency_slider.setValue(65)
            window.appearance_tab.transparency_slider.setValue(0)
            window.appearance_tab._apply()
            assert window._cfg["panel_transparency"] == 0.65
            assert window._cfg["transparency"] == 0
            window.grab().save(str(output.with_suffix(".png")))

            # 控制台页与人设页也截图留证：用户最常问的两件事就是
            # "插件在哪"和"人设是不是还在"，有截图就不用解释。
            with patch.object(gui, "api_get", return_value={
                    "running": True, "mode": "wechat", "model": "deepseek-chat",
                    "ai_test": "最近一次模型调用成功", "plugins": len(manager.list_plugins()),
                    "connection": {"backend": "mock", "online": True, "account": "模拟微信",
                                   "received": 3, "sent": 2, "send_failed": 0, "admin": True,
                                   "db_dir": "", "degraded": ""},
                    "messages": [{"time": 0, "room": "测试群", "sender": "阿明",
                                  "text": "@鲸鱼娘 在吗", "reply": "在的，怎么啦～",
                                  "state": "sent", "error": ""}],
                    "events": [{"time": 0, "text": "机器人已启动（后端 mock）"}]}):
                window.tabs.setCurrentWidget(window.console_tab)
                window._refresh()
                # 轮询在后台线程里跑，等它把状态送回来再截图
                assert _pump(app, lambda: "在吗" in window.console_tab.msg_view.toPlainText()), \
                    "控制台没有等到底层状态刷新"
            app.processEvents()
            window.grab().save(str(output.with_suffix(".console.png")))
            # 真实长路径不能把“测试微信”等按钮及右侧卡片挤出窄窗口。
            long_status='读消息已连接，但发送未就绪；数据库：C:/Users/'+('长账号目录/'*25)+'xwechat_files；当前客户端没有开放输入控件。'
            window.console_tab.set_notice(long_status,False)
            window.console_tab.refresh({'running':True,'model':'测试模型','connection':{
                'online':True,'backend':'wechatauto','account':'离线自检','send_environment':{'ready':False,'reason':long_status}},
                'messages':[],'events':[]})
            window.resize(1000,900);app.processEvents()
            assert window.console_tab.card_model.geometry().right()<=window.console_tab.width()
            assert window.console_tab.btn_test_wechat.geometry().right()<=window.console_tab.width()
            assert window.console_tab.btn_hook.geometry().right()<=window.console_tab.width()
            assert window.console_tab.notice.wordWrap()
            window.grab().save(str(output.with_suffix('.console-waiting.png')))
            summary['long_status_layout']=True
            window.resize(1200,900)

            # 设置页也截图留证：1.2.0 新增的「读取可用模型」入口就在模型名称下面，
            # 打包后控件缺失/错位要能在这张图上直接看出来。
            settings_payload = {
                "backend": "mock", "db_dir": "", "groups": ["测试群"],
                "listen_private": True, "api_url": "https://api.deepseek.com/v1",
                "api_key_set": True, "model": "deepseek-chat", "api_style": "auto",
            }
            with patch.object(gui, "api_get", return_value=settings_payload):
                window.tabs.setCurrentWidget(window.settings_tab)
                window._load_settings()
                # 等的条件必须是**这次 payload 独有**的字段：model 字段在上面控制台那份
                # 假数据里也有，用它当条件会在结果回来之前就判定成功（踩过）。
                assert _pump(app, lambda: window.settings_tab.api_url.text()
                             == "https://api.deepseek.com/v1"), "设置页没有等到后台结果"
            assert window.settings_tab.model_name.text() == "deepseek-chat"
            assert window.settings_tab.btn_models.isEnabled()
            assert window.settings_tab.btn_diagnose.isEnabled()
            window.settings_tab.send_compat.setChecked(True)
            window.settings_tab.background_send.setChecked(True)
            assert window.settings_tab.background_send.isChecked(), '严格后台选择被兼容模式撤销'
            assert not window.settings_tab.send_compat.isChecked(), '严格后台仍允许兼容唤醒'
            assert window.settings_tab.collect()['send_mode'] == 'background'
            summary['strict_background_selection'] = True
            with patch.object(gui,'api_post',return_value={'success':True,'message':'对话请求：成功 · 100 ms\n回复预览：OK'}):
                window.settings_tab.btn_diagnose.click()
                assert _pump(app,lambda:'回复预览：OK' in window.settings_tab.probe_lbl.text())
            app.processEvents()
            window.settings_tab.findChild(gui.QScrollArea).verticalScrollBar().setValue(10000)
            app.processEvents()
            window.grab().save(str(output.with_suffix(".settings.png")))

            persona_payload = {"active": "tsundere_whale_girl", "locked": None,
                               "personas": [{"name": "tsundere_whale_girl",
                                             "display_name": "DeepSeek 鲸鱼娘",
                                             "description": "默认人设（傲娇鲸鱼娘）", "active": True},
                                            {"name": "default", "display_name": "通用助手",
                                             "description": "平衡友好的通用风格", "active": False}]}
            persona_detail = {"name": "tsundere_whale_girl", "display_name": "DeepSeek 鲸鱼娘",
                              "description": "深海鲸族幼体，聪明嗜睡、傲娇又甜，最爱白米饭",
                              "temperature": 0.9, "max_tokens": 100,
                              "system_prompt": "你是\"鲸鱼娘\"，一只住在深海里的傲娇鲸鱼。\n"
                                               "说话短、口语化，先接住情绪再讲道理。",
                              "examples": [{"user": "你是谁？", "assistant": "哼，我是鲸鱼娘。"}]}

            def fake_persona_api(path, timeout=3):
                # 列表接口与详情接口的返回结构不同：详情拿不到 system_prompt 时
                # 人设页会提示"读不到人设内容"，所以这里必须按路径分别返回，
                # 否则截图看起来像是坏了（其实是自检假数据的锅）。
                if path.startswith("personas/"):
                    return persona_detail
                if path == "personas":
                    return persona_payload
                return {}

            with patch.object(gui, "api_get", side_effect=fake_persona_api):
                window.tabs.setCurrentWidget(window.persona_tab)
                window.persona_tab.refresh()
                assert _pump(app, lambda: window.persona_tab.list.count() >= 2 and bool(window.persona_tab.ed_prompt.toPlainText())), \
                    "人设列表没有等到后台结果"
            assert window.persona_tab.list.count() >= 2
            assert persona_payload['active'] in window.persona_tab.active_persona_hint.text()
            assert 'UTF-16' in window.persona_tab.btn_import.toolTip()
            app.processEvents()
            window.grab().save(str(output.with_suffix(".persona.png")))
            from ..style_manager import StyleManager
            from ..persona_files import import_details
            from types import SimpleNamespace
            import_manager = StyleManager(root/'import-fixture',root/'import-fixture/custom')
            import_manager.load_all()
            import_bot = SimpleNamespace(root=root,style=import_manager)
            import_file = root/'中文导入样例.txt'
            import_file.write_text('你是小蓝，先回答当前问题，再保持温柔语气。',encoding='utf-16')
            def persona_import_request(owner, method, path, body=None, **kwargs):
                if path == 'persona-files/import':
                    assert body['auto_name'] is True and body['filename'].endswith('.txt')
                    result = {'success':True,**import_details(import_bot,body['content'],body['name'],body['filename'],True)}
                elif path == 'personas':
                    result = {'personas':import_manager.list_styles(),'active':import_manager.active_name}
                else:
                    result = import_manager.get_for_edit(path.split('/')[-1]).to_dict()
                kwargs['on_done'](result)
            with patch.object(gui.QFileDialog,'getOpenFileName',return_value=(str(import_file),'')), \
                    patch.object(gui,'call_api',side_effect=persona_import_request):
                window.persona_tab._import_persona()
            assert window.persona_tab.ed_prompt.toPlainText() == '你是小蓝，先回答当前问题，再保持温柔语气。'
            assert '设为当前' in window.persona_tab.notice.text()
            assert import_manager.active_name == 'default'
            app.processEvents()
            window.grab().save(str(output.with_suffix('.persona-import.png')))
            summary['persona_import_compatibility'] = True

            management_payload={'success':True,'account':'模拟微信','owners':['wxid_demo'],
                                'reply_paused':False,'sessions':[]}
            with patch.object(gui,'api_get',return_value=management_payload):
                window.tabs.setCurrentWidget(window.management_tab)
                assert _pump(app,lambda:window.management_tab.owners.count()==1)
                with patch.object(gui,'api_post',return_value={'success':True,'message':'已暂停自动回复'}):
                    window.management_tab.local_pause(True)
                    assert _pump(app,lambda:'已暂停自动回复' in window.management_tab.result.text())
                    assert _pump(app,lambda:not window.management_tab._refreshing)
                    assert '已暂停自动回复' in window.management_tab.result.text()
            app.processEvents()
            window.grab().save(str(output.with_suffix('.admin-page.png')))
            dialogue_payload={'success':True,'persona':'tsundere_whale_girl','notes':[
                {'id':'test','content':'对方累了，先关心状态','enabled':True}],
                'people':[{'id':'wxid_demo','name':'测试用户','messages':3}],
                'subjects':[{'subject':'wxid_demo','scope':'wechat|模拟微信|private:wxid_demo','name':'测试用户'}],
                'facts':[{'id':'fact1','subject':'wxid_demo','scope':'wechat|模拟微信|private:wxid_demo',
                          'content':'喜欢蓝色，解释问题时希望按步骤说明','status':'confirmed','evidence':2}],
                'diagnostics':[{'id':'pending1','state':'pending','state_label':'等待处理','text':'请解释这条方案',
                                'sender':'测试用户','target_name':'测试群','error':'微信离线，等待恢复连接；过期消息不会迟到重发'}],
                'autoRemember':True,'memoryEnabled':True}
            with patch.object(gui,'api_get',return_value=dialogue_payload):
                window.tabs.setCurrentWidget(window.dialogue_tab)
                assert _pump(app,lambda:window.dialogue_tab.notes.count()==1)
            app.processEvents()
            window.grab().save(str(output.with_suffix('.dialogue.png')))
            window.dialogue_tab.tabs.setCurrentIndex(1);app.processEvents()
            window.grab().save(str(output.with_suffix('.dialogue-people.png')))
            window.dialogue_tab.tabs.setCurrentIndex(2);app.processEvents()
            assert window.dialogue_tab.tabs.count() >= 4
            archive=window.dialogue_tab.archive
            archive.loaded({'success':True,'account':'模拟微信','rooms':[{'kind':'group','target':'test@chatroom','name':'测试群','count':1}],
                'records':[{'key':'test','id':'one','sender_name':'测试用户','sender_id':'wxid_demo','content':'会议周五举行',
                            'reply':'周五见','persona':'测试人设','created':time.time()}],'total':1})
            window.dialogue_tab.tabs.setCurrentIndex(3);app.processEvents()
            assert '会议周五举行' in archive.text.toPlainText()
            window.grab().save(str(output.with_suffix('.conversation-archive.png')))
            summary['local_conversation_archive']=True
            learning=window.dialogue_tab.learning
            learning.rooms.addItem('测试群',dict(kind='group',target='test@chatroom'))
            learning.loaded(dict(success=True,account='模拟微信',rooms=[dict(kind='group',target='test@chatroom',count=2)],window_count=2,window_limit=1000,
                items=[dict(id='local-learning-test',subject='wxid_demo',category='fact',content='我喜欢乌龙茶',status='approved',hits=2,evidence=['one','two'])]))
            window.dialogue_tab.tabs.setCurrentIndex(4);app.processEvents()
            assert '乌龙茶' in learning.text.toPlainText() and '2/1000' in learning.notice.text()
            window.grab().save(str(output.with_suffix('.local-learning.png')))
            summary['local_learning']=True
            # 冻结包必须实际包含新窗口；只用固定样本，不连接真实 WeFlow 或微信。
            from .weflow_dialog import WeFlowDialog
            def weflow_sample(owner, method, path, body, **kwargs):
                if path.endswith('config'):
                    result = {'success': True, 'url': 'http://127.0.0.1:5031', 'token_set': False}
                elif path.endswith('sessions'):
                    result = {'success': True, 'sessions': [{'username': 'sample@chatroom', 'displayName': '只读示例群'}]}
                else:
                    result = {'success': True, 'messages': [{'createTime': 1792500000,
                              'senderUsername': 'wxid_sample', 'content': '样例：周五开会，未触发回复'}], 'hasMore': True}
                kwargs['on_done'](result)
            weflow_dialog = WeFlowDialog(window.settings_tab, weflow_sample)
            weflow_dialog.sessions(); weflow_dialog.preview(); weflow_dialog.show()
            app.processEvents()
            assert '周五开会' in weflow_dialog.text.toPlainText()
            assert weflow_dialog.export_button.isEnabled()
            weflow_dialog.grab().save(str(output.with_suffix('.weflow.png')))
            weflow_dialog.url.setText('http://127.0.0.1:5032')
            assert not weflow_dialog.export_button.isEnabled()
            weflow_dialog.close(); weflow_dialog.deleteLater()
            summary['weflow_readonly_tools'] = True
            assert 'group_summary' in manager._plugins
            window.dialogue_tab.tabs.setCurrentIndex(2)
            window.grab().save(str(output.with_suffix('.dialogue-diagnostics.png')))
            from types import SimpleNamespace
            from .dialogue_rules import DialogueRulesDialog
            from ..dialogue_rules import snapshot as rules_snapshot
            rule_payload=rules_snapshot(SimpleNamespace(plugins=manager))
            with patch.object(gui,'api_get',return_value=rule_payload):
                rule_dialog=DialogueRulesDialog(gui.call_api,'preferences',window.dialogue_tab)
                rule_dialog.show()
                assert _pump(app,lambda:('conversation_memory','privateToGroup') in rule_dialog.widgets)
                assert _pump(app,lambda:rule_dialog.widgets[('conversation_memory','privateToGroup')][0].isVisible())
                app.processEvents()
                rule_dialog.grab().save(str(output.with_suffix('.dialogue-rules.png')))
                rule_dialog.close();rule_dialog.deleteLater()
            window.dialogue_tab.tabs.setCurrentIndex(0)

            def plugin_response(url,**kwargs):
                response=gui.requests.Response()
                response.status_code=200
                body=manager.list_plugins() if url.endswith('/api/plugins') else {'success':True,'plugins':[]}
                response._content=json.dumps(body).encode('utf-8')
                return response
            # 从正常的数组 JSON 走真实解析器；直接替换 api_get 会漏掉 HTTP 200 误报。
            from ..plugin_catalog import BASIC_PLUGINS as _B, MARKET_PLUGINS as _M
            expected_plugin_count = len(_B) + len(_M)
            with patch.object(gui,'api_get',side_effect=real_api_get), \
                    patch.object(gui,'backend_error',return_value=''), \
                    patch.object(gui._PROXY_FREE,'get',side_effect=plugin_response):
                window.tabs.setCurrentWidget(window.plugin_tab)
                window.plugin_tab.refresh()
                # 数量按插件分层清单算：冻结版 21、源码环境 32。
                # 写死 25 的话两条路必有一条永远是红的（旧断言的坑）。
                assert _pump(app, lambda: len(window.plugin_tab._rows) >= expected_plugin_count), \
                    "插件列表没有等到后台结果"
            assert len(window.plugin_tab._rows) >= expected_plugin_count, len(window.plugin_tab._rows)
            window.plugin_tab.search.setText("image_ocr")
            visible = sum(not row["frame"].isHidden() for row in window.plugin_tab._rows.values())
            assert visible == 1, visible
            window.plugin_tab.search.clear()
            app.processEvents()
            window.grab().save(str(output.with_suffix(".plugins.png")))
            window.plugin_tab.category_filter.setCurrentIndex(window.plugin_tab.category_filter.findData('management'))
            app.processEvents()
            window.grab().save(str(output.with_suffix('.management.png')))
            window.plugin_tab.category_filter.setCurrentIndex(0)

            by_id = {item["id"]: item for item in manager.list_plugins()}
            # 关于作者页：这一页的头像是随包资源，冻结后最容易变成空白，
            # 所以自检必须切过去、断言头像真的画出来了，并留一张截图。
            window.tabs.setCurrentWidget(window.about_tab)
            app.processEvents()
            about = window.about_tab
            assert not about.avatar.pixmap().isNull(), \
                "关于作者页的头像没加载（检查 assets/author/linxi.jpg 是否打进包）"
            about_texts = "\n".join(label.text() or "" for label in about.findChildren(QLabel))
            for expected in (gui.AUTHOR_NAME, gui.AUTHOR_QQ, "你携秋水揽星河", gui.COPYRIGHT_NOTICE):
                assert expected in about_texts, f"关于作者页缺少：{expected}"
            window.grab().save(str(output.with_suffix(".about.png")))
            window.resize(800,900);app.processEvents()
            window.grab().save(str(output.with_suffix('.about-narrow.png')))
            window.resize(1200,900);app.processEvents()
            wakeup_dialog = gui.PluginConfigDialog(by_id['keyword_trigger'])
            wakeup_dialog.show(); app.processEvents()
            assert wakeup_dialog.values() == by_id['keyword_trigger']['settings']
            assert wakeup_dialog.findChild(gui.QPushButton, 'keyword_examples').isVisible()
            wakeup_dialog.grab().save(str(output.with_suffix('.keyword-wakeup.png')))
            wakeup_dialog.close(); wakeup_dialog.deleteLater()
            image_dialog=gui.PluginConfigDialog(by_id['image_understanding'])
            image_dialog.show();app.processEvents()
            assert image_dialog.image_store_button.isVisible()
            image_dialog.grab().save(str(output.with_suffix('.received-images.png')))
            image_dialog.close();image_dialog.deleteLater()
            upload_dialog = gui.PluginConfigDialog(by_id["image_library"])
            upload_dialog.resize(860, 650)
            with patch.object(gui.QFileDialog, "getOpenFileNames",
                              return_value=([str(background)], "")):
                upload_dialog._file_buttons["library"].click()
            uploaded = upload_dialog.values()["library"]
            assert uploaded.startswith("background | "), uploaded
            assert Path(uploaded.partition("|")[2].strip()).is_file()
            upload_dialog.show()
            app.processEvents()
            upload_dialog.grab().save(str(output.with_suffix(".uploads.png")))

            def save_plugin(path, data, timeout=10):
                manager.update_settings("image_library", data)
                return {"success": True}

            with patch.object(gui, "api_post", side_effect=save_plugin):
                upload_dialog._save()
            assert upload_dialog.saved
            upload_dialog.close()

            # keyword_reply 属市场插件：冻结版不内置，这里按"装了才检查"处理，
            # 否则安装包自检会因为一个可选插件永远失败。
            if "keyword_reply" in by_id:
                keyword_dialog = gui.PluginConfigDialog(by_id["keyword_reply"])
                editor = keyword_dialog._widgets["rules"][0]
                editor.example_button.click()
                editor.table.cellWidget(0, 1).setPlainText("你好呀～")
                keyword_dialog._widgets["enabled"][0].setChecked(True)
                keyword_dialog.show()
                app.processEvents()
                keyword_dialog.grab().save(str(output.with_suffix(".keywords.png")))

                def save_keyword(path, data, timeout=10):
                    manager.update_settings("keyword_reply", data)
                    return {"success": True}

                with patch.object(gui, "api_post", side_effect=save_keyword):
                    keyword_dialog._save()
                assert keyword_dialog.saved
                assert manager._plugins["keyword_reply"].on_ingest({"text": "你好"}, None)
                keyword_dialog.close()
                summary["keyword_reply_ui"] = True
            else:
                summary["keyword_reply_ui"] = "skipped(市场插件未安装)"
            window.tabs.setCurrentWidget(window.agent_tab)
            app.processEvents()
            assert window.agent_tab.mode.currentData()=='chat'
            assert not window.agent_tab.cards_view.isVisible()
            window.grab().save(str(output.with_suffix('.agent-workbench.png')))
            from src.gui.agent_workspace import WorkspaceDialog
            def workspace_api(owner,method,path,body=None,**kwargs):
                result={'success':True,'files':[],'items':[]}
                if path=='agent/workspace':result.update(path=str(root/'workspace'),admin={'platform':'wechat'},owner_commands_enabled=False)
                kwargs['on_done'](result)
            workspace_dialog=WorkspaceDialog(workspace_api,window)
            workspace_dialog.show();app.processEvents()
            assert workspace_dialog.editor.isVisible()
            assert workspace_dialog.grab().toImage().pixelColor(4,40).alpha()==255
            workspace_dialog.grab().save(str(output.with_suffix('.agent-workspace.png')))
            from PyQt6.QtWidgets import QTabWidget
            workspace_dialog.findChild(QTabWidget).setCurrentIndex(1);app.processEvents()
            assert workspace_dialog.history_turns.isVisible() and workspace_dialog.memory_search.isVisible()
            workspace_dialog.grab().save(str(output.with_suffix('.agent-memory.png')))
            workspace_dialog.close()
            from src.gui.agent_attachments import AttachmentsDialog
            def attachment_api(owner,method,path,body=None,**kwargs):
                kwargs['on_done']({'success':True,'items':[dict(id='a'*32,name='项目笔记.txt',kind='document',
                    chars=18,text='项目预算3200元，明天检查进度。',truncated=False)]})
            attachment_dialog=AttachmentsDialog(attachment_api,parent=window)
            attachment_dialog.items.setCurrentRow(0);attachment_dialog.show();app.processEvents()
            assert '3200' in attachment_dialog.text.toPlainText()
            attachment_dialog.grab().save(str(output.with_suffix('.agent-attachments.png')));attachment_dialog.close()
            summary['agent_attachments_preview']=True
            from src.gui.dsh_panel import DshDialog
            def dsh_api(owner,method,path,data=None,**kwargs):
                kwargs['on_done']({'success':True,'enabled':False,'base_url':'http://127.0.0.1:5947','stage':'ready'})
            dsh_dialog=DshDialog(dsh_api,window);dsh_dialog.show();app.processEvents()
            dsh_dialog.discovered({'success':True,'message':'本地自动发现测试','candidates':[{'pid':1234,'base_url':'http://127.0.0.1:5947'}]})
            assert dsh_dialog.url.text()=='http://127.0.0.1:5947' and not dsh_dialog.enabled.isChecked()
            dsh_dialog.grab().save(str(output.with_suffix('.dsh-discovery.png')));dsh_dialog.close()
            # 验证冻结包包含隔离后台管理页；样本不访问真实沙盒、不启动微信。
            from src.gui.sandbox_panel import SandboxDialog
            def sandbox_api(owner, method, endpoint, payload=None, on_done=None, **kwargs):
                assert method == 'GET' and endpoint == 'sandbox/status'
                on_done({'success': True, 'environment': 'local', 'sandboxes': [{'id': 'a'*36}],
                    'pairs': [{'channel_id': 'b'*32, 'sandbox_id': 'a'*36, 'short_id': 'b'*12,
                        'stage': 'agent_ready', 'agent_online': True,
                        'reason': '隔离代理已运行；启用后再由启动按钮连接微信'}]})
            sandbox_dialog = SandboxDialog(sandbox_api, window)
            sandbox_dialog.show();app.processEvents()
            assert sandbox_dialog.activate_button.isEnabled()
            sandbox_dialog.grab().save(str(output.with_suffix('.sandbox.png')))
            sandbox_dialog.close()
            summary['sandbox_management_preview'] = True
            # 同样使用固定样本验证候选协议设置页，不能探测真实接口或加载 DLL。
            from src.gui.hook_panel import HookDialog
            hook_calls = []
            def hook_api(owner, method, endpoint, payload=None, on_done=None, **kwargs):
                hook_calls.append((method, endpoint))
                assert method == 'GET' and endpoint == 'wechat/hook'
                on_done({'success': True, 'environment': 'local', 'candidate_only': True,
                    'settings': {'api_url': 'http://127.0.0.1:30001',
                        'account': 'wxid_offline_fixture',
                        'account_path': 'C:/offline-fixture/wxid_offline_fixture',
                        'target': 'filehelper'},
                    'status': {'ready': False, 'waiting_code': 'unsafe_listener',
                        'reason': '原版模块全网监听被拒绝；候选通道尚未完成正式真机验收'}})
            hook_dialog = HookDialog(hook_api, window)
            hook_dialog.show();app.processEvents()
            assert not hook_dialog.activate_button.isEnabled()
            assert hook_dialog.status.textFormat() == Qt.TextFormat.PlainText
            assert hook_calls == [('GET', 'wechat/hook')]
            hook_dialog.grab().save(str(output.with_suffix('.hook.png')))
            hook_dialog.close()
            summary['hook_candidate_preview'] = True
            summary['agent_workbench_workspace']=True
            window.close()
            saved = load_gui_config(root / "gui.json")
            assert saved["panel_transparency"] == 0.65 and saved["transparency"] == 0
            summary["panel_transparency"] = saved["panel_transparency"]
            summary["window_transparency"] = saved["transparency"]

    from .acceptance import run as run_acceptance
    summary['acceptance']=run_acceptance(gui,app)
    output.write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
