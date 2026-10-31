"""AI 唤醒关键词与固定答案关键词分开配置。"""
from . import PluginBase, PluginMeta, FieldSchema
from ..text_matching import configured_terms


class KeywordTrigger(PluginBase):
    META = PluginMeta(id='keyword_trigger', name='关键词唤醒设置', category='management',
        description='设置群友用哪些称呼叫醒机器人，回复内容仍由 AI 生成。若要“一句口令对应一句固定答案”，请使用“关键词自动回复”。',
        config_schema={
            'enabled': FieldSchema(type='bool', label='本页设置', description='关闭后恢复原来的唤醒规则，不代表停止机器人，也不代表只允许 @。'),
            'keywordsEnabled': FieldSchema(type='bool', label='唤醒方式', description='开启：可以通过关键词或 @ 唤醒。关闭：群聊 AI 只由 @ 唤醒，关键词固定回复仍独立生效。'),
            'mode': FieldSchema(type='select', label='如何使用原有关键词', description='保留：以前配置的词继续有效。替换：不再使用旧词表；是否保留人设称呼由下一项决定。', options=[
                {'value':'append','label':'保留原有词表，再加下面的词（推荐）'}, {'value':'replace','label':'替换原有词表，使用下面的词'}]),
            'includePersona': FieldSchema(type='bool', label='人设称呼', description='开启后，人设名称、标识和追加昵称也可以唤醒。当前人设另有专属关键词时，专属词表优先生效。'),
            'keywords': FieldSchema(type='string', label='额外唤醒词', multiline=True,
                description='每行一个词，也可用中文或英文逗号分隔。每个词至少 2 个字符。包含就会触发，例如填“小鲸鱼”，群友说“小鲸鱼，在吗”就会唤醒。留空不等于关闭唤醒。'),
        }, default_settings={'enabled':True,'keywordsEnabled':True,'mode':'append','includePersona':True,'keywords':''})

    def validate_settings(self, settings):
        if any(len(v.strip()) == 1 for v in configured_terms(settings.get('keywords',''))):
            raise ValueError('AI 唤醒词至少需要 2 个字符，避免单字误触发')
