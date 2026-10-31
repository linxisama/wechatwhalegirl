"""人设文本兼容层；只解析数据，不读取文件中引用的路径。"""
from __future__ import annotations

import hashlib
import math
import re
from pathlib import Path

import yaml

MAX_FILE = 5 * 1024 * 1024
MAX_PROMPT = 256 * 1024
NAME_RE = re.compile(r'^[A-Za-z0-9_-]{1,48}$')
RESERVED = {'CON', 'PRN', 'AUX', 'NUL', *(f'COM{i}' for i in range(1, 10)),
            *(f'LPT{i}' for i in range(1, 10))}


def read_persona_file(path):
    with Path(path).open('rb') as stream:
        raw = stream.read(MAX_FILE + 1)
    if len(raw) > MAX_FILE:
        raise ValueError('人设文件最大 5 MB')
    if raw.startswith((b'\xff\xfe', b'\xfe\xff')):
        encodings = ('utf-16',)
    else:
        encodings = ('utf-8-sig', 'gb18030')
    for encoding in encodings:
        try:
            text = raw.decode(encoding)
            if '\0' not in text:
                return text
        except UnicodeError:
            continue
    raise ValueError('无法解码人设文件，请保存为 UTF-8 或带 BOM 的 UTF-16 文本')


class PersonaLoader(yaml.SafeLoader):
    def compose_node(self, parent, index):
        self._nodes = getattr(self, '_nodes', 0) + 1
        self._depth = getattr(self, '_depth', 0) + 1
        try:
            if self._nodes > 20000 or self._depth > 40:
                raise ValueError('人设结构过于复杂；请使用普通键值字段')
            return super().compose_node(parent, index)
        finally:
            self._depth -= 1

    def construct_mapping(self, node, deep=False):
        result = {}
        for key_node, value_node in node.value:
            key = self.construct_object(key_node, deep=deep)
            if not isinstance(key, str):
                raise ValueError('人设字段名必须是文字')
            if key in result:
                raise ValueError(f'人设字段「{key[:60]}」重复，请保留一份')
            result[key] = self.construct_object(value_node, deep=deep)
        return result


