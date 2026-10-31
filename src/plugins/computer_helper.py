"""computer_helper.py - 电脑故障排查助手。

为什么要有这个插件：普通聊天模型遇到"蓝屏 0x0000007B"只会泛泛而谈（"建议重装
系统""去售后看看"），而旧项目里攒了一批**可执行**的排查模板（具体命令、错误码、
设置路径）。这里把那些模板搬过来，命中时连同 COMPUTER_HELP_PROMPT 一起塞进
``extra_system``，由聊天模型用当前人设写出最终回复 —— 人设归人设，命令归命令。

两条底线：
- 只是聊天里顺带出现"电脑""内存"这类词**不算**故障，绝不主动插话（群聊里尤其）。
- 需要给命令的场合，宁可给得具体，也不要"泛泛而谈"。
"""

from __future__ import annotations

import re

from . import PluginBase, PluginMeta, FieldSchema

try:
    # 复用「情商与情绪优先」插件的情绪判定，避免两处情绪词表各写一份迟早对不上。
    # 那个插件缺失或被改名时降级为"不判情绪"，只影响共情那一句，不影响排查本身。
    from .mood_guidance import detect_mood as _detect_mood
except Exception:  # pragma: no cover - 只在插件被裁掉时走到
    def _detect_mood(content: str) -> str:
        return ""


# ---------------------------------------------------------------------------
# 技术支持提示词（旧项目 src/message_handler.py 第 208-217 行原文移植）
# 这一段负责"必须给具体命令/错误码/设置路径、分步、不许泛泛而谈"。
# ---------------------------------------------------------------------------
COMPUTER_HELP_PROMPT = (
    "你是“傲娇鲸鱼娘”，同时也是严谨的电脑技术支持。"
    "使用简体中文回答，语气可以稍微傲娇，但必须以解决问题为主。"
    "先结合最近对话和问题描述判断故障，再给出 3 到 6 条按顺序可执行的排查步骤。"
    "你不会查看图片；图片只作为上下文占位，不得声称识别过图片内容。"
    "命令、错误代码、驱动名、设置路径和必要的系统术语可以保留原文。"
    "不要编造用户没有提供的硬件型号或故障原因；信息不足时明确让用户补充哪一项。"
    "结合最近对话，避免重复上下文中已经出现过的回答，并优先补充尚未做过的检查。"
    "答案要具体、准确，控制在 150 到 450 个汉字。"
)

