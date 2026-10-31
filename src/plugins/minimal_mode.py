"""极简模式（省 token）：按档位裁历史 + 封顶输出 + 追加"说短话"纪律。

移植自 QQ 版 `minimal_mode.py`（1.6.31），差异只有平台适配：

1. QQ 用 `volatile_hint` 把"每轮都会变的说明"排到提示词末尾；微信的
   `run_on_pre_ai` 只有 `extra_system`（由宿主追加在人设之后），效果等价；
2. QQ 通过 `_robot_admin` 标记管理员指令；微信这边管理员口令走
   `on_ingest()` 返回字符串（被动回复，不经模型），权限判定用 `robot_admin.is_admin`；
3. 输出封顶靠插件契约新增的 `max_tokens` 提示（0=不覆盖，多插件取最小）。
"""
from __future__ import annotations

import re
import time

from . import PluginBase, PluginMeta, FieldSchema

TIERS = ('off', 'minimal', 'ultra', 'kamikaze')
TIER_NAMES = {'off': '关闭', 'minimal': '极简', 'ultra': '极限', 'kamikaze': '敢死'}

# 各档位的"说短话"纪律。刻意写得短：它每轮都要发出去，本身就是 token。
NOTES = {
    'minimal': '本轮按极简档回复：直接说结论，不铺垫、不复述对方原话、不列点。',
    'ultra': '本轮按极限档回复：一句话说完，能不说的都不说，最多两个短句。',
    'kamikaze': '本轮按敢死档回复：只回最短的一句（10~20 字），不解释、不追问。',
}

# 口令 → 动作。只在**私聊**、且发送者是已绑定管理员时生效。
COMMANDS = {
    '极简开': 'on', '极简关': 'off', '极限': 'ultra', '敢死': 'kamikaze',
    '极简': 'status', '省 token': 'status', '省token': 'status',
}


def match_command(text):
    value = re.sub(r'^[/／]\s*', '', str(text or '').strip()).lower().replace(' ', '')
    for key, action in COMMANDS.items():
        if value == key.replace(' ', '').lower():
            return action
    return None


def history_limit(messages, limit: int):
    """只留最近 ``limit`` 条，且**永远保留最后一条**（它就是本轮要回应的内容）。"""
    if not isinstance(messages, list) or limit <= 0 or len(messages) <= limit:
        return messages, 0
    head, tail = messages[:-1], messages[-1:]
    kept = head[-(max(1, limit) - 1):] if limit > 1 else []
    return kept + tail, len(head) - len(kept)


