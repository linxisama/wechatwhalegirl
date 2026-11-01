"""发送链路自检：往「文件传输助手」发一条带版本号的消息，验证发送真的通。

接收链路可以只读数据库静默验证，发送链路不行：剪贴板、草稿、标题核验、发送键
这几步只要有一处挡下，聊天记录里就不会出现气泡。以前只能让用户找个人发条微信
等回复，现在用当前账号自己的「文件传输助手」就能一次验证，而且绝不碰真人会话。

安全边界（与项目其它发送路径一致）：
- 目标写死「文件传输助手」，调用方无法指定会话（run() 签名里根本没有这个参数）；
- 必须显式 confirm=True，否则一条也不发（防误触，也防脚本误调）；
- 冷却时间内重复请求直接拒绝，连点不会连发两条；
- 只发一条，失败绝不重发、绝不二次点击；
- 正文固定带前缀和版本号，用户在聊天记录里一眼能认出这是自检。
"""
from __future__ import annotations

import time
from typing import Any, Optional

SELFTEST_TARGET = "文件传输助手"
SELFTEST_KIND = "private"
SELFTEST_COOLDOWN = 20.0
SELFTEST_PREFIX = "【发送自检】"
SELFTEST_MAX_CHARS = 120


def build_text(version: str, now: Optional[float] = None) -> str:
    """自检正文：前缀 + 版本号 + 时分秒（不写账号、不写主机名、不写会话名）。"""
    moment = time.time() if now is None else now
    stamp = time.strftime("%H:%M:%S", time.localtime(moment))
    pieces = [SELFTEST_PREFIX + "WhaleGirlWeChat", str(version or "").strip(), stamp]
    # 版本号为空时不留双空格：拼出来的正文要能直接当一条正常消息看。
    text = " ".join(piece for piece in pieces if piece)
    return text[:SELFTEST_MAX_CHARS]


class SendSelfTest:
    """把「发一条自检消息」做成可以反复点、但绝不会连发的对象。

    transport 就是机器人平时回复用的那个通道对象（src/wechat_transports.py），
    自检走同一条路，才能证明「机器人能发出去」而不是「另写了一条能发出去的路」。
    """

    def __init__(self, transport: Any, version: str = "", clock=time.monotonic):
        self.transport = transport
        self.version = version
        self._clock = clock
        self._last_attempt = 0.0  # 0 表示还没试过；时间戳为 0 也当作没试过

    def cooldown_left(self) -> float:
        """还剩多少秒才能再点；0 表示现在可以。"""
        if not self._last_attempt:
            return 0.0
        left = SELFTEST_COOLDOWN - (self._clock() - self._last_attempt)
        return round(left, 1) if left > 0 else 0.0

    def _result(self, ok: bool, reason: str, message: str, text: str = "",
                elapsed: Optional[float] = None) -> dict:
        out = {"ok": bool(ok), "reason": reason, "message": message,
               "target": SELFTEST_TARGET, "text": text}
        if elapsed is not None:
            out["elapsed"] = round(float(elapsed), 2)
        return out

    async def run(self, confirm: bool = False) -> dict:
        """执行一次自检；任何异常都转成 ok=False 的结果，绝不往上抛。"""
        if not confirm:
            return self._result(False, "not_confirmed",
                                "发送自检会真的往「文件传输助手」发一条消息，需要明确确认后才会执行")
        left = self.cooldown_left()
        if left:
            return self._result(False, "too_soon",
                                "刚做过一次自检，请等 %s 秒再试（避免连点发出两条）" % left)
        transport = self.transport
        if transport is None:
            return self._result(False, "unavailable", "当前后端没有发送通道，无法自检")
        if not getattr(transport, "online", False):
            return self._result(False, "offline", "微信未连接：先点「启动机器人」再自检")
        text = build_text(self.version)
        self._last_attempt = self._clock()
        started = time.perf_counter()
        try:
            ok, detail = await transport.send_to(SELFTEST_TARGET, text, SELFTEST_KIND, SELFTEST_TARGET)
        except Exception as error:  # noqa: BLE001 - 自检失败必须变成结果，不能把界面打崩
            elapsed = time.perf_counter() - started
            return self._result(False, "error", "%s: %s" % (type(error).__name__, error), text, elapsed)
        elapsed = time.perf_counter() - started
        detail = str(detail or "").strip()
        if ok:
            return self._result(True, "", detail or "自检消息已发出", text, elapsed)
        return self._result(False, "send_failed", detail or "发送失败（未说明原因）", text, elapsed)