# ---------------------------------------------------------------------------
# 电脑问题识别：分四层，避免普通闲聊被误判成故障求助
# （旧项目 src/message_handler.py 第 218-263 行原文移植）
# 例如"内存怎么这么贵""他唱歌没声音的"都不该触发长篇排查。
# ---------------------------------------------------------------------------
# 1) 强信号：单独出现就按电脑问题处理
COMPUTER_STRONG_TERMS = (
    "蓝屏", "黑屏", "屏幕不亮", "花屏", "死机", "无法开机", "开不了机",
    "进不去系统", "反复重启", "一直重启", "引导失败", "系统损坏",
    "中病毒", "中毒", "c盘满", "c盘红了", "磁盘满", "开机密码",
    "错误代码", "蓝屏代码", "安装失败", "更新失败", "bootmgr",
    "inaccessiblebootdevice",
)
# 2) 症状：还必须再有一个"电脑上下文"或"故障语气"才算电脑问题
COMPUTER_ISSUE_SYMPTOMS = (
    "报错", "电脑卡", "运行慢", "很卡", "卡顿", "卡死", "风扇很响",
    "过热", "温度高", "没有声音", "没声音", "连不上网", "无法联网",
    "网络连不上", "断网", "掉线", "弹窗", "软件打不开", "无法识别",
    "不识别", "识别不到", "点不亮", "不亮", "开机慢", "特别卡",
)
# 3) 电脑 / 硬件上下文词
COMPUTER_COMPONENT_TERMS = (
    "电脑", "笔记本", "台式机", "台式", "主机", "系统", "windows",
    "win10", "win11", "wifi", "wi-fi", "网络", "路由器", "驱动",
    "硬盘", "固态", "机械盘", "显卡", "内存", "主板", "电源",
    "显示器", "屏幕", "键盘", "鼠标", "打印机", "摄像头", "声卡",
    "音箱", "耳机", "软件", "u盘", "usb", "boot", "bios", "防火墙",
    "安全中心", "迈克菲", "麦克菲",
)
# 4) 故障 / 求助语气：必须与上面的"症状"或"上下文"同时出现才生效，
#    因此不能再放"怎么"这种日常词，否则"XX怎么这么贵"也会命中。
COMPUTER_HELP_HINTS = (
    "怎么办", "求助", "求帮忙", "怎么解决", "如何解决", "是什么问题",
    "什么原因", "帮我看看", "帮忙看看", "有没有办法", "坏了",
    "不正常", "没反应", "失败", "异常", "无法", "不能",
    "下不下来", "下载不了", "下不了", "装不上", "安装不了",
    "打不开", "关不了", "关不掉", "用不了", "连不上", "上不了网",
    "进不去", "起不来", "怎么进", "怎么设置", "怎么弄", "怎么搞",
)
# 错误码白名单（旧项目第 4568-4571 行）：这些码出现就够直接给专项排查了
COMPUTER_ERROR_CODES = (
    "0000007b", "c000000f", "00000050", "0000003b",
    "00000116", "00000133",
)
# 高置信度信号：命中就说明"信息已经够给专项排查了"
HIGH_CONFIDENCE_COMPUTER_TERMS = (
    "0x", "蓝屏", "黑屏", "屏幕不亮", "防火墙", "安全中心",
    "连不上网", "无法联网", "网络连不上", "c盘满", "磁盘满",
    "更新失败", "bootmgr", "inaccessiblebootdevice",
)
# 续问信号："照做了还是不行"这类追问（旧项目第 259-263 行）
COMPUTER_CONTINUATION_HINTS = (
    "不管用", "没用", "还是不行", "仍然不行", "没解决", "问题还在",
    "依旧不行", "又出现了", "照做了", "试过了", "怎么办", "怎么弄",
    "然后呢", "接下来呢", "下一步", "继续", "还有呢", "什么原因",
)

# 带着情绪问电脑问题时，先在技术回答前面接一句"人话"
# （旧项目 src/message_handler.py 第 3665-3682 行原文移植）。
COMPUTER_EMPATHY_PREFIXES: dict[str, tuple[str, ...]] = {
    "angry": (
        "先替主人骂一句：这毛病确实气人，骂完咱们动手。",
        "烦就对了，换谁都得烦，本鲸鱼娘陪你一起收拾它。",
        "别急着砸键盘，砸了还得买，按下面顺序来。",
    ),
    "sad": (
        "先别慌，这事有得治，本鲸鱼娘在。",
        "别急，坏了就修，先照这个顺序试。",
    ),
    "tired": (
        "都这个点儿了还在折腾它，辛苦了，能明天弄就明天弄。",
        "看你也累了，本鲸鱼娘把步骤写清楚，照着一步步来就行。",
    ),
}

MOOD_LABELS = {"angry": "正在气头上", "sad": "情绪低落", "tired": "已经很累了"}


def _codes(compact: str, *values: str) -> bool:
    """错误码只在去掉分隔符的 compact 串里查（"0x0000007B"可能写成"0x0000007b"）。"""
    return any(value in compact for value in values)


def _words(text: str, *values: str) -> bool:
    """中文词在 casefold 后的原串里查。"""
    return any(value in text for value in values)


