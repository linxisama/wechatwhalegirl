"""图片对话的本地标签与候选排序；没有模型/平台依赖。"""
import json
import re

DIMENSIONS = ('subject', 'emotion', 'action', 'scene', 'text')
# 只从已有画面说明提取明示词，不凭文件名猜人物或补造情绪。
WORDS = {
    'subject': ('猫', '狗', '鲸鱼', '女孩', '男孩', '小人', '机器人', '兔子', '熊猫'),
    'emotion': ('开心', '疑惑', '委屈', '生气', '难过', '得意', '害羞', '惊讶', '无语', '疲惫'),
    'action': ('微笑', '大笑', '哭', '歪头', '点赞', '鼓掌', '挥手', '拥抱', '鞠躬', '睡觉'),
    'scene': ('问候', '感谢', '道歉', '晚安', '加油', '庆祝', '再见'),
}
SYNONYMS = {'高兴': '开心', '快乐': '开心', '笑': '开心', '看不懂': '疑惑', '困惑': '疑惑',
            '不明白': '疑惑', '谢谢': '感谢', '多谢': '感谢', '对不起': '道歉',
            '困了': '疲惫', '困倦': '疲惫', '伤心': '难过', '愤怒': '生气', '赞': '点赞'}


def clean_tags(value, limit=12):
    if isinstance(value, str):
        value = re.split(r'[,，、;；\n]+', value)
    if not isinstance(value, list):
        raise ValueError('标签应为列表或逗号分隔文字')
    result = []
    for item in value:
        if not isinstance(item, str):
            raise ValueError('每个标签必须是文字')
        tag = re.sub(r'[\x00-\x1f\[\]|<>]', '', item).strip()[:32]
        if tag and tag not in result:
            result.append(tag)
    return result[:limit]


def annotation(text):
    """接受本机结构化识图结果，也兼容已有模型的普通中文说明。"""
    raw = str(text or '').strip()
    if not raw or len(raw) > 12000:
        raise ValueError('识图结果为空或过长')
    body = re.sub(r'^```(?:json)?\s*|\s*```$', '', raw).strip()
    try:
        parsed = json.loads(body)
    except ValueError:
        parsed = None
    features = {key: [] for key in DIMENSIONS}
    if isinstance(parsed, dict):
        description = str(parsed.get('description') or parsed.get('summary') or '').strip()
        source = parsed.get('features', parsed)
        if not isinstance(source, dict) or not description:
            raise ValueError('图片标注结构缺少说明或特征')
        for key in DIMENSIONS:
            features[key] = clean_tags(source.get(key, []), 4)
        extra = clean_tags(parsed.get('tags', []), 12)
    elif parsed is not None:
        raise ValueError('图片标注应为对象')
    else:
        if raw[:1] in '[{':
            raise ValueError('图片标注JSON无效')
        description, extra = raw, []
        for key, words in WORDS.items():
            features[key] = [word for word in words if word in description][:4]
        for old, normalized in SYNONYMS.items():
            if old in description and normalized in WORDS['emotion'] and normalized not in features['emotion']:
                features['emotion'].append(normalized)
        features['text'] = re.findall(r'[「“"]([^」”"\n]{1,32})[」”"]', description)[:3]
    tags = clean_tags([tag for values in features.values() for tag in values] + extra)
    return {'description': description[:1000], 'features': features, 'auto_tags': tags}


def searchable(entry):
    return ' '.join([str(entry.get('label') or ''), str(entry.get('description') or ''),
                     *map(str, entry.get('manual_tags') or []), *map(str, entry.get('auto_tags') or [])])


def _terms(text):
    value = str(text or '').casefold()[:3000]
    for old, normalized in SYNONYMS.items():
        if old in value:
            value += ' ' + normalized
    chunks = re.findall(r'[\u4e00-\u9fff]+|[a-z0-9]+', value)
    return set(term for chunk in chunks for term in ([chunk] if len(chunk) < 3 else
               [chunk] + [chunk[i:i+2] for i in range(len(chunk)-1)]))


def rank(entries, query, limit=12):
    terms = _terms(query)
    def score(entry):
        text = _terms(searchable(entry))
        overlap = len(terms & text)
        # 相关性优先，其次避免老是重复同一张；这是本地词义检索，不冒充向量模型。
        return (overlap, bool(entry.get('description')), -float(entry.get('last_used') or 0))
    return sorted(entries, key=score, reverse=True)[:max(1, min(20, limit))]
