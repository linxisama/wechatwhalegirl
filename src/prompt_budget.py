"""系统提示词的分块注入预算。

为什么需要这一层：系统提示词原本是逐段字符串拼接——人设、连续性提示、插件提示、
宿主固定规则、图片规则——每段长度都不可控（联网资讯、手动记忆、排查模板都可能
突然变长）。任意一段失控就会把整条提示词推过模型的上下文上限，后果是整轮调用
失败，或者关键人设被更长的内容挤出模型注意力。

这里把每段当成有独立上限的“块”：先按块封顶，再按总预算**整块**丢弃，最后才硬截断。
丢掉的永远是优先级最低的一整块，而不是随手砍掉半句话——插件提示缺席顶多少一条行为
约束，人设缺席等于换了个机器人，所以 persona 永远不丢，只受它自己的分块上限约束。

纯函数 + 不可变对象：不读文件、不写日志、不联网。宿主只拿 text 发模型、拿
log_line() 写运行日志，因此“未超限时逐字不变”可以用测试钉死。
"""

import re

# 块的固定顺序＝拼进提示词的顺序；改顺序会改变与模型的相处方式，不要随手动
PART_ORDER = ("persona", "continuity", "plugins", "rules", "image")

# 超出总预算时的丢弃顺序：先丢信息密度最低、最可缺的，人设不在名单里
DROP_ORDER = ("plugins", "image", "continuity", "rules")

# 各块默认上限（字）。取值来自 1.3.45 的实测：当前生效的自定义人设 2466 字（内置
# tsundere_whale_girl 1150、default 798）、连续性 96、固定规则 172~180、图片规则 299。
# 人设上限必须高于现状：人设尾部的身份锁是提示词里最不能丢的一段，截断等于削弱人设，
# 而 persona 从不参与丢块，所以只给它一个"够现状再留余量"的硬上限；插件块给到总预算
# 的一半，因为联网资讯与手动记忆是真的会长；其余块按实测的 3~4 倍取值。
DEFAULT_PART_CAPS = {
    "persona": 3000,
    "continuity": 400,
    "plugins": 3000,
    "rules": 600,
    "image": 600,
}

# 总预算（字）：6000 与历史消息预算 contextChars 同档，两段加起来不顶爆常见模型的上下文
DEFAULT_MAX_CHARS = 6000

# 表情包段落专用上限（字）。用户点名的缺陷：人设先占 3000 字，剩下的 plugins 额度
# 再被一堆表情包描述挤掉，最终只有 800 字到得了模型，模型只看得到最新五六个表情。
# 所以给这段文案一个**独立**硬预算：不跟联网资讯/手动记忆抢同一个 plugins 额度，
# 超了就在段落内部按相关度先砍最不相关的条目，而不是整段被宿主丢块。
STICKER_SECTION_CAP = 800
# 单条描述上限（字，含省略号）：原 80 字 × 5 条就 600 字，800 的预算照样会爆
STICKER_MEANING_CHARS = 40
# 一轮最多给模型看几条说明：真正可能被选中的只有相关度最高的那几条
STICKER_MAX_ITEMS = 3
# 段落砍掉几条记在这句话里。为什么靠文案回读而不是加参数：段落由 image_library 插件
# 生成，插件只能通过 extra_system 交出结果（dispatch 只转发固定的几个键），
# 所以"砍了几条"必须跟着文案一起走，宿主才说得出来。
STICKER_CUT_NOTE = "（表情包说明超预算，已按相关度省略 {count} 条）"
_STICKER_CUT = re.compile(r"已按相关度省略 (\d+) 条")

TRUNCATE_NOTE = "…（{name} 超长已截断 {count} 字）"
HARD_CUT_NOTE = "（本轮注入超预算，已硬截断）"
JOIN = "\n"


