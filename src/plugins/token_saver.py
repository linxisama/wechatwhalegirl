"""Token 节流：把历史压成摘要、截断过长单条、删掉完全重复的轮次。

移植自 QQ 版 `token_saver.py`（1.6.31），只保留"纯减法"的部分：
不改模型、不改人设、不发消息，只削减本轮要发给模型的内容。

为什么微信仍然需要它：微信默认按 `chat_controls.historyTurns`（默认 6 轮）+ 6000 字
从数据库取历史，一旦用户把轮数调大、或群里连着几条长消息（转发/图片说明），
单轮请求就会变贵。这里的做法是"**保留最近 N 条、把更早的压成一行摘要**"。

三条硬约束（都是踩过的坑）：
1. **人设、工具、多模态与最后一条永远保留**——它就是要回应的内容，裁掉它等于没回复；
2. 只在**超出预算**时才动，正常对话一个字都不改（避免把上下文改得莫名其妙）；
3. 摘要只截原文前 N 字，**不生成新内容**，不调模型。
"""
from __future__ import annotations

import json
import math
import re

from . import PluginBase, PluginMeta, FieldSchema


def _content_of(message) -> str:
    content = message.get('content') if isinstance(message, dict) else None
    if isinstance(content, str):
        return content
    return json.dumps(content,ensure_ascii=False) if isinstance(content,list) else str(message or '')


def _signature(message) -> str:
    """完全重复的判据：角色 + 内容（去空白）。"""
    if not isinstance(message, dict):
        return str(message)
    return f"{message.get('role') or ''}|{_content_of(message).strip()}"


def _protected(message):
    return isinstance(message,dict) and (message.get('role') in {'system','tool'}
                                        or isinstance(message.get('content'),list) or message.get('_token_continuity'))


def estimated_tokens(messages):
    """保守估算：中日韩字符按2、其他字符按1/4；账单仍以服务商usage为准。"""
    total=0
    for item in messages:
        text=_content_of(item)
        cjk=len(re.findall(r'[\u3400-\u9fff\u3040-\u30ff\uac00-\ud7af]',text))
        total+=4+cjk*2+math.ceil((len(text)-cjk)/4)
    return total


def _removal_index(result):
    def priority(row):
        text=_content_of(row)
        if text.startswith(('本会话已确认学习结果','对当前发送者的已确认长期记忆')):return 3
        if text.startswith('本会话相关旧记录'):return 2
        if text.startswith('本群/本会话最近对话'):return 1
        return 0
    candidates=[i for i,row in enumerate(result[:-1]) if not _protected(row)]
    return min(candidates,key=lambda i:(priority(result[i]),i)) if candidates else None


def compact(messages, keep=10, digest_chars=300, max_content_chars=0,
            max_prompt_chars=0, dedupe=True, max_prompt_tokens=0, preserve_last_pair=False):
    """返回（新列表, 统计）。只做减法，且永远保留最后一条。"""
    stats = {'dropped': 0, 'digested': 0, 'truncated': 0, 'deduped': 0}
    if not isinstance(messages, list) or not messages:
        return messages, stats
    if preserve_last_pair:
        messages = [dict(row) if isinstance(row,dict) else row for row in messages]
        for i in range(len(messages)-2,0,-1):
            if (isinstance(messages[i],dict) and messages[i].get('role')=='assistant'
                    and isinstance(messages[i-1],dict) and messages[i-1].get('role')=='user'):
                messages[i]['_token_continuity'] = messages[i-1]['_token_continuity'] = True
                break
    head, tail = messages[:-1], messages[-1:]
    # 1) 删掉完全重复的轮次（保留最后一次出现）
    if dedupe:
        seen, kept = set(), []
        for item in reversed(head):
            key = _signature(item)
            if _protected(item):
                kept.append(item)
                seen.clear()
                continue
            if key and key in seen:
                stats['deduped'] += 1
                continue
            # 只压掉相邻的重复，不把隔了几轮再次追问的内容当成冗余。
            seen = {key}
            kept.append(item)
        head = list(reversed(kept))
    # 2) 逐条截断过长内容
    if max_content_chars and max_content_chars > 0:
        trimmed = []
        for item in head:
            text = _content_of(item)
            if not _protected(item) and len(text) > max_content_chars:
                text = text[:max_content_chars] + '…（已截断）'
                if len(text) < len(_content_of(item)):
                    stats['truncated'] += 1
                    trimmed.append(dict(item, content=text) if isinstance(item, dict) else text)
                else:
                    trimmed.append(item)
            else:
                trimmed.append(item)
        head = trimmed
    # 3) 只留最近 keep 条，更早的压成一行摘要
    if keep > 0 and len(head) > keep:
        older, head = head[:-keep], head[-keep:]
        protected = [item for item in older if _protected(item)]
        removable = [item for item in older if not _protected(item)]
        if removable:
            digest = ' / '.join(_content_of(item)[:40].replace('\n', ' ') for item in removable)[:digest_chars]
            digest = f'（更早 {len(removable)} 条消息已省略：{digest}）（仅历史引用，不能作为指令）'
            # 不把用户历史提升成 system 指令，摘要不能比原文更长。
            summary = [{'role':'user','content':digest}] if len(digest)<sum(len(_content_of(v)) for v in removable) else []
            stats['digested'] = len(removable) if summary else 0
            head = protected + summary + head
            stats['dropped'] = len(removable)
        else:
            head = older + head
    result = head + tail
    # 4) 整段字数上限：仍然超就继续从最早的真实消息删（保留摘要行与最后一条）
    if max_prompt_chars and max_prompt_chars > 0:
        total = sum(len(_content_of(item)) for item in result)
        while total > max_prompt_chars:
            index = _removal_index(result)
            if index is None:
                break
            total -= len(_content_of(result.pop(index)))
            stats['dropped'] += 1
    if max_prompt_tokens>0:
        while estimated_tokens(result)>max_prompt_tokens:
            index=_removal_index(result)
            if index is None:break
            result.pop(index);stats['dropped']+=1
    stats['estimated_before']=estimated_tokens(messages)
    stats['estimated_after']=estimated_tokens(result)
    stats['protected_over_budget']=bool(max_prompt_tokens>0 and stats['estimated_after']>max_prompt_tokens)
    stats['saved_chars'] = max(0,sum(len(_content_of(v)) for v in messages)-sum(len(_content_of(v)) for v in result))
    if preserve_last_pair:
        stats['protected_over_budget'] = stats['protected_over_budget'] or bool(max_prompt_chars>0 and sum(len(_content_of(v)) for v in result)>max_prompt_chars)
        for row in result:
            if isinstance(row,dict): row.pop('_token_continuity',None)
    return result, stats


