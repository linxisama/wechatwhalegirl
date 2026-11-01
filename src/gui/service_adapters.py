"""前端用的同步 HTTP 适配器：把后端接口包成「服务对象」给对话框调。

为什么要有这一层：
- 更新对话框、沙盒面板、DSH 面板都是按「传入一个 service」写的，方便单测注入替身；
- 正式运行时 GUI 与后端是**两个进程**，只能走 HTTP；
- 这里统一负责：同步请求、错误归一化（永远回字典/字符串，不抛异常）。

约定：所有方法都不抛异常，失败时返回 {'ok': False, 'reason': ..., 'message': ...}。
"""
from __future__ import annotations


def _text(value, default: str = '') -> str:
    try:
        return str(value)
    except Exception:
        return default


class _Base:
    def __init__(self, get, post, delete=None):
        self._get = get
        self._post = post
        self._delete = delete

    # 子类只声明 route 表：属性名 -> (HTTP 方法, 路径, 默认 body)
    routes: dict = {}

    def _call(self, name: str, payload: dict | None = None):
        spec = self.routes.get(name)
        if not spec:
            return {'ok': False, 'reason': 'unsupported',
                    'message': f'这个版本的控制台不支持 {name} 操作，请升级后再试'}
        method, path = spec[0], spec[1]
        if '{id}' in path:
            pack_id = str((payload or {}).get('id') or '').strip()
            if not pack_id:
                return {'ok': False, 'reason': 'bad_request', 'message': '缺少插件包标识'}
            path = path.replace('{id}', pack_id)
        try:
            if method == 'GET':
                return self._get(path)
            if method == 'DELETE':
                return self._delete(path) if self._delete else {
                    'ok': False, 'reason': 'unsupported', 'message': '当前环境不支持删除操作'}
            return self._post(path, payload or {})
        except Exception as error:  # 适配层永远不把异常抛给界面
            return {'ok': False, 'reason': 'internal',
                    'message': f'与后台通信失败（{type(error).__name__}），请重试'}

    @staticmethod
    def _unwrap(result, key: str = 'status'):
        """把 {ok:True,status:{...}} 拆成 status 本体；失败时给出统一错误形状。"""
        if not isinstance(result, dict):
            return {'ok': False, 'reason': 'internal', 'message': '后台返回了无法识别的结果'}
        if result.get('ok') is True and isinstance(result.get(key), dict):
            value = dict(result[key])
            value.setdefault('ok', True)
            return value
        if result.get('ok') is True:
            return {'ok': True}
        return {'ok': False,
                'reason': _text(result.get('reason') or 'internal'),
                'message': _text(result.get('message') or '操作失败，请重试')}


class UpdateHttpService(_Base):
    """更新对话框用的服务：状态 / 检查 / 下载 / 安装 / 跳过。"""

    routes = {
        'status': ('GET', 'update/status'),
        'check': ('POST', 'update/check'),
        'download': ('POST', 'update/download'),
        'install': ('POST', 'update/install'),
        'skip': ('POST', 'update/skip'),
        'unskip': ('POST', 'update/skip'),
        'settings': ('POST', 'update/settings'),
    }

    def status(self):
        return self._unwrap(self._call('status'))

    def check(self, force: bool = True):
        return self._unwrap(self._call('check', {'force': bool(force)}))

    def download(self):
        return self._unwrap(self._call('download'))

    def install(self):
        return self._unwrap(self._call('install'))

    def skip(self, version: str):
        return self._unwrap(self._call('skip', {'version': _text(version)}))

    def unskip(self):
        return self._unwrap(self._call('unskip', {'clear': True}))

    def configure(self, feed_url=None, auto_check=None, interval_hours=None):
        body = {}
        if feed_url is not None:
            body['feed_url'] = _text(feed_url)
        if auto_check is not None:
            body['auto_check'] = bool(auto_check)
        if interval_hours is not None:
            body['interval_hours'] = int(interval_hours)
        return self._unwrap(self._call('settings', body))