def parse_persona(content, filename=''):
    if not isinstance(content, str) or not content.strip():
        raise ValueError('人设文件是空的')
    if len(content.encode('utf-8')) > MAX_FILE:
        raise ValueError('人设文件最大 5 MB')
    text = content.lstrip('\ufeff')
    plain = Path(filename).suffix.lower() == '.txt'
    try:
        data = yaml.load(text, Loader=PersonaLoader)
    except yaml.YAMLError as exc:
        if not plain:
            mark = getattr(exc, 'problem_mark', None)
            location = f'（第 {mark.line + 1} 行）' if mark else ''
            raise ValueError('YAML/JSON 格式错误'+location+'，请检查缩进、引号或逗号') from None
        data = None
    if plain and (not isinstance(data, dict) or not any(k in data for k in
                  ('system_prompt', 'prompt', 'persona', 'format'))):
        data = {'system_prompt': text.strip(), 'display_name': Path(filename).stem}
        source_format = 'TXT 原文'
    else:
        source_format = 'YAML/JSON 人设'
    if not isinstance(data, dict):
        raise ValueError('人设文件必须是 YAML/JSON 对象；纯文字请保存为 .txt')
    envelope = data
    if 'persona' in data or 'format' in data:
        if data.get('format') not in (None, 'WhaleGirlPersona/v1'):
            raise ValueError('不支持此人设文件版本，请导出为 WhaleGirlPersona/v1')
        if not isinstance(data.get('persona'), dict):
            raise ValueError('persona 字段必须是人设对象，不能是文字或列表')
        data = data['persona']
        source_format = 'WhaleGirlPersona/v1'
    warnings = []
    prompt = data.get('system_prompt')
    if not prompt and isinstance(data.get('prompt'), str):
        prompt = data['prompt']
        warnings.append('已将 prompt 字段识别为人设本体')
    if not isinstance(prompt, str) or not prompt.strip():
        raise ValueError('没有有效的人设本体：请填写 system_prompt 或 prompt；纯文字可保存为 .txt')
    if len(prompt.encode('utf-8')) > MAX_PROMPT:
        raise ValueError('人设本体最大 256 KB，请精简正文')
    persona = {'system_prompt': prompt.strip()}
    for key in ('name', 'display_name', 'description', 'image_note'):
        value = data.get(key, '')
        if value is None:
            value = ''
        if not isinstance(value, str):
            raise ValueError(f'{key} 必须是文字')
        persona[key] = value.strip()
    if not persona['display_name']:
        persona['display_name'] = persona['name'] or Path(filename).stem or '导入人设'
    for key, default in (('temperature', .7), ('max_tokens', 500)):
        value = data.get(key, default)
        try:
            number = float(value)
            if isinstance(value, bool) or not math.isfinite(number) or (key == 'max_tokens' and not number.is_integer()):
                raise ValueError()
        except (ValueError, TypeError, OverflowError):
            raise ValueError(f'{key} 必须是有效'+('整数' if key == 'max_tokens' else '数字')) from None
        persona[key] = int(number) if key == 'max_tokens' else number
        low, high = (16, 8000) if key == 'max_tokens' else (0, 2)
        if not low <= number <= high:
            warnings.append(f'{key} 已限制在 {low}–{high} 范围内')
            persona[key] = min(max(persona[key], low), high)
    examples = data.get('examples', [])
    if not isinstance(examples, (str, list)):
        raise ValueError('examples 应为示例列表或「用户：… / 你：…」文本')
    if isinstance(examples, list):
        if len(examples) > 100:
            raise ValueError('对话示例最多 100 组')
        for example in examples:
            if (not isinstance(example, dict) or any(not isinstance(example.get(key), str)
                    or not example[key].strip() for key in ('user', 'assistant'))):
                raise ValueError('每组 examples 都需要非空的 user 和 assistant 文字')
    elif examples.strip():
        from .style_manager import StyleManager
        examples = re.sub(r'\s+/\s+(?=(?:你|assistant|A)[:：])', '\n', examples)
        if not StyleManager._parse_examples_text(examples):
            raise ValueError('对话示例文本无法识别，请按「用户：问题」换行「你：回答」填写')
    persona['examples'] = examples
    keywords = data.get('keywords', [])
    if not isinstance(keywords, (str, list)) or (isinstance(keywords, list) and
                                                any(not isinstance(item, str) for item in keywords)):
        raise ValueError('keywords 应为文字或关键词列表')
    from .text_matching import configured_terms
    terms = list(dict.fromkeys(configured_terms(keywords)))
    if len(terms) > 50 or any(not 2 <= len(term) <= 40 for term in terms):
        raise ValueError('人设关键词最多 50 个，每个 2–40 个字符')
    persona['keywords'] = terms
    image_data = envelope.get('image_data', data.get('image_data', ''))
    if not isinstance(image_data, str):
        raise ValueError('image_data 必须是内嵌图片的 Base64 文字')
    if data.get('image') and not image_data:
        warnings.append('未导入文件内的图片路径；可在导入后单独上传形象图')
    known = set(persona) | {'prompt', 'image', 'image_data', 'image_ext', 'original_name'}
    extras = [str(key)[:40] for key in data if key not in known]
    if extras:
        warnings.append('以下字段未用于回复：'+', '.join(extras[:10]))
    return {'persona': persona, 'image_data': image_data, 'source_format': source_format,
            'warnings': warnings, 'prompt_chars': len(persona['system_prompt'])}


def import_name(bot, persona, requested='', filename='', automatic=False):
    if not automatic:
        name = str(requested or persona.get('name') or '').strip()
        if not NAME_RE.fullmatch(name) or name.upper() in RESERVED:
            raise ValueError('人设标识只能用字母、数字、下划线或短横线（1–48 位），不能使用 Windows 保留名')
        if (name.casefold() in {item['name'].casefold() for item in bot.style.list_styles()}
                or (bot.style.custom_dir / (name+'.yaml')).exists()
                or (bot.style.styles_dir / (name+'.yaml')).exists()):
            raise ValueError('同名人设已存在，请填写新的导入标识')
        return name
    candidates = [persona.get('name'), requested, Path(filename).stem]
    name = next((str(item) for item in candidates if item and NAME_RE.fullmatch(str(item))
                 and str(item).upper() not in RESERVED), '')
    if not name:
        name = 'imported_' + hashlib.sha256((persona['display_name']+persona['system_prompt']).encode('utf-8')).hexdigest()[:10]
    existing = {item['name'].casefold() for item in bot.style.list_styles()}
    for number in range(1, 10001):
        suffix = '' if number == 1 else f'_{number}'
        candidate = name[:48-len(suffix)] + suffix
        if (candidate.casefold() not in existing and not (bot.style.custom_dir / (candidate+'.yaml')).exists()
                and not (bot.style.styles_dir / (candidate+'.yaml')).exists()):
            return candidate
    raise ValueError('同名副本太多，请更换文件名后导入')
