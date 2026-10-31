"""只剥除输入框末尾已知的无障碍提示，绝不按包含关系猜会话。"""
import re


def clean_chat_name(value, expected=''):
    text = str(value or '').strip()
    # 真正的群名也可能包含提示文字；精确名称始终优先。
    if expected and text == expected:
        return text
    for pattern in (r'按住\s*Ctrl\s*\+\s*Win\s*使用',
                    r'按住鼠标\s*语音输入文字', r'语音输入文字'):
        match = re.search(pattern + r'\s*$', text, re.I)
        if match and text[:match.start()].strip():
            return text[:match.start()].strip()
    return text


def current_chat(uia, expected=''):
    # 读取原始Name，避免依赖库先剥掉合法群名中的文字。
    field = uia._chat_input()
    return clean_chat_name(field.Name, expected) if field is not None else ''