def _config_get(config, key, default):
    """同时认 ConfigManager（点号 get）和普通 dict，方便测试直接传 dict。"""
    if config is None:
        return default
    if isinstance(config, dict):
        # dict 的 get 也是 callable，必须先走分支，否则点号键永远查不到（测试大多传 dict）
        value = config
        for part in key.split("."):
            if not isinstance(value, dict):
                return default
            value = value.get(part)
        return default if value is None else value
    getter = getattr(config, "get", None)
    if callable(getter):
        try:
            value = getter(key, default)
        except Exception:
            return default
        return default if value is None else value
    return default


def _positive_int(value):
    """配置是用户手改的 YAML：写坏的数不许让整轮对话失败，一律退回默认。"""
    if isinstance(value, bool):
        return 0
    try:
        number = int(value)
    except (TypeError, ValueError):
        return 0
    return number if number > 0 else 0


def part_caps(config=None):
    """每块上限：prompt_budget.partCaps.<name> 覆盖，未配置的块用默认值。"""
    caps = dict(DEFAULT_PART_CAPS)
    overrides = _config_get(config, "prompt_budget.partCaps", {})
    nested = overrides if isinstance(overrides, dict) else {}
    for name in PART_ORDER:
        raw = nested.get(name)
        if raw is None:
            raw = _config_get(config, "prompt_budget.partCaps." + name, None)
        cap = _positive_int(raw)
        if cap:
            caps[name] = cap
    return caps


def max_chars(config=None):
    """总预算：prompt_budget.maxChars 覆盖，非法值退回默认。"""
    limit = _positive_int(_config_get(config, "prompt_budget.maxChars", None))
    return limit or DEFAULT_MAX_CHARS


def sticker_cap(config=None):
    """表情包段落专用上限：prompt_budget.stickerCap 覆盖，非法值退回默认。"""
    limit = _positive_int(_config_get(config, "prompt_budget.stickerCap", None))
    return limit or STICKER_SECTION_CAP


def sticker_meaning_chars(config=None):
    """单条表情描述上限：prompt_budget.stickerMeaningChars 覆盖，非法值退回默认。"""
    limit = _positive_int(_config_get(config, "prompt_budget.stickerMeaningChars", None))
    return limit or STICKER_MEANING_CHARS


def sticker_max_items(config=None):
    """一轮最多几条表情说明：prompt_budget.stickerMaxItems 覆盖，非法值退回默认。"""
    limit = _positive_int(_config_get(config, "prompt_budget.stickerMaxItems", None))
    return limit or STICKER_MAX_ITEMS


def clip_note(text, limit):
    """单行说明截断：超限补省略号，且省略号算在上限里（调用方给的数就是最终上限）。"""
    value = str(text or "").strip()
    limit = _positive_int(limit)
    if not limit or len(value) <= limit:
        return value
    return value[:max(0, limit - 1)] + "…"


def sticker_cut_count(text):
    """从表情包段落文案里读回它自己报告的省略条数（0 表示没砍）。"""
    found = _STICKER_CUT.search(str(text or ""))
    return int(found.group(1)) if found else 0


class _Rendered:
    """一次渲染的完整结果；宿主只需要 text 与那行日志。"""

    __slots__ = ("text", "total", "sizes", "dropped", "truncated", "hard_cut",
                 "plugin_parts", "sticker_cut")

    def __init__(self, text, total, sizes, dropped, truncated, hard_cut, plugin_parts,
                 sticker_cut=0):
        self.text = text
        self.total = total
        self.sizes = sizes
        self.dropped = dropped
        self.truncated = truncated
        self.hard_cut = hard_cut
        self.plugin_parts = plugin_parts
        self.sticker_cut = sticker_cut


