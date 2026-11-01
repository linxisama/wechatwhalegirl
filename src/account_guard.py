"""账号守卫：把「机器人账号」和「微信当前登录的账号」讲清楚，并支持一键改绑。

为什么单独成文件：这台机器上最常见的"能收不能发"就是微信换号了——配置里还写着
鲸鱼娘，实际登录的是另一个号。发送通道每次都会在核验阶段被拦（这是设计如此：不核验
就发等于用别人的身份发消息），可用户看到的只是"发送失败"，没有任何可操作入口。
所以这里只做三件事：判断是否一致、给出能照做的提示、算出改绑目标。全部纯函数，
不碰微信、不碰配置，方便单测。
"""
from __future__ import annotations

import re

SAME = "same"
DIFFERENT = "different"
UNKNOWN = "unknown"
AUTO = "auto"

# 账号目录名形如 wxid_xxx_ab12，wxid 部分是去掉末四位后缀；设置里两种写法都允许。
_SUFFIX = re.compile(r"_[0-9A-Za-z]{4}$")

HINT_SAME = "微信登录的就是机器人账号，收发都走这个号。"
HINT_AUTO = "没有指定机器人账号，程序按本机情况自动判断。"
HINT_UNKNOWN = ("读不到微信当前登录的是哪个账号（多半是没以管理员身份启动）：请用管理员身份启动控制台，"
                 "或到「设置」页手动指定机器人账号。")
HINT_DIFFERENT = ("微信当前登录的是「{logged_in}」，机器人账号是「{configured}」：现在能收消息但发不出去。"
                  "把微信切回机器人账号，或点「改绑到当前登录账号」把机器人改成这个号。")


def wxid_of(account: str) -> str:
    """从账号目录名里取出 wxid；不是目录名就原样返回。"""
    text = str(account or "").strip()
    if not text:
        return ""
    return _SUFFIX.sub("", text)


def same_account(left: str, right: str) -> bool:
    """两个账号写法不同（目录名 / wxid）也算同一个。"""
    one, two = str(left or "").strip(), str(right or "").strip()
    if not one or not two:
        return False
    if one == two:
        return True
    return wxid_of(one) == wxid_of(two)


def display(account: str, nickname: str = "") -> str:
    """界面上显示的名字：优先昵称，其次 wxid，最后原值。"""
    name = str(nickname or "").strip()
    if name:
        return name
    return wxid_of(account) or str(account or "").strip()


def judge(configured: str, logged_in: str, configured_name: str = "",
          logged_in_name: str = "") -> dict:
    """判断机器人账号与微信当前登录账号的关系。

    四种结果：
    - same：一致，可以收发；
    - different：不一致，能收不能发，需要切回或改绑；
    - auto：没指定账号（自动判断），不算异常；
    - unknown：读不到当前登录账号，既不能判错也不能当正常。
    """
    configured_text = str(configured or "").strip()
    logged_text = str(logged_in or "").strip()
    shown_configured = display(configured_text, configured_name)
    shown_logged = display(logged_text, logged_in_name)
    if not logged_text:
        match = UNKNOWN
        hint = HINT_UNKNOWN
    elif not configured_text:
        match = AUTO
        hint = HINT_AUTO + (f"当前登录的是「{shown_logged}」。" if shown_logged else "")
    elif same_account(configured_text, logged_text):
        match = SAME
        hint = HINT_SAME
    else:
        match = DIFFERENT
        hint = HINT_DIFFERENT.format(logged_in=shown_logged or logged_text,
                                     configured=shown_configured or configured_text)
    return {
        "configured": configured_text,
        "logged_in": logged_text,
        "configured_name": shown_configured,
        "logged_in_name": shown_logged,
        "match": match,
        "hint": hint,
        "can_rebind": match == DIFFERENT,
    }


def rebind_target(configured: str, logged_in: str) -> dict:
    """算出"改绑到当前登录账号"这个动作能不能做、改成什么。

    三条拒绝理由都必须明确：读不到账号时不能瞎写配置；已经一致时不该做无意义的
    重启（重启一次要重新定位校准窗口十几秒）；只有确实不一致才允许。
    """
    logged_text = str(logged_in or "").strip()
    configured_text = str(configured or "").strip()
    if not logged_text:
        return {"ok": False, "reason": "no_active_account",
                "message": "读不到微信当前登录的账号，没法改绑；请用管理员身份启动控制台后重试。"}
    if same_account(configured_text, logged_text):
        return {"ok": False, "reason": "already_bound",
                "message": "机器人账号已经是当前登录的这个号，不需要改绑。"}
    return {"ok": True, "reason": "", "account": logged_text,
            "message": "已把机器人账号改成当前登录的账号。"}


def mismatch_detail(state: dict) -> str:
    """把账号错位的现场压成一句话，供日志与状态卡片使用。"""
    if not isinstance(state, dict) or not state:
        return ""
    current = display(str(state.get("current") or ""))
    active = display(str(state.get("active") or ""))
    if not current and not active:
        return ""
    return (f"微信当前登录的是「{active or '未知'}」，机器人账号是「{current or '未知'}」："
            "能收消息但发不出去，请切回机器人账号或改绑。")

