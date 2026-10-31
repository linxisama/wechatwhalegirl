"""称呼只影响模型表达；快速模式不放宽实际发送的安全闸门。"""
import re
from . import PluginBase, PluginMeta, FieldSchema
from ..chat_state import turn_text


class ReplyPreferences(PluginBase):
    META = PluginMeta(id='reply_preferences',name='回复称呼与速度',category='management',
        description='默认不以微信昵称或 @昵称起句；快速模式缩短连发等待、跳过模拟打字，在支持的模型上关闭思考。四秒为优化目标，实际受模型接口和发送耗时影响。',
        config_schema={
            'enabled':FieldSchema(type='bool',label='启用回复优化'),
            'hideNicknames':FieldSchema(type='bool',label='不自动带微信昵称或 @称呼'),
            'fastMode':FieldSchema(type='bool',label='快速回复模式',description='优先马上回答，覆盖连发合并和模拟打字的等待，保留发送间隔与单线程闸门。'),
            'mergeMs':FieldSchema(type='int',label='快速模式合并等待（毫秒）',min=0,max=2000),
            'dynamicTemperature':FieldSchema(type='bool',label='温度随对话起伏',description='以当前人设温度为基准，求助/纠正时降低，创作/活跃闲聊时提高。仅支持允许temperature的模型。'),
            'temperatureSwing':FieldSchema(type='float',label='温度最大浮动幅度',min=0,max=0.4),
            'thinkingMode':FieldSchema(type='select',label='模型思考模式',options=[
                {'value':'auto','label':'自动：快速模式尝试关闭可切换模型的思考'},
                {'value':'inherit','label':'沿用服务商默认'},
                {'value':'disabled','label':'明确关闭思考（接口需支持）'}]),
        },default_settings={'enabled':True,'hideNicknames':True,'fastMode':True,'mergeMs':800,'thinkingMode':'auto',
                            'dynamicTemperature':True,'temperatureSwing':0.15})

    def temperature(self, base, text, history=()):
        from ..dialogue_context import temperature
        return temperature(base,text,history,self.enabled and self.get('dynamicTemperature',True),self.get('temperatureSwing',.15))

    def fast(self):
        return self.enabled and self.get('fastMode',True)

    def model_options(self):
        mode=self.get('thinkingMode','auto')
        return {'thinking':'disabled'} if self.enabled and mode=='disabled' else (
            {'thinking':'auto_fast'} if self.fast() and mode=='auto' else {})

    def model_turn(self,message,runtime):
        copy=dict(message)
        if self.enabled and self.get('hideNicknames',True):
            text=str(copy.get('text') or '')
            names=[getattr(runtime.bridge,'account_name','')]+list(runtime.aliases())
            for name in sorted({str(n).strip() for n in names if n},key=len,reverse=True):
                # 只去掉开头完整的寻址称呼，不改引文、邮件、正文中的其他人名。
                text=re.sub(r'^\s*[@＠]\s*'+re.escape(name)+r'(?=[\s\u2005:：,，。!?！？]|$)[\s\u2005:：,，]*','',text,count=1)
            copy['text']=text or str(message.get('text') or '')
        return turn_text(copy)

    def clean_reply(self,reply,message):
        if not self.enabled or not self.get('hideNicknames',True):
            return reply
        text=str(reply)
        names={str(message.get(k) or '').strip() for k in ('sender','sender_name')}
        for _ in range(3):
            before=text
            for name in sorted(names-{''},key=len,reverse=True):
                # 裸昵称仅在后接称呼标点时移除；不能删“董的电脑”这样的主语。
                text=re.sub(r'^\s*[@＠]\s*'+re.escape(name)+r'(?=[\s\u2005:：,，。!?！？]|$)[\s\u2005:：,，]*','',text,count=1)
                text=re.sub(r'^\s*'+re.escape(name)+r'\s*[:：,，]\s*','',text,count=1)
            if text==before:
                break
        return text.lstrip()

    def on_pre_ai(self,message,conversation,runtime):
        rules=[]
        if self.get('hideNicknames',True):
            rules.append('微信账号名、群昵称和发送者资料仅用于区分会话，不是你的人设名或称呼要求。以当前人设自称，不把微信昵称带入自我介绍；回复直接说内容，不用对方微信昵称开头，不自动添加 @任何人。正文需要谈论的人名可正常提及。')
        if self.fast():
            rules.append('普通闲聊优先直接用一两句短话回应，不重复题目或展开分析；技术求助按实际需要给完整步骤。')
        return {'extra_system':'\n'.join(rules)} if rules else None
