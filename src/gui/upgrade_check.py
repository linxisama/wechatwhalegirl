"""隔离的覆盖升级夹具：比较真实文件和 SQLite 记录，绝不连接微信或模型。"""
import asyncio
import base64
import hashlib
import io
import json
import shutil
import sqlite3
from contextlib import closing
from pathlib import Path

import yaml

from ..runtime_identity import APP_VERSION

MARKER = 'upgrade-fixture.json'
HISTORY_COUNT = 20025
TABLES = ('inbox','chat_ambient','chat_memories','chat_outbox','management_sessions','persona_notes','dialogue_decisions')
AGENT_TABLES = ('agent_dialogue','agent_turns','agent_memories','agent_note_versions','agent_diaries','agent_memory_meta','agent_diary_entries')


def _digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _records(db):
    result={}
    for table in TABLES:
        rows=db.execute(f'SELECT * FROM {table} ORDER BY rowid').fetchall()
        result[table]={'count':len(rows),'sha256':hashlib.sha256(json.dumps(rows,ensure_ascii=False).encode()).hexdigest()}
    for table in AGENT_TABLES:
        if db.execute('SELECT 1 FROM sqlite_master WHERE type=\'table\' AND name=?',(table,)).fetchone():
            rows=db.execute(f'SELECT * FROM {table} ORDER BY rowid').fetchall()
            result[table]={'count':len(rows),'sha256':hashlib.sha256(json.dumps(rows,ensure_ascii=False).encode()).hexdigest()}
    return result