class SandboxProvisionHttpService(_Base):
    """沙盒面板用的服务：状态 / 启用 / 复核 / 建通道 / 禁用命令。"""

    routes = {
        'status': ('GET', 'sandbox/provision/status'),
        'enable': ('POST', 'sandbox/provision/enable'),
        'check': ('POST', 'sandbox/provision/check'),
        'channel': ('POST', 'sandbox/provision/channel'),
        'rollback': ('GET', 'sandbox/provision/rollback'),
    }

    def status(self):
        return self._unwrap(self._call('status'))

    def enable(self):
        return self._unwrap(self._call('enable'))

    def check(self, log_path: str = ''):
        return self._unwrap(self._call('check', {'log_path': _text(log_path)} if log_path else {}))

    def create_channel(self):
        return self._unwrap(self._call('channel'))

    def rollback_text(self) -> str:
        result = self._call('rollback')
        if isinstance(result, dict) and result.get('ok') is True:
            status = result.get('status')
            if isinstance(status, dict):
                text = _text(status.get('text'))
                if text:
                    return text
        message = ''
        if isinstance(result, dict):
            message = _text(result.get('message'))
        return message or '暂时读不到禁用命令，请稍后重试'


class DshConnectHttpService(_Base):
    """DSH 面板用的服务：状态 / 一键修复 / 守护开关。"""

    routes = {
        'status': ('GET', 'dsh/connect'),
        'repair': ('POST', 'dsh/connect/repair'),
        'watch': ('POST', 'dsh/connect/watch'),
    }

    def status(self):
        return self._unwrap(self._call('status'))

    def repair(self):
        return self._unwrap(self._call('repair'))

    def watch(self, enabled: bool = True):
        return self._unwrap(self._call('watch', {'enabled': bool(enabled)}))


class PluginPacksHttpService(_Base):
    """插件包面板用的服务：列表 / 重扫 / 安装 / 移除 / 启停。"""

    routes = {
        'packs': ('GET', 'plugins/packs'),
        'rescan': ('POST', 'plugins/packs/rescan'),
        'install': ('POST', 'plugins/packs/install'),
        'remove': ('DELETE', 'plugins/packs/{id}'),
        'enabled': ('POST', 'plugins/packs/{id}/enabled'),
    }

    def list_packs(self):
        # 后端把扫描结果平铺在同一层：{ok, packs:[...], message, ...}
        result = self._call('packs')
        if isinstance(result, dict) and result.get('ok') is True:
            value = dict(result)
            value.setdefault('packs', [])
            return value
        if isinstance(result, list):
            return {'ok': True, 'packs': result}
        return self._unwrap(result)

    def rescan(self):
        return self._unwrap(self._call('rescan'))

    def install(self, source: str):
        # 后端的两种来源：本机路径（path）或上传的 zip（zip_base64），这里只用路径
        return self._unwrap(self._call('install', {'path': _text(source)}))

    def remove(self, pack_id: str):
        return self._unwrap(self._call('remove', {'id': _text(pack_id)}))

    def set_enabled(self, pack_id: str, enabled: bool):
        return self._unwrap(self._call('enabled', {'id': _text(pack_id), 'enabled': bool(enabled)}))



class AccountGuardHttpService(_Base):
    """账号与登录：机器人账号 vs 微信当前登录账号，以及一键改绑。"""

    routes = {
        'state': ('GET', 'wechat/account-guard'),
        'rebind': ('POST', 'wechat/account-guard/rebind'),
    }

    def _state(self, result):
        if isinstance(result, dict) and result.get('success') is not False:
            value = dict(result)
            value['ok'] = True
            return value
        if isinstance(result, dict):
            return {'ok': False,
                    'reason': _text(result.get('reason') or 'internal'),
                    'message': _text(result.get('message') or '读取账号状态失败')}
        return {'ok': False, 'reason': 'internal', 'message': '后台返回了无法识别的结果'}

    def state(self):
        return self._state(self._call('state'))

    def rebind(self):
        return self._state(self._call('rebind', {'confirm': True}))

__all__ = ['UpdateHttpService', 'SandboxProvisionHttpService', 'DshConnectHttpService',
           'PluginPacksHttpService', 'AccountGuardHttpService']