# ---------------------------------------------------------------------------
# 排查模板（旧项目 src/message_handler.py 第 3807-3950 行完整移植）
#
# 结构：(键, 标题, 命中判定, 模板正文)。判定按**从上到下**的顺序取最匹配的，
# 顺序必须和旧项目 `_computer_issue_fallback` 的 if 顺序一致，否则"0x0000007B
# 而且黑屏"这种情况会挑到泛化模板。命令原文一字不改 —— 这是这个插件的价值所在。
# ---------------------------------------------------------------------------
COMPUTER_TEMPLATES: tuple[tuple[str, str, object, str], ...] = (
    (
        "0x0000007B",
        "启动蓝屏 0x0000007B（无法访问系统盘）",
        lambda text, compact: _codes(
            compact, "0x0000007b", "00000007b", "inaccessiblebootdevice"
        ),
        "这是系统启动时无法访问系统盘。先关机断电，拔掉最近接入的U盘、"
        "移动硬盘，台式机重新插紧SATA线和电源线，M.2硬盘重新安装一次。"
        "开机进入BIOS，确认硬盘能被识别；如果原来使用AHCI，就不要切到RAID，"
        "若刚改过SATA模式，先改回原设置。仍无法启动时，用Windows安装U盘进入"
        "修复环境，依次执行bootrec /fixmbr、bootrec /fixboot、"
        "bootrec /rebuildbcd。重要数据先通过PE系统备份。",
    ),
    (
        "0xc000000f",
        "引导记录损坏 0xc000000f / bootmgr 丢失",
        lambda text, compact: _codes(compact, "0xc000000f", "c000000f", "bootmgr"),
        "这通常表示引导记录损坏或找不到启动分区。用Windows安装U盘启动，"
        "进入修复计算机、疑难解答、命令提示符，先运行bcdedit /enum确认分区，"
        "再执行bootrec /rebuildbcd。若提示拒绝访问，执行bootsect /nt60 SYS "
        "/mbr并确认系统分区已标记为活动分区。操作前先备份重要文件。",
    ),
    (
        "0x00000050/0x3B",
        "内存、驱动或系统文件异常 0x00000050 / 0x0000003B",
        lambda text, compact: _codes(
            compact, "0x00000050", "00000050", "0x0000003b", "0000003b"
        ),
        "这类错误常与内存、驱动或系统文件异常有关。先进入安全模式，卸载最近"
        "安装的驱动和杀毒软件；运行Windows内存诊断并检查两根内存条单独启动"
        "是否正常；随后在管理员命令提示符执行sfc /scannow和"
        "DISM /Online /Cleanup-Image /RestoreHealth。仍蓝屏时记录完整代码。",
    ),
    (
        "0x116/0x133",
        "显卡驱动或负载超时 0x00000116 / 0x00000133",
        lambda text, compact: _codes(
            compact, "0x00000116", "00000116", "0x00000133", "00000133"
        ),
        "优先排查显卡驱动和散热。进入安全模式，用DDU彻底卸载显卡驱动，"
        "再从显卡官网安装稳定版驱动；同时检查风扇、散热器和电源供电。"
        "若只在运行游戏或高负载时出现，降低显卡频率并记录温度。",
    ),
    (
        "黑屏-鼠标能动",
        "桌面黑屏但鼠标能动",
        lambda text, compact: (
            _words(text, "黑屏", "屏幕不亮", "显示器无信号")
            and _words(text, "鼠标", "光标")
        ),
        "桌面黑屏但鼠标还能动，通常先排查资源管理器崩溃或显卡驱动异常。"
        "按Ctrl+Alt+Del打开任务管理器，选择运行新任务并输入explorer.exe；"
        "如果仍黑屏，按Win+Ctrl+Shift+B重启显卡驱动。"
        "能进入安全模式时，用DDU卸载显卡驱动，再从显卡官网安装稳定版。"
        "若任务管理器也打不开，再检查显示线、内存和最近安装的更新。",
    ),
    (
        "黑屏",
        "开机黑屏 / 显示器无信号",
        lambda text, compact: _words(text, "黑屏", "屏幕不亮", "显示器无信号"),
        "先确认显示器和主机都通电，显示器输入源选对，重新插紧视频线并换一个"
        "接口或另一台显示器测试。键盘指示灯是否能切换、风扇是否转动，可判断"
        "主机是否仍在运行。若只是开机后黑屏，断开外设并重新插紧内存和显卡，"
        "清理内存金手指后再开机；仍无显示就进入安全模式回退显卡驱动，"
        "最后再用另一块硬盘或启动盘判断系统与硬件问题。",
    ),
    (
        "防火墙-装不上",
        "防火墙/安全中心：下载或安装失败",
        lambda text, compact: (
            _words(text, "防火墙", "安全中心")
            and _words(text, "下不下来", "下载不了", "下不了", "安装不了", "装不上")
        ),
        "先确认你要装的是系统自带防火墙，还是迈克菲、火绒这类第三方。"
        "系统防火墙是Windows组件，不需要单独下载；安全中心入口丢失时，"
        "可在管理员终端执行Get-AppxPackage Microsoft.SecHealthUI -AllUsers "
        "| Reset-AppxPackage重新注册。若是第三方或浏览器下载失败，"
        "先关闭代理和VPN，把DNS改为223.5.5.5与119.29.29.29，"
        "再执行ipconfig /flushdns、netsh winsock reset并重启。"
        "迈克菲预装版应从华硕官网或MyASUS重新安装，不要从来路不明的网站下载。",
    ),
    (
        "防火墙-关不掉",
        "防火墙/安全中心：开关变灰、关不掉",
        lambda text, compact: (
            _words(text, "防火墙", "安全中心")
            and _words(text, "关不了", "关不掉", "无法关闭")
        ),
        "先在Windows安全中心、防火墙和网络保护里检查当前网络配置文件是否由"
        "组织或安全软件接管。若开关变灰，打开组策略编辑器，检查计算机配置、"
        "管理模板、网络、网络连接、Windows Defender防火墙中的策略；"
        "同时卸载迈克菲或其他安全软件残留，并确认基础筛选引擎服务没有被禁用。",
    ),
    (
        "防火墙",
        "防火墙/安全中心：常规排查",
        lambda text, compact: _words(text, "防火墙", "安全中心"),
        "先打开Windows安全中心，进入防火墙和网络保护，分别检查专用网络、"
        "公用网络和域网络的开关。若安全中心打不开，在管理员终端执行"
        "Get-AppxPackage Microsoft.SecHealthUI -AllUsers | Reset-AppxPackage；"
        "若某个程序被拦截，在允许应用通过防火墙中勾选对应程序，"
        "不要直接关闭整个防火墙。把具体是下载失败、打不开还是拦截程序的提示补上，"
        "本鲸鱼娘可以继续给对应命令。",
    ),
    (
        "更新失败",
        "Windows 更新失败（含 0x800 系列）",
        lambda text, compact: _words(compact, "更新失败", "0x800"),
        "先执行Windows更新疑难解答，再以管理员身份运行命令提示符，依次执行"
        "net stop wuauserv、net stop bits、net stop cryptsvc，重命名"
        "SoftwareDistribution和catroot2文件夹，最后重启相关服务并重新更新。"
        "若仍失败，使用Media Creation Tool进行就地升级修复。",
    ),
    (
        "联网失败",
        "连不上网 / DNS 异常",
        lambda text, compact: _words(text, "连不上网", "无法联网", "dns"),
        "先确认网线和路由器端口正常，再在管理员命令提示符执行"
        "ipconfig /release、ipconfig /renew、ipconfig /flushdns、"
        "netsh winsock reset、netsh int ip reset，然后重启电脑。"
        "能连路由器但不能上网时，把DNS临时改为223.5.5.5和119.29.29.29；"
        "仍失败就重新安装网卡驱动。",
    ),
    (
        "无声音",
        "没有声音",
        lambda text, compact: _words(text, "没声音", "没有声音"),
        "先确认播放设备和默认输出设备没有选错，再检查耳机或音箱接口。"
        "打开设备管理器，卸载声音控制器后重启自动重装；"
        "同时确认Windows Audio和Windows Audio Endpoint Builder服务正在运行。"
        "若是更新后出现，回退声卡驱动或安装主板、笔记本官网驱动。",
    ),
    (
        "C盘满",
        "C 盘满 / 磁盘满",
        lambda text, compact: _words(text, "c盘满", "磁盘满"),
        "先打开设置、系统、存储查看占用来源，清理Windows更新缓存、临时文件"
        "和休眠文件。管理员命令提示符可执行cleanmgr和"
        "powercfg /h off关闭休眠释放空间。不要直接删除系统目录；"
        "先迁移桌面、下载和微信文件到其他磁盘。",
    ),
    (
        "通用兜底",
        "信息不足时的通用排查顺序",
        lambda text, compact: bool(text.strip()),
        "主人描述的问题我收到了。先记录故障出现前最后一次系统更新、"
        "驱动安装或硬件变动，再重启并进入安全模式观察是否复现。"
        "如果开机就有异常，先检查电源、显示线和内存；如果是进入桌面后出现，"
        "优先从任务管理器、事件查看器和显卡或网卡驱动开始排查。"
        "把完整报错代码或设备型号补上，本鲸鱼娘就能给出更具体的命令。",
    ),
    (
        "通用追问",
        "信息不足时的追问（兜底）",
        lambda text, compact: not text.strip(),
        "主人把故障现象、报错代码和出现时间再说具体一点，"
        "本鲸鱼娘会结合刚才聊到的内容继续排查，不会直接让你重装系统。",
    ),
)