def seed_fixture(root, styles):
    """只允许给空测试目录造数据，调用者必须提供独立的临时根目录。"""
    from PIL import Image
    from ..wechat_app import WeChatRuntime
    from ..wechat_management import bind_owner
    from .config import save_gui_config
    from ..persona_files import save_image

    root=Path(root).resolve()
    if root.exists() and any(root.iterdir()):
        raise ValueError('升级测试目录必须为空，拒绝覆盖已有用户数据')
    root.mkdir(parents=True,exist_ok=True)
    shutil.copytree(styles,root/'styles')
    # 与随包默认文件刻意不同，才能发现启动补资源时误覆盖用户编辑的内置人设。
    with (root/'styles/default.yaml').open('a',encoding='utf-8') as file:
        file.write('\n# 用户保留的内置人设编辑，覆盖升级不可替换此文件\n')
    config=root/'config.wechat.yaml'
    config.write_text(yaml.safe_dump({'ai':{'api_url':'https://offline-upgrade.invalid/v1',
        'model':'user-chosen-model','api_style':'openai'},'wechat_only':{'backend':'mock',
        'account':'wxid_upgrade_bot','groups':['upgrade_group@chatroom'],'listen_private':True},
        'wechat':{'backend':'mock','trigger_keywords':['保留唤醒词']}} ,allow_unicode=True),'utf-8')
    bot=WeChatRuntime(root=root,config_path=config)
    try:
        bot.config.set('ai.api_key_encrypted',bot.config.encrypt_value('OfflineUpgradeFixtureKey'))
        bot.style.save_persona({'name':'upgrade_keeper','display_name':'用户自定义人设',
            'system_prompt':'保留用户的人设内容','keywords':['用户唤醒词']})
        bot.activate_persona('upgrade_keeper')
        bot.plugins.update_settings('private_replies',{'mode':'allow','contacts':'wxid_saved_friend'})
        bot.plugins.update_settings('keyword_reply',{'enabled':True,'rules':'用户暗号=保留固定回复'})
        bot.config.set('agent.context',dict(history_turns=7,max_chars=4000,recall_limit=2))
        bot.plugins.update_settings('robot_admin',{'enabled':True,'paused':True})
        # 账号安全暂停也属于用户数据，覆盖升级不能顺手解除它。
        bot.safety.activate('upgrade-fixture')
        from ..agent_authority import authority
        from ..agent_workspace import workspace
        authority(bot).bind({'platform':'wechat','account':'wxid_upgrade_bot','subject':'wxid_saved_admin'})
        area=workspace(bot)
        area.write('保留的主人笔记.md','升级前的笔记原文','')
        area.remember('保留主人工作区记忆',source='upgrade-fixture')
        area.learn('我喜欢保留学习证据','upgrade_keeper','upgrade-learning')
        workspace_key,_=area.identity()
        bot.db.executemany('INSERT INTO agent_dialogue VALUES(?,?,?,?,?,?,?)', [
            (workspace_key,'upgrade-session','upgrade-turn','upgrade_keeper','user','保留Agent问题',1),
            (workspace_key,'upgrade-session','upgrade-turn','upgrade_keeper','assistant','保留Agent回答',2)])
        bot.db.execute('INSERT INTO agent_turns VALUES(?,?,?,?,?,?)',
                       (workspace_key,'upgrade-turn','fixture-digest','done','{"success":true}',2))
        bot.db.commit()
        area.diary()
        bot.db.execute('INSERT INTO agent_diary_entries VALUES(?,?,?,?,?,?,?,?)',
            (workspace_key,'2026-01-01','upgrade_keeper','done','今天我记下了升级前的故事。','[]',1,''));bot.db.commit()
        bot.plugins.set_removed('quick_reply')
        bot.bridge.self_id='wxid_upgrade_bot'
        bot.bridge.account_name='升级验证账号'
        bind_owner(bot,'wxid_saved_admin',manual=True)
        bot.admin_sessions.set_password('UpgradePassword123')
        plugin=bot.plugins.custom_plugin_path('upgrade_fixture')
        plugin.write_text('from src.plugins import PluginBase, PluginMeta\n'
            'class UpgradeFixture(PluginBase):\n'
            '    META=PluginMeta(id="upgrade_fixture",name="用户自定义插件",description="离线升级夹具")\n','utf-8')
        bot.plugins.register_custom('upgrade_fixture')
        scope='wechat|升级验证账号|private:wxid_saved_friend'
        def record(index):
            message={'id':str(index),'kind':'private','target':'wxid_saved_friend','sender_id':'wxid_saved_friend',
                     'sender':'测试联系人','text':f'保留用户对话{index}','_management_account':'wxid_upgrade_bot'}
            return (f'upgrade:{index}','wechat','升级验证账号',scope,json.dumps(message,ensure_ascii=False),
                    'sent',f'保留机器人回复{index}','',float(index+1))
        bot.db.executemany('INSERT INTO inbox VALUES(?,?,?,?,?,?,?,?,?)',(record(i) for i in range(HISTORY_COUNT)))
        bot.db.execute('INSERT INTO chat_ambient VALUES(?,?,?,?,?,?)',(scope,'old-ambient','wxid_saved_friend','测试联系人','旧上下文',1))
        bot.db.execute('INSERT INTO chat_memories VALUES(?,?,?,?,?,?,?,?,?)',('old-memory',scope,'wxid_saved_friend','喜欢蓝色','confirmed','test',2,1,1))
        bot.db.execute('INSERT INTO chat_outbox VALUES(?,?,?,?,?,?,?,?,?)',('old-outbox','wechat','升级验证账号','private','wxid_saved_friend','旧版发送记录','sent','',1))
        bot.db.execute('INSERT INTO persona_notes VALUES(?,?,?,?)',('user-note','upgrade_keeper','保留人设手动记忆',1))
        bot.db.execute('INSERT INTO management_sessions VALUES(?,?,?,?,?,?,?,?)',('wechat','wxid_upgrade_bot','private','wxid_saved_friend','测试联系人',1,12,1))
        bot.db.commit()
        from ..dialogue_management import decision
        decision(bot,{'id':'preserved-decision','kind':'private','target':'wxid_saved_friend','sender_id':'wxid_saved_friend',
                      'sender':'测试联系人','text':'保留的诊断记录'},'observed','用户设置为仅观察')
        records=_records(bot.db)
        image=io.BytesIO()
        Image.new('RGB',(2,2),'#4769A1').save(image,format='PNG')
        background=root/'data/backgrounds/saved.png'
        background.parent.mkdir(parents=True,exist_ok=True)
        background.write_bytes(image.getvalue())
        save_gui_config(root/'data/gui_config.json',{'background_image':'data/backgrounds/saved.png',
            'bg_color':'#DFEAF1','transparency':0.12,'panel_transparency':0.58})
        image_id=save_image(bot,base64.b64encode(image.getvalue()).decode('ascii'))
        persona=bot.style.get_for_edit('upgrade_keeper').to_dict()
        persona.update(image=image_id,image_note='用户保留的形象说明')
        bot.style.save_persona(persona)
    finally:
        asyncio.run(bot.close())
    files={str(path.relative_to(root).as_posix()):_digest(path) for path in root.rglob('*')
           if path.is_file() and 'logs' not in path.relative_to(root).parts and '__pycache__' not in path.parts}
    manifest={'format':'WhaleGirlUpgradeFixture/v1','files':files,'records':records}
    (root/MARKER).write_text(json.dumps(manifest,ensure_ascii=False,indent=2),'utf-8')
    return {'ok':True,'files':len(files),'history_count':HISTORY_COUNT}


