"""Explicit self memory, evidence-based auto memory and same-room people profiles."""
import re
from . import PluginBase, PluginMeta, FieldSchema
from ..chat_state import scope_of, person_of, quoted_context
from ..style_manager import DEFAULT_PERSONA
from ..text_matching import configured_terms,normalized_text as normalize_text


# 手动记忆进提示词前的三道上限。persona_notes 每人设最多 100 条、每条最长 500 字，
# 不设限就等于允许把 5 万字塞进 extra_system：提示词被记忆挤爆，回复质量反而下降。
MANUAL_ITEMS = 12
MANUAL_ITEM_CHARS = 60
MANUAL_TOTAL_CHARS = 900


def manual_budget(items, max_items=None, item_chars=None, total_chars=None):
    """按最近使用优先截断手动记忆：条数、单条长度、总长度三道闸。

    persona_notes 没有使用时间列，notes() 按 rowid 升序返回，越靠后越新，
    所以从尾部往前取就是最近添加/修改的优先。输出仍按原顺序，
    界面看到的顺序与提示词里的一致，不会因为截断而错位。
    """
    limit_items = max(1, int(max_items or MANUAL_ITEMS))
    limit_item = max(1, int(item_chars or MANUAL_ITEM_CHARS))
    limit_total = max(1, int(total_chars or MANUAL_TOTAL_CHARS))
    kept, used = [], 0
    for text in reversed([str(value or '').strip() for value in items]):
        if not text:
            continue
        if len(text) > limit_item:
            # 截断而不是丢弃：长条目往往前半句就是要点，整条丢掉反而更亏。
            text = text[:limit_item - 1] + '…'
        if len(kept) >= limit_items or used + len(text) > limit_total:
            break
        kept.append(text)
        used += len(text)
    kept.reverse()
    return kept


