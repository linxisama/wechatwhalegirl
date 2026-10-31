"""控制台 GUI 配置的读写与校验。纯逻辑，不依赖 PyQt6。"""
import json
import math
import os
import tempfile
from copy import deepcopy
from pathlib import Path

DEFAULTS = {
    "opacity": 0.70,
    # 银白主题的默认底色（与 wechat_gui.BG 保持一致）
    "bg_color": "#F4F6F9",
    "geometry": {},
    "background_image": "",
    # 首次启动套用内置浅色背景；用户主动清除背景后置 False，不再自动套。
    "default_background": True,
    # 外观改版：blur = 云母片模糊度 0~1；transparency = 透明度 0~1。
    # transparency 为 0 表示完全不透明（看不到桌面），此时窗口按纯色绘制。
    "blur": 0.70,
    "transparency": 0.0,
    # 卡片/面板底色透明度 0~1。以前不在 DEFAULTS 里，只靠"未知字段原样保留"混过去：
    # 一旦值非法（字符串等）就会一路透传到 float() 抛异常，这里补上默认值与校验。
    "panel_transparency": 0.35,
    # 拖窗口四角时保持宽高比（等比例缩放）。默认关，按住 Shift 可临时生效。
    "proportional_resize": False,
}

# 同时保存 transparency 与旧 opacity 两个字段：opacity 是 transparency 的换算值，
# 让还没升级的读取方也能拿到一份合理的外观设置。
CONFIG_VERSION = 2
GEOMETRY_KEYS = ("w", "h", "x", "y")
OPACITY_MIN = 0.3
OPACITY_MAX = 1.0
# 三个 0~1 的比例字段走同一套 clamp（panel_transparency 也在这里校验）
RATIO_KEYS = ("blur", "transparency", "panel_transparency")
# 可读性上限：超过这个值窗口/卡片就"看不清也点不准"了，加载时强制收回。
MAX_TRANSPARENCY = 0.85
MAX_PANEL_TRANSPARENCY = 0.90
_HEX_CHARS = set("0123456789abcdefABCDEF")


def _is_number(value):
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _is_valid_hex_color(value):
    if not isinstance(value, str) or len(value) != 7 or not value.startswith("#"):
        return False
    return all(ch in _HEX_CHARS for ch in value[1:])


def _sanitize_geometry(value):
    if not isinstance(value, dict):
        return {}
    result = {}
    for key in GEOMETRY_KEYS:
        item = value.get(key)
        if isinstance(item, int) and not isinstance(item, bool) and -(2**30)<=item<=2**30:
            if key in ('w','h') and item<=0:
                continue
            result[key] = item
    return result


def clamp_ratio(value, fallback):
    """把 0~1 的比例值规整成 float；非数字或越界一律回退。"""
    if not _is_number(value):
        return fallback
    number = float(value)
    if not math.isfinite(number) or number < 0.0 or number > 1.0:
        return fallback
    return number


def config_version(cfg):
    try:return int(cfg.get('config_version') or 0)
    except (TypeError,ValueError,OverflowError):return 0


def migrate(cfg):
    """旧配置迁移（只在缺少 config_version 标记时做一次）。

    版本 1 只认识 opacity = 窗口不透明度（0.3~1.0，默认 0.70）。版本 2 改成
    transparency = 1 - opacity，并新增 blur。不迁移的话，老用户的窗口会从
    "七成不透明" 突然变成默认的完全透明。

    同时处理主题换色：旧版深色底（#0B1220 / #0c1625 一类的深蓝）在银白主题下
    会把整个窗口压成灰色，因此旧配置里凡是深色底一律换成新的银白默认值。
    """
    if not isinstance(cfg, dict):
        return cfg
    if config_version(cfg) >= CONFIG_VERSION:
        return cfg

    cfg = dict(cfg)
    if "transparency" not in cfg:
        legacy = cfg.get("opacity")
        if _is_number(legacy) and OPACITY_MIN <= float(legacy) <= OPACITY_MAX:
            cfg["transparency"] = round(1.0 - float(legacy), 4)

    color = cfg.get("bg_color")
    if isinstance(color, str) and color.strip().lower() in LEGACY_DARK_DEFAULTS:
        cfg["bg_color"] = DEFAULTS["bg_color"]
    return cfg


# 只替换旧版**默认**的深蓝底色（用户没改过就是它）。用户自己挑的深色不动 ——
# 换主题不该把人家特意选的颜色抹掉。
LEGACY_DARK_DEFAULTS = frozenset({
    "#0b1220",  # 旧版 BG 默认值
    "#0c1625",  # 旧版输入框/标签底色
    "#131f31",  # 旧版卡片色
})


