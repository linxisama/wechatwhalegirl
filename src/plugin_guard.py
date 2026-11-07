"""插件权限门（1.3.105）：声明与强制。

为什么要有这一层：
  用户 2026-11-08 的第三条判定标准是「不靠改原版内部实现活着。靠了也能装，
  但必须标出来，把代价写在明面上」。清单里的 permissions 字段过去就是那个
  「明面上」—— 但它只是被解析成一个列表，**从来没有被检查过**。
  插件声明 permissions: [] 和声明 permissions: ["net","fs_write"] 在运行期完全一样。

五项权限：
  net          联网（HTTP 请求）
  fs_read      读本机文件
  fs_write     写本机文件
  exec         起子进程
  wechat_send  绕过宿主闸门直接发微信消息

两档行为（与 1.3.103 的双档清单同一个思路）：
  enforce=False（默认）  只审计：缺声明时记一条 permission_violation 到 runtime，
                         不拦截。理由：44 个内置插件里有 7 个真的联网或读写文件，
                         一刀切会让它们在升级当天全部被隔离。
  enforce=True           真拦截：缺声明直接抛 PermissionDenied，由
                         plugin_dispatch.protected() 记故障并跳过该插件本轮。
"""
from __future__ import annotations

from typing import Iterable, Tuple

# 五项权限的规范名与中文说明（控制台与错误文案都用这张表）
PERMISSIONS: Tuple[Tuple[str, str], ...] = (
    ("net", "联网"),
    ("fs_read", "读本机文件"),
    ("fs_write", "写本机文件"),
    ("exec", "起子进程"),
    ("wechat_send", "直接发微信消息"),
)

PERMISSION_NAMES: Tuple[str, ...] = tuple(name for name, _label in PERMISSIONS)


class PermissionDenied(PermissionError):
    """插件用了没声明的能力。继承 PermissionError，让既有 except 也能接住。"""

    def __init__(self, plugin_id: str, permission: str, message: str = ""):
        self.plugin_id = plugin_id
        self.permission = permission
        label = dict(PERMISSIONS).get(permission, permission)
        super().__init__(message or (
            "插件 " + plugin_id + " 没有声明 " + permission + "（" + label + "）权限，"
            "请在 plugin.json 的 permissions 里写上它"))


def normalize(values: Iterable) -> Tuple[str, ...]:
    """把任意形态的 permissions 归一成去重后的规范名元组。

    认不出来的一律丢弃而不是报错 —— 声明层的宽松校验在 plugin_packs 里做，
    这里只负责「能用的先收下」。
    """
    result = []
    for value in values or ():
        text = str(value).strip().lower()
        if text in PERMISSION_NAMES and text not in result:
            result.append(text)
    return tuple(result)


def unknown(values: Iterable) -> Tuple[str, ...]:
    """声明里不在五项之内的名字；清单校验用它给用户提示。"""
    result = []
    for value in values or ():
        text = str(value).strip().lower()
        if text and text not in PERMISSION_NAMES and text not in result:
            result.append(text)
    return tuple(result)


class PermissionGuard:
    """一个插件一份的权限视图，挂在 runtime 上给插件用。

    插件调 require("net") 才知道自己有没有被授权；宿主自己调 check() 做审计或拦截。
    """

    def __init__(self, plugin_id: str, granted: Iterable, enforce: bool = False,
                 sink=None):
        self.plugin_id = plugin_id
        self.granted = normalize(granted)
        self.enforce = bool(enforce)
        self._sink = sink

    def has(self, permission: str) -> bool:
        return str(permission).strip().lower() in self.granted

    def check(self, permission: str) -> bool:
        """审计：缺声明时记一条，返回是否被授权。不抛。"""
        name = str(permission).strip().lower()
        if name in self.granted:
            return True
        if self._sink is not None:
            try:
                self._sink({"plugin": self.plugin_id, "permission": name,
                            "enforced": self.enforce})
            except Exception:
                pass
        return False

    def require(self, permission: str) -> None:
        """强制：enforce=True 时缺声明直接抛，否则只审计后放行。"""
        if self.check(permission):
            return
        if self.enforce:
            raise PermissionDenied(self.plugin_id, str(permission).strip().lower())

    def as_dict(self) -> dict:
        return {"plugin": self.plugin_id, "granted": list(self.granted),
                "enforce": self.enforce}