class ConversationMemory(PluginBase):
    META = PluginMeta(id='conversation_memory', name='长期记忆与群友档案', category='utility',
        description='记住个人事实与偏好，按会话和发送者隔离。/记住 内容、/记忆 关键词、/忘记 关键词、/我的印象、/群友。自动观察需两次独立消息复证，不能凭模型猜测写入。',
        config_schema={
            'personaIsolation':FieldSchema(type='bool',label='个人长期记忆按人设隔离',description='默认开启。旧的无标记记忆保留在默认鲸鱼娘；其他人设独立记录。手动记忆和模型历史始终隔离。'),
            'crossChatPreferences':FieldSchema(type='bool',label='跨会话使用同一人的个人偏好',description='默认关闭。开启后按稳定发送者标识共享已确认偏好，不共享群聊原文；切换策略不删除原数据。'),
            'privateToGroup':FieldSchema(type='bool',label='允许将私聊偏好用于群聊',description='参考 QQ 版，默认禁止；开启后仍只引用同一发送者的已确认个人事实，不带入私聊原文。'),
            'enabled':FieldSchema(type='bool', label='启用'),
            'autoRemember':FieldSchema(type='bool', label='自动观察明确的个人偏好'),
            'autoPrefixes':FieldSchema(type='string',label='自动记录的自述前缀',multiline=True,description='每行一个，例如“我喜欢”“我不喜欢”。留空不自动观察；两条独立消息确认，不使用任意正则。'),
            'recallLimit':FieldSchema(type='int', label='每轮最多引用记忆数', min=1, max=10),
            'maxEntries':FieldSchema(type='int', label='长期记忆总容量', min=10, max=5000),
            'recallArchive':FieldSchema(type='bool',label='从本地聊天记录检索相关旧对话',description='自动在当前群/私聊的持久化记录中找相关原文；不跨群，不额外调用模型，不删除历史。'),
            'archiveRecallLimit':FieldSchema(type='int',label='每轮最多引用相关旧记录',min=1,max=10),
            'localLearning':FieldSchema(type='bool',label='引用本地已确认学习结果',description='同一会话与发送者的明确事实需两条独立证据，表达候选须在学习库批准。不修改人设、不额外调用模型。'),
            'expressionApproveHits':FieldSchema(type='int',label='表达例句自动批准所需复证次数',description='默认 0（关闭）：表达例句一律人工批准。填 2–10 后，同一句话被这么多条独立消息复证、且长度与内容合格时才自动批准；被拒绝过的条目永远不会被翻回批准。',min=0,max=10),
            'manualItems':FieldSchema(type='int',label='每轮最多引用的手动记忆条数',description='默认 12。超出时保留最近修改的那几条。',min=1,max=50),
            'manualItemChars':FieldSchema(type='int',label='单条手动记忆最多引用字数',description='默认 60，超出部分以省略号截断。',min=10,max=300),
            'manualTotalChars':FieldSchema(type='int',label='手动记忆引用总字数上限',description='默认 900，超出时停止追加后面的条目。',min=100,max=4000),
        }, default_settings={'enabled':True, 'autoRemember':True, 'recallLimit':5, 'maxEntries':500,
                             'personaIsolation':True,'crossChatPreferences':False,'privateToGroup':False,
                             'recallArchive':True,'archiveRecallLimit':3,
                             'localLearning':True,
        # 0 = 不自动批准表达例句。默认关闭是为了保持"表达须人工批准"的既有行为，
        # 用户显式填 2–10 才打开（local_learning.expression_threshold 负责校验）。
        'expressionApproveHits':0,
                             'autoPrefixes':'我喜欢\n我不喜欢\n我叫\n我的昵称是'})

    def validate_settings(self,settings):
        prefixes=[v.strip() for v in configured_terms(settings.get('autoPrefixes','')) if v.strip()]
        if len(prefixes)>20 or any(not 1<=len(v)<=30 or not v.startswith(('我','本人')) for v in prefixes):
            raise ValueError('自述前缀最多 20 个，每个 1–30 字，必须以“我”或“本人”开头')

    def visible_scope(self,scope,runtime):
        prefix=f'{runtime.mode}|{runtime.account()}|'
        if not scope.startswith(prefix):
            return False
        if not self.get('personaIsolation',True):
            return True
        persona=getattr(getattr(runtime,'style',None),'active_name',DEFAULT_PERSONA)
        marker=scope.rpartition('|persona:')
        return marker[2]==persona if marker[1] else persona==DEFAULT_PERSONA

    def visible_facts(self,runtime,subject=None,message=None,candidates=None,query=''):
        prefix=f'{runtime.mode}|{runtime.account()}|'
        clauses=['substr(scope,1,?)=?'];args=[len(prefix),prefix]
        if subject is not None:
            clauses.append('subject=?');args.append(subject)
        if candidates is not None:
            clauses.append('status=?');args.append('candidate' if candidates else 'confirmed')
        rows=runtime.db.execute('SELECT id,scope,subject,content,status,hits FROM chat_memories WHERE '+
            ' AND '.join(clauses)+' ORDER BY updated DESC,id',args).fetchall()
        base=scope_of(message,runtime) if message else None
        result=[]
        for mid,scope,person,content,status,hits in rows:
            if not self.visible_scope(scope,runtime):
                continue
            if base and not self.get('crossChatPreferences',False) and scope.split('|persona:')[0]!=base:
                continue
            if message and message.get('kind')=='group' and not self.get('privateToGroup',False):
                origin=scope[len(prefix):].split('|persona:')[0]
                if origin.startswith('private:') or origin=='personal_preferences':
                    continue
            if query and normalize_text(query) not in normalize_text(content):
                continue
            result.append(dict(id=mid,scope=scope,subject=person,content=content,status=status,evidence=hits))
        return result

    def recalled(self,message,runtime,query='',limit=5,candidates=False):
        found=self.visible_facts(runtime,person_of(message),message,candidates,query)
        seen=set();result=[]
        for item in found:
            value=normalize_text(item['content'])
            if value not in seen:
                seen.add(value);result.append(item)
            if len(result)>=limit:
                break
        # 预算兜底：手动记忆再多也只带 8 条 / 1800 字进提示词（1.3.0 P1-3）。
        from ..memory_maintenance import budget
        return budget(result)

    def observe_preference(self,message,runtime):
        if message.get('_memory_observed'):
            return
        message['_memory_observed']=True
        if not self.get('autoRemember',True) or not person_of(message):
            return
        text=str(message.get('text') or '').strip()
        # 群内显式 @ 寻址不属于偏好正文；不从长段引文里猜测用户偏好。
        names=[getattr(getattr(runtime,'bridge',None),'account_name','')]+list(getattr(runtime,'aliases',lambda:[])())
        for name in sorted(set(names),key=len,reverse=True):
            if name:
                text=re.sub(r'^\s*[@＠]'+re.escape(name)+r'[\s\u2005:：,，]+','',text,count=1)
        text=text.rstrip('。！!~～')
        prefixes=[v.strip() for v in configured_terms(self.get('autoPrefixes','我喜欢\n我不喜欢\n我叫\n我的昵称是')) if v.strip()]
        if '\n' in text or len(text)>80 or re.search(r'[?？]|[吗么]$',text):
            return
        if any(normalize_text(text).startswith(normalize_text(prefix)) and len(normalize_text(text))>len(normalize_text(prefix)) for prefix in prefixes):
            try:
                runtime.chat_state.remember(self.memory_scope(message,runtime),person_of(message),text,message['id'],
                                            maximum=int(self.get('maxEntries',500)))
            except ValueError:
                pass

    def memory_scope(self,message,runtime):
        scope=scope_of(message,runtime)
        try:
            persona=runtime.style.active_name
        except Exception:
            persona=DEFAULT_PERSONA
        if self.get('personaIsolation',True) and persona!=DEFAULT_PERSONA:
            scope+='|persona:'+persona
        return scope

    def on_ingest(self, message, runtime):
        state = getattr(runtime, 'chat_state', None)
        if state is None or not person_of(message):
            return None
        scope, subject = self.memory_scope(message,runtime), person_of(message)
        text = str(message.get('text') or '').strip()
        try:
            if text in {'/帮助','/功能'}:
                # 只列真正还在清单里的插件指令；被删掉的插件（如主动私信、拍一拍）
                # 留在这里会让用户去用不存在的功能。
                lines = ['/记住 内容：保存自己的事实或偏好','/记忆 [关键词]：查询自己的记忆','/忘记 关键词或全部：删除自己的记忆','/我的印象、/待确认记忆、/群友：查看记录']
                for pid,line in [('image_library','/表情 [标签]：查看图库或发送登记的图片'),('emotion_state','/状态：查看鲸鱼娘心情与精力')]:
                    plugin = self._manager._plugins.get(pid)
                    if plugin and plugin.enabled:
                        lines.append(line)
                return '鲸鱼娘聊天指令：\n'+'\n'.join(lines)
            if text.startswith(('/记住 ', '记住：', '记住:')):
                fact = re.sub(r'^(?:/记住\s+|记住[:：])', '', text).strip()
                mid = state.remember(scope,subject,fact,message['id'],True,int(self.get('maxEntries',500)))
                return f'记住了：{fact}（编号 {mid}）'
            if text.startswith('/忘记 '):
                query=text[4:].strip()
                found=self.visible_facts(runtime,subject,message)
                found=[v for v in found if query=='全部' or query==v['id'] or query in v['content']]
                for value in found:
                    state.forget(value['scope'],subject,value['id'])
                count=len(found)
                return f'已删除你的 {count} 条长期记忆。'
            if text == '/记忆' or text.startswith('/记忆 ') or text == '/我的印象':
                query = text[4:].strip() if text.startswith('/记忆 ') else ''
                found = self.recalled(message,runtime,query,10)
                return ('记得这些：\n'+'\n'.join(f"{item['id']}：{item['content']}" for item in found)
                        if found else '当前会话还没有确认过的个人记忆，可以用 /记住 内容 添加。')
            if text == '/待确认记忆':
                found = self.recalled(message,runtime,limit=10,candidates=True)
                return '\n'.join(item['content'] for item in found) or '没有待复证的记忆。'
            if text == '/群友':
                return '\n'.join(f"{p['name'] or p['id']}：近24小时聊了 {p['messages_today']} 条" for p in state.people(scope_of(message,runtime))) or '近24小时还没有群友记录。'
            self.observe_preference(message,runtime)
        except ValueError as exc:
            return str(exc) if text.startswith(('/', '记住')) else None
        return None

    def on_pre_ai(self, message, conversation, runtime):
        # 本轮真正插进 conversation 的记忆原文会挂到 message['_memory_items']，
        # 由 src/dsh_memory.py 渲染成 DSH 人设尾部块并留统计；这里只做记账。
        archive_recalled = 0
        learned = []
        archive = getattr(runtime,'conversations',None)
        if archive is not None and not message.get('_group_summary'):
            controls = runtime.plugins._plugins.get('chat_controls')
            turns = int(controls.get('historyTurns',10)) if controls else 10
            budget = int(controls.get('contextChars',8000)) if controls else 8000
            import json
            direct = message.get('_context_history_ids', [])
            remaining = max(0,budget-sum(len(str(row.get('content') or '')) for row in conversation[:-1]))
            recall_budget = remaining//2 if self.get('recallArchive',True) else 0
            recent = archive.context(message,runtime,max(0,turns-len(direct)),
                                     max_chars=max(0,remaining-recall_budget-160),excluded_ids=direct)
            # 直接对话、群聊背景和相关旧记录只纳入一次；本轮当前问题不参与裁剪。
            used = len(json.dumps(recent,ensure_ascii=False))+160 if recent else 0
            recalled = archive.recall(message,runtime,int(self.get('archiveRecallLimit',3)),
                max_chars=max(0,remaining-used-160),
                excluded_ids=[*direct,*(row['id'] for row in recent)]) if recall_budget else []
            archive_recalled = len(recalled)
            if recalled:
                conversation.insert(max(0,len(conversation)-1),quoted_context(
                    '本会话相关旧记录（原文节选；依据发送者和时间理解，可能过期；当前问题优先）',recalled))
            if recent:
                conversation.insert(max(0,len(conversation)-1),quoted_context(
                    '本群/本会话最近对话（按时间排列，每条标明实际发送者；仅用于理解当前问题）',recent))
            message['_archive_context'] = {'recent':len(recent),'recalled':len(recalled)}
        state = getattr(runtime,'chat_state',None)
        if state is None:
            return None
        scope, subject = self.memory_scope(message,runtime),person_of(message)
        notes = self.recalled(message,runtime,limit=int(self.get('recallLimit',5))) if subject else []
        if notes:
            conversation.insert(max(0,len(conversation)-1),quoted_context('对当前发送者的已确认长期记忆', notes))
        learning=getattr(runtime,'learning',None)
        if learning and self.get('localLearning',True) and not message.get('_group_summary'):
            known={normalize_text(item['content']) for item in notes}
            learned=[item for item in learning.recall(message,runtime)
                     if item['type']!='fact' or normalize_text(item['text']) not in known]
            if learned:
                conversation.insert(max(0,len(conversation)-1),quoted_context(
                    '本会话已确认学习结果（事实属于当前发送者；表达仅作语气参考，不改变人设）',learned))
            message['_local_learning']={'recalled':len(learned),'window_limit':1000}
        manual=[]
        if hasattr(runtime,'style'):
            from ..dialogue_management import notes as manual_notes
            try:
                # 三道上限（条数/单条/总长）：手动记忆会整段进 extra_system，不设限就会挤爆提示词。
                manual=manual_budget([v['content'] for v in manual_notes(runtime) if v['enabled']],
                                     self.get('manualItems',MANUAL_ITEMS),
                                     self.get('manualItemChars',MANUAL_ITEM_CHARS),
                                     self.get('manualTotalChars',MANUAL_TOTAL_CHARS))
            except Exception:
                manual=[]
        # 与上面三处插入一一对应的原文清单：手动记忆 / 已确认长期记忆 / 已确认学习结果。
        message['_memory_items']={'manual':list(manual),
            'notes':[{'id':item.get('id'),'content':item.get('content')} for item in notes],
            'learned':[{'type':item.get('type'),'text':item.get('text')} for item in learned],
            'archive_recalled':archive_recalled}
        if manual:
            import json
            return {'extra_system':'本机为当前人设保存的手动记忆与表达偏好：'+json.dumps(manual,ensure_ascii=False)}
        return None