def sanitize(cfg):
    """按字段校验并回退，返回全新字典（深拷贝）。未知字段原样保留。"""
    clean = deepcopy(DEFAULTS)
    if not isinstance(cfg, dict):
        return clean

    for key, value in cfg.items():
        if key not in DEFAULTS:
            clean[key] = deepcopy(value)

    opacity = cfg.get("opacity")
    if _is_number(opacity) and OPACITY_MIN <= float(opacity) <= OPACITY_MAX:
        clean["opacity"] = float(opacity)

    bg_color = cfg.get("bg_color")
    if _is_valid_hex_color(bg_color):
        clean["bg_color"] = bg_color

    if "geometry" in cfg:
        clean["geometry"] = _sanitize_geometry(cfg.get("geometry"))

    background_image = cfg.get("background_image")
    if isinstance(background_image, str):
        clean["background_image"] = background_image

    # 等比例缩放：拖窗口四角时保持宽高比（按住 Shift 临时生效）。必须是真布尔，
    # 字符串 "false" 之类一律回退默认，避免界面把它当成"开"。
    if isinstance(cfg.get("proportional_resize"), bool):
        clean["proportional_resize"] = cfg["proportional_resize"]

    for key in RATIO_KEYS:
        if key in cfg:
            clean[key] = clamp_ratio(cfg.get(key), DEFAULTS[key])

    # 可读性上限：透明度和面板透明度都能拉到 1.0，但那样窗口几乎看不见
    # （用户实测"拉满后重装也没救"，因为配置在数据目录里、覆盖安装会保留）。
    # 这里在**加载时**就把超限值收回来，坏配置会在下一次启动自愈。
    clean["transparency"] = min(float(clean.get("transparency", 0.0) or 0.0), MAX_TRANSPARENCY)
    # 从**输入**取，不能从 clean 取：clean 是 DEFAULTS 的深拷贝，读到的一直是默认值。
    clean["default_background"] = bool(cfg.get("default_background", True))
    clean["panel_transparency"] = min(float(clean.get("panel_transparency", 0.35) or 0.35),
                                      MAX_PANEL_TRANSPARENCY)

    return clean


DEFAULT_BACKGROUND_NAME = "mio_light.png"


def apply_default_background(config, bundle_dir, data_dir) -> dict:
    """首次启动时把内置浅色背景复制到用户目录并设为背景。

    为什么要复制而不是直接用包内路径：安装目录是只读资源（`sys._MEIPASS` 随版本变化），
    背景一旦指向包内路径，下次覆盖升级就会指向不存在的文件，用户看到的是黑屏。
    用户自己设过背景、或主动清除后写了 `default_background=false`，这里一律不动。
    """
    config = dict(config or {})
    if str(config.get("background_image") or "").strip():
        return config
    if not config.get("default_background", True):
        return config
    source = Path(bundle_dir) / "assets" / "backgrounds" / DEFAULT_BACKGROUND_NAME
    if not source.is_file():
        return config
    target_dir = Path(data_dir) / "backgrounds"
    target = target_dir / DEFAULT_BACKGROUND_NAME
    try:
        target_dir.mkdir(parents=True, exist_ok=True)
        if not target.is_file() or target.stat().st_size != source.stat().st_size:
            target.write_bytes(source.read_bytes())
    except OSError:
        return config
    config["background_image"] = str(target)
    return config


def load_gui_config(path):
    """读取配置；文件缺失、空文件、非法 JSON 或结构损坏都回退默认值，绝不抛异常。"""
    if not path or not isinstance(path, (str, bytes, os.PathLike)):
        return deepcopy(DEFAULTS)
    try:
        with open(path, "r", encoding="utf-8") as handle:
            content = handle.read()
    except (OSError,UnicodeError):
        try:
            return sanitize(migrate(json.loads(Path(str(path)+'.bak').read_text('utf-8'))))
        except (OSError,ValueError):
            return deepcopy(DEFAULTS)
    if not content.strip():
        try:
            return sanitize(migrate(json.loads(Path(str(path)+'.bak').read_text('utf-8'))))
        except (OSError,ValueError):
            return deepcopy(DEFAULTS)
    try:
        data = json.loads(content)
    except ValueError:
        try:
            return sanitize(migrate(json.loads(Path(str(path)+'.bak').read_text('utf-8'))))
        except (OSError,ValueError):
            return deepcopy(DEFAULTS)
    return sanitize(migrate(data))


def save_gui_config(path, cfg):
    """先 sanitize 再原子写盘（临时文件 + os.replace），UTF-8 + LF。

    写盘失败时删除临时文件、保留原文件，并把异常上抛给调用方提示。
    注意：绝不修改调用方传进来的字典 —— 早先的实现会就地写入 config_version 与
    opacity，调用方若传的是模块级 DEFAULTS，就会把全局默认值一起改掉。
    """
    clean = sanitize(migrate(deepcopy(cfg) if isinstance(cfg, dict) else cfg))
    clean["config_version"] = CONFIG_VERSION
    # 同步一份旧字段，兼容尚未升级的读取方
    clean["opacity"] = round(1.0 - float(clean.get("transparency", 0.0)), 4)
    directory = os.path.dirname(os.path.abspath(path)) or os.getcwd()
    os.makedirs(directory, exist_ok=True)
    handle_fd, tmp_path = tempfile.mkstemp(dir=directory, prefix="gui_cfg_")
    previous=None
    try:
        raw=Path(path).read_bytes()
        if isinstance(json.loads(raw),dict):
            previous=raw
    except (OSError,ValueError):
        pass
    try:
        with os.fdopen(handle_fd, "w", encoding="utf-8", newline="\n") as handle:
            json.dump(clean, handle, ensure_ascii=False, indent=2)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp_path, path)
        if previous is not None:
            Path(str(path)+'.bak').write_bytes(previous)
    except BaseException:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise


def ensure_migrated(path):
    """读取并**落盘**迁移结果，返回可用的配置。

    `migrate()` 只在读取时换算，不写盘 —— 于是老用户的窗口会以旧值渲染一整个会话，
    直到他哪天改了外观才顺手写回。首次启动时调用本函数，让迁移立刻生效。
    """
    try:
        raw = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError,TypeError):
        return load_gui_config(path)
    config = load_gui_config(path)
    needs_write = False
    if isinstance(raw, dict):
        needs_write = "transparency" not in raw or config_version(raw) < CONFIG_VERSION
        if str(raw.get("bg_color", "")).strip().lower() in LEGACY_DARK_DEFAULTS:
            needs_write = True
    if needs_write:
        try:
            save_gui_config(path, config)
        except OSError:
            pass
    return config
