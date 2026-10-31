"""仅净化诊断导出副本中的常见凭据；不读取配置、不修改原日志。

这不是通用隐私擦除：普通聊天、用户名与本地路径仍可能出现在排障材料中。
"""
import re

REDACTED = '[已脱敏]'
_CREDENTIAL_NAMES = (
    r'api[_-]?key(?:[_-]?encrypted)?|x-api-key|access[_-]?token|refresh[_-]?token|'
    r'(?:api|auth|oauth|bearer|webhook|bot)[_-]?token|session[_-]?(?:token|key)|'
    r'token(?:[_-]?(?:hash|salt|encrypted))?|(?:client|shared|webhook)[_-]?secret|'
    r'secret(?:[_-]?key)?(?:[_-]?encrypted)?|'
    r'password(?:[_-]?(?:hash|salt|encrypted))?|passwd|pwd|'
    r'credentials?|authorization|proxy-authorization|cookie|set-cookie|'
    r'private[_-]?key|signing[_-]?key|encryption[_-]?key'
)
_FIELD = re.compile(
    r'(?<![\w-])(?:[\"\']?)(?P<label>(?:[A-Za-z_][A-Za-z0-9_-]*\.)*'
    r'(?:' + _CREDENTIAL_NAMES + r'))(?:[\"\']?)[ \t]*[:=][ \t]*', re.I)
_BARE_END = re.compile(r'[\r\n,;&}\])\"\'<>]|[ \t]+(?=[A-Za-z_][A-Za-z0-9_.-]*[ \t]*[:=])')
_AUTH = re.compile(r'\b(Bearer|Basic)([ \t]+)[^\s\"\'<>;,)}\]]+', re.I)
_CALLBACK = re.compile(r'(/api/wechat/hook/callback/)[A-Za-z0-9_%~-]+', re.I)
_USERINFO = re.compile(r'(https?://)[^/\s@]+@', re.I)
_KNOWN_KEY = re.compile(r'(?<![\w-])(?:sk-[A-Za-z0-9_-]{8,}|AIza[A-Za-z0-9_-]{20,}|'
                        r'(?:ghp_|gho_|github_pat_)[A-Za-z0-9_]{16,})(?![\w-])')
_PRIVATE_KEY = re.compile(r'-----BEGIN ((?:[A-Z]+ )?PRIVATE KEY)-----.*?-----END \1-----', re.S)
_NEXT_RECORD = re.compile(r'(?:Traceback \(most recent call last\)|\d{4}-\d\d-\d\d \d\d:\d\d:\d\d)')
_YAML_BLOCK = re.compile(r'[|>][-+1-9]*[ \t]*(?:#[^\r\n]*)?(?:\r?\n|$)')


def redact_diagnostic_text(value):
    """保留记录头、阶段码及堆栈，覆盖有明确标签或已知格式的凭据值。"""
    text = str(value or '')
    pieces, consumed = [], 0
    for match in _FIELD.finditer(text):
        if match.start() < consumed:
            continue
        start = match.end()
        if start >= len(text) or text[start] in '\r\n,;&}])':
            continue
        end, quote = start, ''
        if text.startswith(REDACTED, start):
            end = start + len(REDACTED)
        elif text[start] in '\"\'':
            quote = text[start]
            end += 1
            while end < len(text):
                if text[end] in '\r\n':
                    break  # 损坏的单行字段不能吞掉下一行的整个排障堆栈。
                if text[end] == '\\':
                    end += 2
                    continue
                if text[end] == quote:
                    end += 1
                    break
                end += 1
        elif text[start] in '[{':
            # credentials 有时是整个 JSON/Python 映射或列表，不能只隐藏首个值。
            stack, nested_quote = [text[start]], ''
            end += 1
            while end < len(text) and stack:
                character = text[end]
                if nested_quote:
                    if character == '\\':
                        end += 2
                        continue
                    if character == nested_quote:
                        nested_quote = ''
                elif character in '\"\'':
                    nested_quote = character
                elif character in '[{':
                    stack.append(character)
                elif character in ']}':
                    if (stack[-1], character) not in (('[', ']'), ('{', '}')):
                        break
                    stack.pop()
                elif character in '\r\n' and _NEXT_RECORD.match(text, end + 1):
                    break  # 未闭合字段不能连带吞掉下一条日志/异常。
                end += 1
        elif text[start] in '|>' and (block := _YAML_BLOCK.match(text, start)):
            line_start = text.rfind('\n', 0, match.start()) + 1
            before_key = text[line_start:match.start()]
            indentation = len(before_key) - len(before_key.lstrip(' \t'))
            end = block.end()
            while end < len(text):
                line_end = text.find('\n', end)
                line_end = len(text) if line_end < 0 else line_end + 1
                line = text[end:line_end]
                if line.strip() and len(line) - len(line.lstrip(' \t')) <= indentation:
                    break
                end = line_end
            # 保留后续记录的换行，避免把新的异常头接在替换文本后面。
            if end > start and text[end - 1] == '\n':
                end -= 1
        else:
            # Cookie 头含多个分号字段，其全部内容都属于凭据。
            header = match['label'].lower() in ('cookie', 'set-cookie', 'authorization', 'proxy-authorization')
            stop = re.search(r'[\r\n]', text[start:]) if header else _BARE_END.search(text, start)
            if header and stop:
                end = start + stop.start()
            else:
                end = stop.start() if stop else len(text)
        if end <= start:
            continue
        pieces.append(text[consumed:start])
        pieces.append(quote + REDACTED + quote)
        consumed = min(end, len(text))
    pieces.append(text[consumed:])
    text = ''.join(pieces)
    text = _AUTH.sub(lambda match: match[1] + match[2] + REDACTED, text)
    text = _CALLBACK.sub(lambda match: match[1] + REDACTED, text)
    text = _USERINFO.sub(lambda match: match[1] + REDACTED + '@', text)
    text = _KNOWN_KEY.sub(REDACTED, text)
    return _PRIVATE_KEY.sub(REDACTED, text)