class MinimalMode(PluginBase):
    META = PluginMeta(
        id='minimal_mode',
        name='极简模式（省 token）',
        description='按档位（极简/极限/敢死）裁掉历史、封顶输出并追加"说短话"纪律；管理员私聊发「极简开 / 极简关 / 极限 / 敢死 / 极简」切换，口令不经过模型',
        category='utility',
        enabled_by_default=False,
        config_schema={
            'enabled': FieldSchema(type='bool', label='启用极简模式'),
            'tier': FieldSchema(type='select', label='默认档位',
                                values=['off', 'minimal', 'ultra', 'kamikaze']),
            'ownerIds': FieldSchema(type='string', label='允许开关的管理员（留空=沿用机器人管理里绑定的管理员）'),
            'minimalMsgs': FieldSchema(type='int', label='极简档保留历史条数', min=2, max=50),
            'ultraMsgs': FieldSchema(type='int', label='极限/敢死档保留历史条数', min=2, max=30),
            'minimalMaxTokens': FieldSchema(type='int', label='极简档输出上限（0=不限）', min=0, max=8000),
            'ultraMaxTokens': FieldSchema(type='int', label='极限档输出上限', min=16, max=4000),
            'kamikazeMaxTokens': FieldSchema(type='int', label='敢死档输出上限', min=8, max=1000),
            'verbose': FieldSchema(type='bool', label='在运行记录里记下每次省下的内容'),
        },
        default_settings={'enabled': False, 'tier': 'off', 'ownerIds': '',
                          'minimalMsgs': 12, 'ultraMsgs': 6,
                          'minimalMaxTokens': 0, 'ultraMaxTokens': 300, 'kamikazeMaxTokens': 80,
                          'verbose': True},
    )

    # ── 档位 ────────────────────────────────────────────────────────────────
    def tier(self) -> str:
        state = self.load_state('minimal_mode.json', {})
        tier = str(state.get('tier') or self.get('tier', 'off') or 'off')
        return tier if tier in TIERS else 'off'

    def set_tier(self, tier: str, by: str = '') -> str:
        tier = tier if tier in TIERS else 'off'
        state = self.load_state('minimal_mode.json', {})
        if state.get('tier') != tier:
            state['tier'] = tier
            state['since'] = time.time()
            state['by'] = str(by or '')
            self.save_state('minimal_mode.json', state)
        return tier

    def stats(self) -> dict:
        return dict(self.load_state('minimal_mode.json', {}).get('stats') or {})

    def bump(self, **deltas):
        state = self.load_state('minimal_mode.json', {})
        stats = state.get('stats') or {}
        for key, value in deltas.items():
            stats[key] = int(stats.get(key, 0)) + int(value)
        state['stats'] = stats
        self.save_state('minimal_mode.json', state)

    def limits(self) -> dict:
        tier = self.tier()
        if tier == 'off':
            return {'tier': tier, 'messages': 0, 'max_tokens': 0, 'note': ''}
        if tier == 'minimal':
            return {'tier': tier, 'messages': int(self.get('minimalMsgs', 12) or 12),
                    'max_tokens': int(self.get('minimalMaxTokens', 0) or 0),
                    'note': NOTES['minimal']}
        if tier == 'ultra':
            return {'tier': tier, 'messages': int(self.get('ultraMsgs', 6) or 6),
                    'max_tokens': int(self.get('ultraMaxTokens', 300) or 300),
                    'note': NOTES['ultra']}
        return {'tier': tier, 'messages': int(self.get('ultraMsgs', 6) or 6),
                'max_tokens': int(self.get('kamikazeMaxTokens', 80) or 80),
                'note': NOTES['kamikaze']}

    # ── 生成前：裁历史 + 封顶 + 纪律 ────────────────────────────────────────
    def on_pre_ai(self, message, conversation, runtime):
        if not self.get('enabled'):
            return None
        plan = self.limits()
        if plan['tier'] == 'off':
            return None
        hints = {'extra_system': plan['note']}
        if isinstance(conversation, list):
            trimmed, dropped = history_limit(conversation, plan['messages'])
            if dropped:
                conversation[:] = trimmed
                self.bump(messages=dropped)
                if self.get('verbose', True):
                    runtime.note(f"[极简模式] {TIER_NAMES[plan['tier']]}档：本轮只带最近 "
                                 f"{plan['messages']} 条记录（裁掉 {dropped} 条）")
        if plan['max_tokens']:
            hints['max_tokens'] = plan['max_tokens']
            if self.get('verbose', True):
                runtime.note(f"[极简模式] {TIER_NAMES[plan['tier']]}档：本轮输出上限 "
                             f"{plan['max_tokens']} token")
        return hints

    # ── 口令（只认已绑定管理员，私聊）────────────────────────────────────────
    def is_owner(self, message, runtime) -> bool:
        if message.get('kind') != 'private':
            return False
        sender = str(message.get('sender_id') or '')
        configured = [v.strip() for v in re.split(r'[,，\s]+', str(self.get('ownerIds') or '')) if v.strip()]
        if configured:
            return sender in configured
        admin = runtime.plugins._plugins.get('robot_admin')
        return bool(admin and admin.is_admin(message, runtime))

    def status_text(self, runtime) -> str:
        tier = self.tier()
        stats = self.stats()
        lines = [f"极简模式：{'开启' if tier != 'off' else '关闭'}（档位：{TIER_NAMES[tier]}）"]
        plan = self.limits()
        if tier != 'off':
            lines.append(f"本轮保留历史 {plan['messages']} 条，输出上限 "
                         f"{plan['max_tokens'] or '不限'} token")
        lines.append(f"累计裁掉 {int(stats.get('messages', 0))} 条历史")
        lines.append("切换：极简开 / 极简关 / 极限 / 敢死")
        return '；'.join(lines)

    def on_ingest(self, message, runtime):
        if not self.get('enabled'):
            return None
        action = match_command(message.get('text', ''))
        if not action:
            return None
        if message.get('kind') != 'private':
            # 群里有人打这几个字不该让机器人接话；口令只在私聊生效
            return None
        if not self.is_owner(message, runtime):
            return '极简模式只有已绑定的管理员能开关。'
        if action == 'status':
            return self.status_text(runtime)
        current = self.tier()
        mapping = {'on': 'minimal', 'off': 'off', 'ultra': 'ultra', 'kamikaze': 'kamikaze'}
        tier = mapping[action]
        self.set_tier(tier, by=str(message.get('sender_id') or ''))
        return f"极简模式已切换为：{TIER_NAMES[tier]}。{('（本轮起生效）' if tier != 'off' else '（恢复原样）')}"