def looks_like_computer_issue(content: str) -> bool:
    """判断文本是否属于需要技术支持的电脑问题。

    判定规则（旧项目 src/message_handler.py 第 4554-4588 行原文移植，
    宁可少回一条闲聊，也不要答非所问地刷屏）：
      1) 0x 错误码 / 白名单错误码 → 直接命中；
      2) 强信号词 → 直接命中；
      3) 故障症状 + （电脑上下文 或 求助语气）→ 命中；
      4) 电脑上下文 + 明确的故障/求助语气 → 命中。
    单纯出现"内存""电脑""怎么"这类词不算电脑问题。
    """
    text = str(content or "").casefold()
    if not text:
        return False
    compact = re.sub(r"[\s|｜:：._-]+", "", text)
    if re.search(r"0x[0-9a-f]{8}", compact) or any(
        code in compact for code in COMPUTER_ERROR_CODES
    ):
        return True
    if any(term in text for term in COMPUTER_STRONG_TERMS):
        return True

    has_symptom = any(term in text for term in COMPUTER_ISSUE_SYMPTOMS)
    has_context = any(term in text for term in COMPUTER_COMPONENT_TERMS)
    has_help = any(term in text for term in COMPUTER_HELP_HINTS)
    if has_symptom and (has_context or has_help):
        return True
    return bool(has_context and has_help)