def verify_fixture(root, reload_runtime=False):
    root=Path(root).resolve()
    manifest=json.loads((root/MARKER).read_text('utf-8'))
    if manifest.get('format')!='WhaleGirlUpgradeFixture/v1':
        raise ValueError('缺少有效升级测试标记，拒绝读取普通用户目录')
    def check():
        for relative,digest in manifest['files'].items():
            path=(root/relative).resolve()
            if not path.is_relative_to(root) or not path.is_file() or _digest(path)!=digest:
                raise AssertionError('升级改变或删除了用户文件：'+relative)
        with closing(sqlite3.connect((root/'data/wechat-only/messages.sqlite3').as_uri()+'?mode=ro',uri=True)) as db:
            if _records(db)!=manifest['records']:
                raise AssertionError('升级改变或删除了历史记录/记忆')
            if db.execute('PRAGMA integrity_check').fetchone()[0]!='ok':
                raise AssertionError('对话数据库完整性检查失败')
    check()
    if reload_runtime:
        from ..wechat_app import WeChatRuntime
        from .config import ensure_migrated
        bot=WeChatRuntime(root=root)
        try:
            assert bot.style.active_name=='upgrade_keeper','当前人设丢失'
            assert bot.aliases()==['用户唤醒词'],'人设关键词丢失'
            assert bot.ai.model=='user-chosen-model','模型设置丢失'
            assert bot.secret('ai.api_key')=='OfflineUpgradeFixtureKey','API 密钥丢失'
            assert bot.config.get('wechat_only.groups')==['upgrade_group@chatroom'],'群名单丢失'
            assert bot.plugins.get_settings('private_replies')['contacts']=='wxid_saved_friend','私聊设置丢失'
            assert bot.plugins.get_settings('keyword_reply')['rules']=='用户暗号=保留固定回复','固定回复丢失'
            from ..plugin_catalog import BASIC_PLUGINS, MARKET_PLUGINS
            assert set(BASIC_PLUGINS+MARKET_PLUGINS) <= set(bot.plugins._plugins), '升级安装缺少受支持的旧插件模块'
            assert bot.plugins._plugins['keyword_reply'].on_ingest({'kind':'private','text':'用户暗号'},bot)=='保留固定回复', '旧版关键词规则虽保存但不能执行'
            assert bot.plugins.get_settings('robot_admin')['paused'] is True,'暂停设置丢失'
            if (root/'data/wechat_safety_hold.json').exists():
                assert bot.safety.status()['active'] is True,'账号安全暂停在升级中被解除'
            assert 'quick_reply' in bot.plugins._removed and not bot.plugins._plugins['quick_reply'].enabled,'插件移除选择丢失'
            assert bot.plugins._plugins['robot_admin'].owners('wxid_upgrade_bot')==['wxid_saved_admin'],'管理员绑定丢失'
            assert bot.admin_sessions.credential and not bot.admin_sessions.authenticated,'密码摘要丢失或会话错误恢复'
            assert 'upgrade_fixture' in bot.plugins._plugins,'自定义插件丢失'
            assert ensure_migrated(root/'data/gui_config.json')['panel_transparency']==0.58,'外观设置丢失'
            assert not bot.running and not bot.bridge.online,'升级检查不允许连接微信'
        finally:
            asyncio.run(bot.close())
        check()
    return {'ok':True,'version':APP_VERSION,'files_preserved':len(manifest['files']),
            'history_count':manifest['records']['inbox']['count'],'history_preserved':True,
            'config_preserved':True,'runtime_reloaded':reload_runtime,
            'legacy_plugin_behavior_verified':bool(reload_runtime)}


def run(root,output):
    output=Path(output)
    output.parent.mkdir(parents=True,exist_ok=True)
    output.write_text(json.dumps(verify_fixture(root,True),ensure_ascii=False,indent=2),'utf-8')