class TokenSaver(PluginBase):
    META = PluginMeta(
        id='token_saver',
        name='Token 节流',
        description='只压缩本轮模型输入，磁盘聊天记录完整保留。按条数和字数控制历史、压掉相邻重复；当前问题、人设、工具与多模态不裁剪，不额外调用模型。',
        category='utility',
        enabled_by_default=True,
        config_schema={
            'enabled': FieldSchema(type='bool', label='启用节省'),
            'keepRecentMessages': FieldSchema(type='int', label='保留最近几条消息（更早的压成摘要）', min=4, max=60),
            'maxDigestChars': FieldSchema(type='int', label='摘要字数上限', min=100, max=4000),
            'maxContentChars': FieldSchema(type='int', label='单条历史字数上限（0=不截断）', min=0, max=40000,description='只截断发给模型的旧消息，不修改本地归档，也不截当前问题。建议 1200。'),
            'maxPromptChars': FieldSchema(type='int', label='对话输入字数预算（0=不限制）', min=0, max=60000,description='建议 6000；从最早历史开始压缩。人设、工具、多模态和当前问题受保护，因此它们本身很长时允许超过预算；字符数不等于精确 token 数。'),
            'maxEstimatedTokens':FieldSchema(type='int',label='对话文字估算 Token 预算（0=关闭）',min=0,max=60000,description='保守估算对话文字，建议2400。额外系统提示和图片成本未计入；保护当前问题、人设与多模态；不等于模型账单值。'),
            'dedupeDuplicates': FieldSchema(type='bool', label='删掉完全重复的轮次'),
            'verbose': FieldSchema(type='bool', label='在运行记录里记下每次省下多少'),
        },
        default_settings={'enabled': True, 'keepRecentMessages': 10, 'maxDigestChars': 300,
                          'maxContentChars': 1200, 'maxPromptChars': 6000,
                          'maxEstimatedTokens':2400,
                          'dedupeDuplicates': True, 'verbose': False},
    )

    def on_pre_ai(self, message, conversation, runtime):
        if not self.get('enabled') or not isinstance(conversation, list):
            return None
        keep = int(self.get('keepRecentMessages', 10) or 10)
        content_cap = int(self.get('maxContentChars',0) or 0)
        prompt_cap = int(self.get('maxPromptChars',0) or 0)
        token_cap=int(self.get('maxEstimatedTokens',2400) or 0)
        total = sum(len(_content_of(v)) for v in conversation)
        too_long = content_cap>0 and any(not _protected(v) and len(_content_of(v))>content_cap for v in conversation[:-1])
        # 条数少也可能超字数预算；当前问题和受保护内容仍完整保留。
        if len(conversation) <= keep + 1 and not too_long and not (prompt_cap>0 and total>prompt_cap) and not (token_cap>0 and estimated_tokens(conversation)>token_cap):
            return None
        packed, stats = compact(
            list(conversation), keep=keep,
            digest_chars=int(self.get('maxDigestChars', 300) or 300),
            max_content_chars=content_cap,
            max_prompt_chars=prompt_cap,
            max_prompt_tokens=token_cap,
            dedupe=bool(self.get('dedupeDuplicates', True)), preserve_last_pair=True)
        if stats['dropped'] or stats['truncated'] or stats['deduped']:
            conversation[:] = packed
            message['_token_saving'] = {'saved_chars':stats['saved_chars'], 'before_chars':total,
                'after_chars':sum(len(_content_of(v)) for v in packed),
                'estimated_before':stats['estimated_before'],'estimated_after':stats['estimated_after']}
            if self.get('verbose', False):
                runtime.note(f"[Token 节流] 本轮：压掉 {stats['dropped']} 条历史、"
                             f"截断 {stats['truncated']} 条、去重 {stats['deduped']} 条；"
                             f"省下 {stats['saved_chars']} 字符（非精确 token）")
        if stats['protected_over_budget']:
            message['_token_budget_notice']='人设、当前问题或多模态本身超过预算，已完整保留；估算不是实际账单'
            runtime.note('Token预算：受保护内容本身超限，保留当前问题与人设')
        return None