def pick_templates(content: str, limit: int) -> list[tuple[str, str, str]]:
    """挑出最匹配的排查模板，返回 [(键, 标题, 正文), ...]。

    ``limit`` 是最多注入几个：默认 1 —— 模板本身很长，多塞几段会把模型
    真正要写的回答挤掉，而且会互相矛盾（"先关防火墙"和"别关防火墙"）。
    """
    text = str(content or "").casefold()
    compact = re.sub(r"[\s|｜:：._-]+", "", text)
    found: list[tuple[str, str, str]] = []
    for key, title, predicate, body in COMPUTER_TEMPLATES:
        if len(found) >= max(1, limit):
            break
        try:
            if predicate(text, compact):
                found.append((key, title, body))
        except Exception:
            continue
    return found


def empathy_requirement(content: str) -> str:
    """带着情绪问电脑问题时，要求"先接住情绪，再给命令"。

    旧项目是直接把这句前缀拼在本地兜底答案前面；这里改成给模型的**要求** ——
    回复由聊天模型按当前人设写，硬拼一句"本鲸鱼娘"会在换人设后穿帮。
    """
    mood = _detect_mood(content)
    pool = COMPUTER_EMPATHY_PREFIXES.get(mood)
    if pool:
        return (
            "【先接住情绪，再给命令】这句话是带着情绪来求助的（对方目前"
            + MOOD_LABELS.get(mood, "状态不好")
            + "），第一句必须先用人话接住情绪，第二句再进入排查；"
            "可以照抄下面任一句，也可以用自己的话说："
            + " / ".join(pool)
            + "。不要只甩一张命令清单，也不要为了共情把步骤省掉。"
        )
    return (
        "【先接住情绪，再给命令】对方是着急来求助的：开头先用半句稳住情绪"
        "（例如“先别急，这个有得治”），再给命令。不要只甩一张命令清单。"
    )


