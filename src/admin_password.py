"""管理页面与本机接口使用同一密码规则，避免两边允许的输入不同。"""
import re

PASSWORD_RULE = '管理员密码需要 1–128 个字符，仅允许中文汉字、英文字母和数字，不能含空格或特殊符号'
_ALLOWED = re.compile(r'[A-Za-z0-9\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff\U00020000-\U0002fa1f]+')


def password_error(password):
    if not isinstance(password, str) or not 1 <= len(password) <= 128:
        return PASSWORD_RULE
    if not _ALLOWED.fullmatch(password):
        return PASSWORD_RULE
    return ''
