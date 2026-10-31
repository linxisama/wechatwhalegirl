"""账号安全暂停与官方通知拦截；不尝试绕过微信风控。"""
import html
import json
import re
import time
from pathlib import Path
from .persistence import atomic_text

SERVICES = frozenset({'weixin','newsapp','fmessage','tmessage','qmessage','qqmail',
                      'medianote','floatbottle','notification_messages','officialaccounts'})
RESET_PHRASE = '我已完成微信官方安全验证'


def service_target(message):
    target = str(message.get('username') or message.get('target') or '').strip().casefold()
    return target if target in SERVICES or target.startswith('gh_') else ''


def security_notice(raw):
    if service_target(raw) != 'weixin' or raw.get('attr') == 'self' or raw.get('is_send') in (True,1,'1'):
        return False
    value = raw.get('content') or raw.get('text') or ''
    if isinstance(value, bytes):
        value = value.decode('utf-8',errors='replace')
    text = html.unescape(str(value))[:30000].replace('<![CDATA[','').replace(']]>','')
    text = re.sub(r'<[^>]*>|\s+', '', text)
    return any(s in text for s in ('当前无法使用所有社交场景', '使用行为或设备网络环境异常',
        '为了你的账号安全，请重新登录', '为保护帐号安全', '为保护账号安全'))


class SafetyHold:
    def __init__(self, path):
        self.path = Path(path)

    def status(self):
        if not self.path.exists():
            return {'active':False}
        try:
            value = json.loads(self.path.read_text('utf-8-sig'))
            if not isinstance(value, dict) or type(value.get('active')) is not bool:
                raise ValueError()
            return {k:value[k] for k in ('active','reason','source','created') if k in value}
        except (OSError,ValueError):
            return {'active':True,'reason':'安全暂停记录异常，请在本机检查；未恢复微信自动化'}

    def activate(self, source='wechat-notice'):
        value = {'active':True,'reason':'账号安全提醒：已暂停微信收发和自动重连，请先在微信完成官方安全验证',
                 'source':source,'created':time.time()}
        atomic_text(self.path,json.dumps(value,ensure_ascii=False))
        return value

    def clear(self, confirmation):
        if confirmation != RESET_PHRASE:
            raise ValueError('请先完成微信官方安全验证，再输入完整确认文字')
        atomic_text(self.path,json.dumps({'active':False,'created':time.time()},ensure_ascii=False))
        return {'active':False}
