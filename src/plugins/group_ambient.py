"""群聊主动行为：隔一段时间自己起一句 / 随机复读群友说过的一句。

⚠️ 这是**主动外发**，属于封号高风险功能（`AGENTS.md` 第二节明令"不要顺手加回来"）。
用户 2026-10-20 明确要求加回来（"都加上吧"），因此本插件：

* **默认关闭**（`enabled=false`），不打开就完全不会发；
* 所有发送都走运行时已有的安全通道 `runtime.queue_outgoing()` —— 它内部强制：
  ① `wechat.proactive.enabled: true` 才放行；② 目标必须在「回复群列表」白名单里；
  ③ 180 秒发送窗口 + 按 key 去重；④ 机器人必须是运行状态；
* 再加一层自我节流：每群各自的最小间隔、全局每小时上限、免打扰时段
  （默认 23:00–08:00 不说话），并且只在"最近还有人在聊"的群里开口。

复读只复读**短句**（默认 ≤60 字）、跳过命令和空消息、跳过机器人自己的话。
"""
from __future__ import annotations

import random
import re
import time

from . import PluginBase, PluginMeta, FieldSchema


def intimacy_score(replies: int, cap: int = 20) -> int:
    """亲密度 0~100：最近 N 天机器人在这群里回过多少条，`cap` 条封顶。

    为什么要它：主动开口只该发生在"确实聊得热"的群里。刚进群就自说自话、
    或者对只在群里问过一句话的人嘘寒问暖，既不像人，也更容易被当成营销号。
    """
    cap = max(1, int(cap or 20))
    return max(0, min(100, round(max(0, int(replies or 0)) * 100 / cap)))


def parse_quiet(raw):
    """``"23-8"`` → ``(23, 8)``；解析不了返回 None（=不免打扰）。"""
    match = re.fullmatch(r'\s*(\d{1,2})\s*[-~到]\s*(\d{1,2})\s*', str(raw or ''))
    if not match:
        return None
    start, end = int(match.group(1)) % 24, int(match.group(2)) % 24
    return start, end


def in_quiet_hours(hour, window) -> bool:
    if not window:
        return False
    start, end = window
    if start == end:
        return False
    return start <= hour < end if start < end else (hour >= start or hour < end)