class ComputerHelper(PluginBase):
    META = PluginMeta(
        id="computer_helper",
        name="电脑故障排查助手",
        category="utility",
        description=(
            "识别蓝屏/黑屏/连不上网/没声音/更新失败等电脑故障求助，把技术支持提示词"
            "和最匹配的可执行排查模板注入本轮系统提示词，由聊天模型用当前人设写出"
            "带具体命令、错误码和设置路径的回复。群里只在被 @ 或叫到昵称时才介入。"
        ),
        enabled_by_default=True,
        config_schema={
            "enabled": FieldSchema(type="bool", label="启用"),
            "maxTemplates": FieldSchema(
                type="int", label="最多注入几个排查模板", min=1, max=4,
                description="默认 1。模板很长且可能互相矛盾，一次只给最匹配的那一个最稳。",
            ),
            "includeTemplates": FieldSchema(
                type="bool", label="注入排查模板",
                description="关闭后只注入技术支持提示词，由模型自己组织步骤。",
            ),
        },
        default_settings={"enabled": True, "maxTemplates": 1, "includeTemplates": True},
    )

    # 触发范围与 quick_reply 同一套规则：群聊不请自来地插一段电脑排查，
    # 比不回还招人烦（也更容易被举报）。
    @staticmethod
    def _aliases(runtime) -> list[str]:
        try:
            names = runtime.aliases()
        except Exception:
            return []
        if not isinstance(names, (list, tuple)):
            return []
        return [str(name).strip() for name in names if len(str(name).strip()) >= 2]

    def _addressed(self, message: dict, runtime) -> bool:
        kind = message.get("kind")
        if kind == "private":
            return True
        if kind != "group":
            return False
        if message.get("at"):
            return True
        text = str(message.get("text") or "")
        return any(name in text for name in self._aliases(runtime))

    def _is_continuation(self, text: str, conversation) -> bool:
        """判断是不是"照做了还是不行"这类续问。

        只看续问词会把无关话题也拉成电脑排查（"还是不行"可以在聊任何事），
        所以额外要求**最近两轮里出现过高置信度故障信号**才算续问。
        """
        if not any(hint in text.casefold() for hint in COMPUTER_CONTINUATION_HINTS):
            return False
        if not isinstance(conversation, (list, tuple)):
            return False
        for item in list(conversation)[-2:]:
            content = item.get("content") if isinstance(item, dict) else item
            blob = str(content or "").casefold()
            if not blob:
                continue
            if any(term in blob for term in HIGH_CONFIDENCE_COMPUTER_TERMS):
                return True
        return False

    def _build_extra_system(self, text: str, conversation) -> str:
        parts = [COMPUTER_HELP_PROMPT, empathy_requirement(text)]
        if self.get("includeTemplates", True):
            limit = int(self.get("maxTemplates", 1) or 1)
            templates = pick_templates(text, limit)
            if templates:
                blocks = "\n\n".join(
                    f"模板{index}（{title}）：\n{body}"
                    for index, (_key, title, body) in enumerate(templates, 1)
                )
                parts.append(
                    "【最匹配的排查模板（命令、错误码、设置路径照抄原文，不要改写不要省略）】\n"
                    + blocks
                )
        return "\n".join(parts)

    def on_pre_ai(self, message, conversation, runtime):
        try:
            if not self.get("enabled", True):
                return None
            if not isinstance(message, dict):
                return None
            if not self._addressed(message, runtime):
                return None
            text = str(message.get("text") or "")
            if not looks_like_computer_issue(text) and not self._is_continuation(
                text, conversation
            ):
                return None
            return {"extra_system": self._build_extra_system(text, conversation)}
        except Exception:
            # 钩子不能抛异常：真出错了就按普通聊天处理，并留一笔运行记录。
            try:
                runtime.note("电脑故障排查插件出错，本轮按普通聊天处理")
            except Exception:
                pass
            return None
