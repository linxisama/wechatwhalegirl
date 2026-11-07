"""微信控制台的后台身份标识：界面用它判断端口上跑的是不是本程序。"""
import os
from pathlib import Path

APP_VERSION = '1.3.111'
APP_ID = 'WhaleGirlWeChat'
DEFAULT_PORT = 8091


def _console_port(raw):
    """WECHAT_CONSOLE_PORT 可以换端口；写错的值回落到默认端口，绝不让后台起不来。"""
    try:
        value = int(str(raw).strip())
    except (TypeError, ValueError):
        return DEFAULT_PORT
    return value if 0 < value <= 65535 else DEFAULT_PORT


PORT = _console_port(os.environ.get('WECHAT_CONSOLE_PORT'))


def backend_identity_error(identity, root):
    if not isinstance(identity, dict) or identity.get('app') != APP_ID:
        return f'{PORT} 端口上不是当前版本的鲸鱼娘微信后台。请关闭旧后台后重新打开控制台。'
    if identity.get('version') != APP_VERSION:
        return f'界面为 {APP_VERSION}，后台版本不一致。请关闭旧控制台和旧后台后重新打开。'
    try:
        matches = bool(identity.get('data_root')) and Path(identity['data_root']).resolve() == Path(root).resolve()
    except (ValueError, TypeError, OSError):
        matches = False
    if not matches:
        return '后台使用了其他目录的配置。请关闭旧后台，再从当前安装目录启动。'
    return ''