class PromptPlan:
    """一次系统提示词的注入方案：原始块 + 解析后的上限，渲染结果在构造时算好。

    不可变：with_block 返回新方案，宿主在图片识别前后各算一次也不会互相污染。
    """

    def __init__(self, blocks, plugin_parts=(), caps=None, limit=None):
        self._raw = {str(k): str(v or "") for k, v in dict(blocks).items()}
        self._parts = tuple((str(pid), str(text or "")) for pid, text in tuple(plugin_parts or ())
                            if str(text or ""))
        self._caps = dict(caps or DEFAULT_PART_CAPS)
        self._limit = int(limit or DEFAULT_MAX_CHARS)
        self._rendered = self._render()

    def with_block(self, name, text):
        """补一块（例如图片规则要等识别结果出来才能定），返回新方案。"""
        blocks = dict(self._raw)
        blocks[str(name)] = str(text or "")
        return PromptPlan(blocks, self._parts, self._caps, self._limit)

    def _render(self):
        caps, limit = self._caps, self._limit
        kept = []
        truncated = {}
        for name in PART_ORDER:
            text = self._raw.get(name, "")
            if not text:
                continue
            cap = caps.get(name, len(text))
            if len(text) > cap:
                cut = len(text) - cap
                truncated[name] = cut
                text = text[:cap] + TRUNCATE_NOTE.format(name=name, count=cut)
            kept.append([name, text])
        sizes = {name: len(text) for name, text in kept}
        dropped = []

        def total():
            return sum(len(text) for _name, text in kept) + (len(kept) - 1) * len(JOIN)

        for name in DROP_ORDER:
            if total() <= limit:
                break
            for index, (kept_name, _text) in enumerate(kept):
                if kept_name == name:
                    del kept[index]
                    dropped.append(name)
                    sizes[name] = 0
                    break

        text = JOIN.join(item[1] for item in kept)
        hard_cut = len(text) > limit
        if hard_cut:
            # 全丢完还超预算，说明单块上限本身配大了；这里只保证不再超，被砍掉的部分靠标记说明
            text = text[:max(0, limit - len(HARD_CUT_NOTE))] + HARD_CUT_NOTE
        # 表情包段落自己报告砍了几条；宿主据此在日志里说清楚，不用猜
        sticker_cut = 0
        for pid, part in self._parts:
            if pid == "image_library":
                sticker_cut += sticker_cut_count(part)
        return _Rendered(text, len(text), sizes, dropped, truncated, hard_cut,
                         tuple((pid, len(part)) for pid, part in self._parts), sticker_cut)

    @property
    def text(self):
        return self._rendered.text

    @property
    def total(self):
        return self._rendered.total

    @property
    def sizes(self):
        return dict(self._rendered.sizes)

    @property
    def dropped(self):
        return tuple(self._rendered.dropped)

    @property
    def truncated(self):
        return dict(self._rendered.truncated)

    @property
    def hard_cut(self):
        return self._rendered.hard_cut

    @property
    def sticker_cut(self):
        """本轮表情包段落按相关度砍掉的条数（0 = 没砍）。"""
        return self._rendered.sticker_cut

    def log_line(self):
        """一行可诊断的注入构成：谁占了多少、丢了谁、哪个插件贡献多少。

        丢块时仍然打印各插件字数：正因为要判断“该给哪个插件封顶”才需要这行日志。
        """
        rendered = self._rendered
        parts = " ".join(f"{name}={rendered.sizes.get(name, 0)}" for name in PART_ORDER)
        line = f"注入构成：总 {rendered.total} 字（{parts}）"
        if rendered.dropped:
            line += "；丢块：" + ",".join(rendered.dropped)
        if rendered.sticker_cut:
            line += f"；表情包段落砍掉 {rendered.sticker_cut} 条"
        if rendered.plugin_parts:
            line += "；插件归因：" + ",".join(f"{pid}={size}" for pid, size in rendered.plugin_parts)
        return line


def compose_prompt(persona="", continuity="", plugins="", rules="", image="",
                   plugin_parts=(), config=None):
    """按固定顺序组装系统提示词并施加预算。

    plugins 传 run_on_pre_ai 拼好的原字符串（它自带一个前导换行），这样未超限时
    输出与逐字拼接完全一致；只有超限时才按块上限/总预算动手。
    """
    blocks = {"persona": persona, "continuity": continuity, "plugins": plugins,
              "rules": rules, "image": image}
    return PromptPlan(blocks, plugin_parts=plugin_parts,
                      caps=part_caps(config), limit=max_chars(config))