class GroupAmbient(PluginBase):
    META = PluginMeta(
        id='group_ambient',
        name='群聊主动发言（高风险）',
        description='⚠️ 主动外发：隔一段时间在群里自己起一句、或随机复读群友说过的一句。默认关闭；只在回复群列表内、并按自定义间隔与每小时上限节流，23~8 点默认不说话',
        category='social',
        enabled_by_default=False,
        config_schema={
            'enabled': FieldSchema(type='bool', label='启用（主动发消息，有封号风险）'),
            'intervalMin': FieldSchema(type='int', label='每隔几分钟主动说一句', min=5, max=180),
            'echoEnabled': FieldSchema(type='bool', label='随机复读群友说过的一句'),
            'echoIntervalMin': FieldSchema(type='int', label='复读间隔（分钟）', min=5, max=180),
            'maxPerHour': FieldSchema(type='int', label='每小时最多主动发几条', min=1, max=20),
            'activeWithinMin': FieldSchema(type='int', label='只在最近 N 分钟有人说话的群里开口（0=不限）',
                                           min=0, max=720),
            'quietHours': FieldSchema(type='string', label='免打扰时段（如 23-8；留空=不设）'),
            'echoMaxChars': FieldSchema(type='int', label='复读只复读多长的短句', min=10, max=200),
            'intimacyEnabled': FieldSchema(type='bool', label='只对「聊得热」的群主动开口（按亲密度）'),
            'intimacyDays': FieldSchema(type='int', label='亲密度统计最近几天', min=1, max=90),
            'intimacyMin': FieldSchema(type='int', label='亲密度达到多少才开口（0~100）', min=0, max=100),
        },
        default_settings={'intimacyEnabled': False, 'intimacyDays': 7, 'intimacyMin': 30,
                          'enabled': False, 'intervalMin': 30, 'echoEnabled': False,
                          'echoIntervalMin': 10, 'maxPerHour': 3, 'activeWithinMin': 20,
                          'quietHours': '23-8', 'echoMaxChars': 60},
    )

    # ── 状态 ────────────────────────────────────────────────────────────────
    def _state(self) -> dict:
        state = self.load_state('group_ambient.json', {})
        state.setdefault('lastLine', {})
        state.setdefault('lastEcho', {})
        state.setdefault('sent', [])
        return state

    def _save(self, state) -> None:
        now = time.time()
        state['sent'] = [ts for ts in state.get('sent', []) if now - float(ts) < 7200][-200:]
        try:
            self.save_state('group_ambient.json', state)
        except Exception:
            pass

    def _sent_last_hour(self, state) -> int:
        now = time.time()
        return sum(1 for ts in state.get('sent', []) if now - float(ts) < 3600)

    # ── 取群 ────────────────────────────────────────────────────────────────
    @staticmethod
    def _groups(runtime) -> list:
        """要开口的群：优先用「回复群列表」；列表为空（= 全部群）时从最近群聊里发现。

        为什么要自动发现：绝大多数用户不填群列表（表示"全部群都回"），
        如果这里只读配置，插件就永远找不到群、等于没装。
        """
        try:
            configured = [str(g) for g in (runtime._configured_groups() or []) if str(g).strip()]
        except Exception:
            configured = []
        if configured:
            return configured
        try:
            rows = runtime.db.execute(
                "SELECT DISTINCT scope FROM chat_ambient WHERE scope LIKE '%group:%' "
                "AND created>? ORDER BY created DESC LIMIT 20",
                (time.time() - 24 * 3600,)).fetchall()
        except Exception:
            return []
        groups = []
        for row in rows:
            scope = str(row[0] or '')
            if 'group:' in scope:
                target = scope.split('group:', 1)[1].strip()
                if target and target not in groups:
                    groups.append(target)
        return groups

    @staticmethod
    def _scope(runtime, group: str) -> str:
        try:
            from ..chat_state import scope_of
            return str(scope_of({'kind': 'group', 'target': group}, runtime))
        except Exception:
            return f'group:{group}'

    def _active_recently(self, runtime, group: str, minutes: int) -> bool:
        if minutes <= 0:
            return True
        since = time.time() - minutes * 60
        row = runtime.db.execute(
            "SELECT COUNT(*) FROM chat_ambient WHERE scope=? AND created>?",
            (self._scope(runtime, group), since)).fetchone()
        return bool(row and row[0])

    def intimacy(self, runtime, group: str) -> int:
        """这个群最近的亲密度（0~100）；查不到数据时按 0 处理，也就是不主动开口。"""
        days = max(1, int(self.get('intimacyDays', 7) or 7))
        since = time.time() - days * 86400
        try:
            row = runtime.db.execute(
                "SELECT COUNT(*) FROM conversation_log WHERE mode=? AND account=? AND kind='group' "
                "AND target=? AND created>? AND COALESCE(reply,'')<>''",
                (runtime.mode, runtime.account(), group, since)).fetchone()
        except Exception:
            return 0
        return intimacy_score(row[0] if row else 0)

    def _random_quote(self, runtime, group: str) -> str:
        """随机取一条群友近期说过的话（短句、非命令、不是机器人自己说的）。"""
        since = time.time() - 3600
        limit = max(10, int(self.get('echoMaxChars', 60) or 60))
        rows = runtime.db.execute(
            "SELECT subject,text FROM chat_ambient WHERE scope=? AND created>? AND text<>'' "
            "ORDER BY created DESC LIMIT 200",
            (self._scope(runtime, group), since)).fetchall()
        self_id = str(getattr(getattr(runtime, 'bridge', None), 'self_id', '') or '')
        pool = []
        for subject, text in rows:
            text = str(text or '').strip()
            if not text or len(text) > limit or text.startswith('/'):
                continue
            if self_id and str(subject or '') == self_id:
                continue
            pool.append(text)
        return random.choice(pool) if pool else ''

    # ── 定时钩子 ────────────────────────────────────────────────────────────
    async def tick(self, runtime) -> None:
        if not self.get('enabled'):
            return
        now = time.time()
        if in_quiet_hours(time.localtime(now).tm_hour, parse_quiet(self.get('quietHours'))):
            return
        state = self._state()
        if self._sent_last_hour(state) >= int(self.get('maxPerHour', 3) or 3):
            return
        groups = self._groups(runtime)
        if not groups:
            return
        line_gap = max(5, int(self.get('intervalMin', 30) or 30)) * 60
        echo_gap = max(5, int(self.get('echoIntervalMin', 10) or 10)) * 60
        active_within = max(0, int(self.get('activeWithinMin', 20) or 0))
        intimacy_min = max(0, int(self.get('intimacyMin', 30) or 0))
        for group in groups:
            if not self._active_recently(runtime, group, active_within):
                continue
            if self.get('intimacyEnabled') and self.intimacy(runtime, group) < intimacy_min:
                continue
            last_echo = float(state['lastEcho'].get(group, 0))
            last_line = float(state['lastLine'].get(group, 0))
            # 复读优先：它比"起话题"更像真人，也更省 token（不调模型）
            if self.get('echoEnabled') and now - last_echo >= echo_gap:
                quote = self._random_quote(runtime, group)
                if quote and runtime.queue_outgoing(f'ambient-echo:{group}:{int(now)}',
                                                    'group', group, quote):
                    state['lastEcho'][group] = now
                    state['sent'].append(now)
                    self._save(state)
                    runtime.note(f'群聊主动复读：{group} → {quote[:30]}')
                    return
            if now - last_line >= line_gap:
                text = await self._compose(runtime)
                if text and runtime.queue_outgoing(f'ambient-line:{group}:{int(now)}',
                                                   'group', group, text):
                    state['lastLine'][group] = now
                    state['sent'].append(now)
                    self._save(state)
                    runtime.note(f'群聊主动发言：{group} → {text[:30]}')
                    return

    async def _compose(self, runtime) -> str:
        """让模型写一句短的起话题话；失败就放弃本轮（不硬凑）。"""
        try:
            persona = runtime.style.get()
            system = persona.prompt_text() + (
                '\n现在群里安静了一会儿，你可以自然地起一个很短的话题（一句话，20~30 字），'
                '不要复述群友原话，不要提问太多，不要提到自己是机器人。')
            reply = await runtime.plugins.generate_reply(
                runtime.ai, [{'role': 'user', 'content': '（群里安静了一会儿）'}],
                system_prompt=system, temperature=float(persona.temperature or 0.7),
                max_tokens=120)
        except Exception:
            return ''
        text = re.sub(r'\s+', ' ', str(reply or '')).strip()
        if not text or len(text) > 120:
            return ''
        return text
