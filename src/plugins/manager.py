"""PluginManager: loads, stores settings, and dispatches plugin hooks."""
from __future__ import annotations
import json
import logging
import os
import importlib
import importlib.util
import math
import copy
import sys
import re
import inspect
from pathlib import Path
from typing import Any, Dict, List, Optional

from . import PluginBase, PluginMeta, FieldSchema
from ..plugin_health import PluginHealth
from ..plugin_dispatch import PluginDispatch
from ..plugin_policy import RETIRED_PLUGINS, CORE_PLUGINS
from ..plugin_packs import (
    ENV_FROM_PACKS, OPTIONAL_PLUGIN_PACKS, PACK_DIRNAME,
    default_pack_dirs, discover as discover_pack_dirs,
    load_module as load_pack_module,
)

logger = logging.getLogger(__name__)

PLUGIN_MODULES = [
    "reply_preferences",
    "robot_admin", "private_replies", "keyword_trigger", "group_monitor",
    "conversation_memory", "chat_controls", "group_summary", "image_library", "image_understanding",
    "image_ocr", "speech_to_text_wechat", "quick_reply",
    "sleep_mode", "burst_merge",
    "anti_repeat", "reply_dedupe", "keyword_reply", "short_followup",
    "addressee_guard", "sticker_cadence", "reply_guards",
    "mention_gate", "sticker_memory",
    # 机器人本地表情库（对齐 QQ 版）：sticker_admin 是管理窗口的开关，
    # sticker_steal 负责把别人发来的图片/表情收进本地图库。
    "sticker_admin", "sticker_steal",
    # 省 token（移植自 QQ 版）：极简模式裁历史+封顶输出，Token 节流压缩长历史
    "minimal_mode", "token_saver",
    # 群聊主动行为（默认关闭，主动外发需用户显式打开且配 wechat.proactive.enabled）
    "group_ambient",
    # 联网资讯：时间 + 今日要闻 + 网络热梗（只读公开接口，失败不编造）
    "web_context",
    # 联网现查：只在命中"搜一下/最新…"时去公开搜索页取几条结果
    "web_lookup",
    # 回复节奏：开口前随机等一小会儿，别秒回（默认关闭）
    "reply_pace",
    # 群友外号对照（移植自 QQ 版 member-aliases）：命中才加几行提示词，默认关闭
    "member_aliases",
]

def _is_number(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _coerce_numeric(value: Any, expected_type: type) -> Any:
    """拒绝隐式字符串转换；int 字段只接受整数或整数值浮点。"""
    if expected_type is int:
        if isinstance(value, bool):
            raise ValueError("int field cannot accept bool")
        if isinstance(value, int):
            return value
        if isinstance(value, float) and value.is_integer():
            return int(value)
        raise ValueError(f"int field requires int, got {type(value).__name__}")
    if expected_type is float:
        if isinstance(value, bool):
            raise ValueError("float field cannot accept bool")
        if isinstance(value, (int, float)):
            result = float(value)
        else:
            raise ValueError(f"float field requires number, got {type(value).__name__}")
        if math.isnan(result) or math.isinf(result):
            raise ValueError("float field must not be NaN or Infinity")
        return result
    raise ValueError(f"unknown numeric type {expected_type}")


def _validate_field(schema, value, field_name: str, plugin_id: str) -> Any:
    """按 FieldSchema 校验单个字段，非法值抛 ValueError。"""
    ftype = schema.type
    if ftype == "bool":
        if isinstance(value, bool):
            return value
        raise ValueError(f"{plugin_id}.{field_name}: bool field requires bool")
    if ftype in ("int", "float"):
        value = _coerce_numeric(value, int if ftype == "int" else float)
        if schema.min is not None and value < schema.min:
            raise ValueError(f"{plugin_id}.{field_name}: value {value} below min {schema.min}")
        if schema.max is not None and value > schema.max:
            raise ValueError(f"{plugin_id}.{field_name}: value {value} above max {schema.max}")
        return value
    if ftype == "string":
        if not isinstance(value, str):
            raise ValueError(f"{plugin_id}.{field_name}: string field requires str")
        return value
    if ftype == "select":
        if isinstance(value, bool):
            raise ValueError(f"{plugin_id}.{field_name}: select field cannot accept bool")
        allowed = {opt.get("value") for opt in schema.options} | set(schema.values)
        if value in allowed:
            return value
        raise ValueError(f"{plugin_id}.{field_name}: value {value!r} not in allowed select options")
    # 未知 schema 类型属于 schema 自身的错误，此处不拦。
    return value


def _fallback_field_default(schema_field, defaults: Dict[str, Any], field_name: str) -> Any:
    """字段级回退：优先用 default_settings，其次按类型给中性值。"""
    if field_name in defaults:
        return copy.deepcopy(defaults[field_name])
    ftype = schema_field.type
    if ftype == "bool":
        return False
    if ftype == "int":
        return 0
    if ftype == "float":
        return 0.0
    if ftype == "string":
        return ""
    if ftype == "select":
        options = schema_field.options or []
        values = schema_field.values or []
        if options:
            first = options[0]
            return first.get("value") if isinstance(first, dict) else first
        if values:
            return values[0]
        return None
    return None


def _env_flag(name: str) -> bool:
    """环境变量开关：1/true/yes/on 视为打开，其余（含未设置）都算关闭。"""
    try:
        return os.environ.get(name, '').strip().lower() in ('1', 'true', 'yes', 'on')
    except Exception:
        return False


def _host_version() -> str:
    """宿主版本（唯一来源是 src/runtime_identity.py，读不到时按 0.0.0 处理）。"""
    try:
        from ..runtime_identity import APP_VERSION
        return str(APP_VERSION)
    except Exception:
        return '0.0.0'


class PluginManager(PluginDispatch, PluginHealth):
    def __init__(self, data_dir: Path, custom_plugins_dir: Optional[Path] = None,
                 default_overrides=None, pack_dirs: Optional[List[Path]] = None):
        self._default_overrides = copy.deepcopy(default_overrides or {})
        self.data_dir = Path(data_dir)
        self.state_dir = self.data_dir / "plugin_state"
        self.state_dir.mkdir(parents=True, exist_ok=True)
        self._settings_path = self.data_dir / "plugin_settings.json"
        # 一次性数据迁移标记与插件设置同目录，但不塞进 plugin_settings.json：
        # 那个文件被当作纯「插件 id → 设置」映射遍历，混入非插件键会被别人当插件读。
        self._migrations_path = self.data_dir / "plugin_settings.migrations.json"
        self._removed_path = self.data_dir / 'removed_plugins.json'
        self._removed = self._load_removed()
        # 用户自己装的插件清单（与内置 PLUGIN_MODULES 分开记账，
        # 这样"删除"只可能删到自定义插件，内置的删不掉）
        self._custom_path = self.data_dir / "custom_plugins.json"
        # 自定义插件文件落在这里。**不能放 src/plugins/**：打包成 exe 之后
        # 代码在 exe 内部，磁盘上没有 src/plugins 目录，装进去的东西重启就没了。
        # 放在可写的用户数据目录里，装/删/重启都能保持。
        self.custom_plugins_dir = (Path(custom_plugins_dir) if custom_plugins_dir
                                   else self.data_dir / "custom_plugins")
        self.custom_plugins_dir.mkdir(parents=True, exist_ok=True)
        # 插件包：用户手动放进来的独立插件（文件夹 / zip）。默认先看程序旁的
        # plugin-packs，再看数据目录的 plugin_packs；目录不存在时静默跳过。
        self.pack_dirs = self._resolve_pack_dirs(pack_dirs)
        self._pack_report: List[dict] = []
        self._pack_ids = set()
        self._pack_loaded = False
        self._sources: Dict[str, str] = {}
        self._from_packs = _env_flag(ENV_FROM_PACKS)
        self._settings: Dict[str, dict] = {}
        self._plugins: Dict[str, PluginBase] = {}
        self._init_health()
        self._load_settings()
        # 必须在按 schema 校正之前：校正会把缺省值补成新的工厂默认 800，
        # 之后就看不出用户盘上原本存的是旧默认 100 了。
        self._migrate_fast_merge_window()
        self._migrate_single_segment()
        self._load_plugins()
        self._reconcile_settings_with_schemas()

    def custom_plugin_path(self, plugin_id: str) -> Path:
        """自定义插件的文件路径（安装与卸载都用它，保证是同一个地方）。"""
        return self.custom_plugins_dir / f"{plugin_id}.py"

    def _load_removed(self):
        for path in (self._removed_path,self._removed_path.with_suffix('.json.bak')):
            try:
                value=json.loads(path.read_text('utf-8'))
                if not isinstance(value,list) or not all(isinstance(v,str) for v in value):
                    raise ValueError('插件移除清单格式无效')
                return set(value)-CORE_PLUGINS
            except FileNotFoundError:
                continue
            except Exception:
                continue
        # 损坏时保守关闭可移除项，不因文件损坏重新启用用户已移除的能力。
        return set(PLUGIN_MODULES)-CORE_PLUGINS if self._removed_path.exists() else set()

    def set_removed(self,plugin_id,removed=True):
        if plugin_id not in PLUGIN_MODULES or plugin_id not in self._plugins:
            raise ValueError('插件不存在或不是内置插件')
        if plugin_id in CORE_PLUGINS:
            raise ValueError('这是核心组件，不允许删除；可停用或调整设置')
        updated=set(self._removed)
        if removed:updated.add(plugin_id)
        else:updated.discard(plugin_id)
        from ..persistence import atomic_text
        atomic_text(self._removed_path,json.dumps(sorted(updated),ensure_ascii=False),backup=True)
        self._removed=updated

    # ── 自定义插件清单 ────────────────────────────────────────────────────────
    def custom_plugin_ids(self) -> List[str]:
        """已登记的自定义插件标识。清单损坏时返回空列表，不崩。"""
        try:
            raw = json.loads(self._custom_path.read_text("utf-8"))
        except Exception:
            return []
        if isinstance(raw, dict):
            raw = raw.get("plugins")
        if not isinstance(raw, list):
            return []
        return list(dict.fromkeys(item for item in raw if isinstance(item, str)
                                  and re.fullmatch(r'[a-z][a-z0-9_]{1,47}', item)))

    def _save_custom(self, ids: List[str]) -> None:
        payload = {"plugins": sorted(set(ids))}
        # 直接 write_text 会先截断再写：写到一半失败（磁盘满、被占用）就把清单
        # 留成半截 JSON，下次启动 custom_plugin_ids() 解析失败返回 []，用户装的
        # 插件全部从界面消失。本文件其它四处写盘都用 atomic_text，这里同样用。
        from ..persistence import atomic_text
        atomic_text(self._custom_path,
                    json.dumps(payload, ensure_ascii=False, indent=2) + "\n", backup=True)

    def register_custom(self, plugin_id: str) -> None:
        """把插件登记为自定义插件（下次启动也会加载）。"""
        if plugin_id in RETIRED_PLUGINS:
            raise ValueError('此插件已因主动插话或刷屏风险移除')
        ids = self.custom_plugin_ids()
        if plugin_id not in ids:
            ids.append(plugin_id)
            self._save_custom(ids)

    def unregister_custom(self, plugin_id: str) -> bool:
        """从自定义清单移除；返回是否真的移除了（内置插件返回 False）。"""
        ids = self.custom_plugin_ids()
        if plugin_id not in ids:
            return False
        ids = [item for item in ids if item != plugin_id]
        self._save_custom(ids)
        if self._faults.pop(plugin_id, None):
            self._save_faults()
        return True

    def reload(self) -> None:
        """重新读取设置与插件（装/卸插件后调用，界面无需重启）。"""
        self._settings = {}
        self._plugins = {}
        self._plugin_ids = {}
        self._pack_report = []
        self._pack_ids = set()
        self._pack_loaded = False
        self._sources = {}
        self._from_packs = _env_flag(ENV_FROM_PACKS)
        self._removed = self._load_removed()
        self._load_settings()
        self._load_plugins()
        self._reconcile_settings_with_schemas()

    def _load_settings(self):
        """读取设置文件；顶层不是 dict 时整体回退为空，不崩溃。"""
        try:
            raw = json.loads(self._settings_path.read_text("utf-8"))
            if not isinstance(raw,dict):
                raise ValueError('插件设置根节点无效')
            self._settings = raw
        except Exception:
            try:
                raw=json.loads(self._settings_path.with_suffix('.json.bak').read_text('utf-8'))
                self._settings=raw if isinstance(raw,dict) else {}
                self._settings_recovered=True
            except Exception:
                self._settings = {}

    def _reconcile_settings_with_schemas(self):
        """插件加载后按各自 schema 校正已存值：非法字段回退默认，未知字段忽略。"""
        for pid, plugin in self._plugins.items():
            if self.is_quarantined(pid):
                continue
            with self.protected(plugin, 'settings'):
                self._reconcile_plugin(pid, plugin)

    def _reconcile_plugin(self, pid, plugin):
        meta = plugin.META
        schema = meta.config_schema or {}
        defaults = meta.default_settings or {}
        current = self._settings.get(pid)
        if not isinstance(current, dict):
            current = {}
        merged = copy.deepcopy(defaults)
        for key, value in current.items():
            if key not in schema:
                continue
            try:
                merged[key] = _validate_field(schema[key], value, key, pid)
            except Exception:
                merged[key] = _fallback_field_default(schema[key], defaults, key)
        self._settings[pid] = merged

    def _save_settings(self):
        """原子写盘：先写临时文件再替换，失败时不留半截文件。"""
        payload = json.dumps(self._settings, ensure_ascii=False, indent=2)
        from ..persistence import atomic_text
        atomic_text(self._settings_path,payload,backup=not getattr(self,'_settings_recovered',False))
        self._settings_recovered=False

    def _migrate_fast_merge_window(self):
        """一次性把「旧工厂默认 100ms」的快速模式合并窗口迁到 800ms。

        为什么必须迁移：1.3.51 及更早的安装已经把 mergeMs=100 显式写进了
        plugin_settings.json，只改 default_settings 对已有安装完全无效，
        而 100ms 在真机上几乎命不中，连发合并等于没生效。
        只认「值等于旧默认 100」，用户自己改成 300/500 的一律不动。
        标记单独放 plugin_settings.migrations.json，不往 plugin_settings.json
        里塞非插件键（那个文件被当作「插件 id → 设置」映射遍历）。
        失败一律降级为不迁移：读标记失败当没迁移；写设置或写标记失败只记
        warning，内存里已改的 800 不撤回（避免半迁移），同进程内不再重试。
        """
        if getattr(self, '_fast_merge_migration_done', False):
            return
        self._fast_merge_migration_done = True
        marker = 'fast_merge_800'
        migrations = {}
        try:
            raw = json.loads(self._migrations_path.read_text('utf-8'))
            if isinstance(raw, dict):
                migrations = raw
        except FileNotFoundError:
            pass
        except Exception as exc:
            logger.warning('读取插件迁移标记失败，按未迁移处理：%s', exc)
        if migrations.get(marker) is True:
            return
        prefs = self._settings.get('reply_preferences')
        if not isinstance(prefs, dict):
            return
        value = prefs.get('mergeMs')
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            return
        if value != 100:
            return
        prefs['mergeMs'] = 800
        try:
            self._save_settings()
        except Exception as exc:
            # 设置没落盘就不写标记：下次启动还能重来，不退化成「标记已迁移、盘上仍是 100」。
            logger.warning('连发合并窗口迁移保存设置失败，保留内存值，下次启动再试：%s', exc)
            return
        migrations[marker] = True
        try:
            from ..persistence import atomic_text
            atomic_text(self._migrations_path,
                        json.dumps(migrations, ensure_ascii=False, indent=2), backup=False)
        except Exception as exc:
            logger.warning('写插件迁移标记失败，下次启动会重新判定：%s', exc)

    def _migrate_single_segment(self):
        """把旧默认「长回复拆两条」一次性迁成「一条发完」（1.3.57）。

        为什么现在改默认：真机实测一次完整发送（逐帧取证 → 聚焦 → 粘贴 → 回读 →
        按发送键 → 确认清空）约 9.6 秒，拆两条就是 19~22 秒，用户体感就是「很久不回」。
        拆条是 1.3.52 定的工厂默认 2，用户没自己改过；只有盘上恰好是 2 才迁，
        1/3/4 或非数字一律不动。标记与 fast_merge_800 同放 plugin_settings.migrations.json，
        设置没落盘就不写标记，下次启动还能重来。
        """
        if getattr(self, '_single_segment_migration_done', False):
            return
        self._single_segment_migration_done = True
        marker = 'single_segment_1357'
        migrations = {}
        try:
            raw = json.loads(self._migrations_path.read_text('utf-8'))
            if isinstance(raw, dict):
                migrations = raw
        except FileNotFoundError:
            pass
        except Exception as exc:
            logger.warning('读取插件迁移标记失败，按未迁移处理：%s', exc)
        if migrations.get(marker) is True:
            return
        prefs = self._settings.get('chat_controls')
        if not isinstance(prefs, dict):
            return
        value = prefs.get('replySegments')
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            return
        if value != 2:
            return
        prefs['replySegments'] = 1
        try:
            self._save_settings()
        except Exception as exc:
            logger.warning('拆条迁移保存设置失败，保留内存值，下次启动再试：%s', exc)
            return
        migrations[marker] = True
        try:
            from ..persistence import atomic_text
            atomic_text(self._migrations_path,
                        json.dumps(migrations, ensure_ascii=False, indent=2), backup=False)
        except Exception as exc:
            logger.warning('写插件迁移标记失败，下次启动会重新判定：%s', exc)

    def _import_plugin_module(self, mod_name: str):
        """加载一个插件模块。

        先按包内模块名导入（内置插件都走这里）；失败再尝试从
        ``custom_plugins_dir`` 按**文件路径**加载 —— 用户装的插件不在包目录里。
        两次都失败返回 None（调用方打日志跳过）。
        """
        if mod_name in RETIRED_PLUGINS:
            return None
        try:
            return importlib.import_module(f".{mod_name}", package="src.plugins")
        except ModuleNotFoundError as exc:
            if exc.name != f'src.plugins.{mod_name}':
                self._fault(mod_name, 'import', exc)
                return None
        except (Exception, SystemExit) as exc:
            self._fault(mod_name, 'import', exc)
            return None

        path = self.custom_plugins_dir / f"{mod_name}.py"
        if not path.is_file():
            from ..plugin_catalog import MARKET_PLUGINS
            if mod_name not in MARKET_PLUGINS or mod_name in self.custom_plugin_ids():
                self._fault(mod_name, 'import', FileNotFoundError())
            return None
        # 唯一的模块名，避免与包内模块重名互相污染
        alias = f"src_custom_plugin_{mod_name}"
        try:
            from ..plugin_installer import check_source
            source = path.read_text('utf-8')
            code = compile(source, str(path), 'exec')
            if check_source(source):
                self._fault(mod_name, 'source_check', ValueError())
                return None
            spec = importlib.util.spec_from_file_location(alias, path)
            if spec is None or spec.loader is None:
                return None
            module = importlib.util.module_from_spec(spec)
            # 关键：项目里的插件习惯用**相对导入**（`from . import PluginBase`）。
            # 按文件路径加载的模块默认是顶层模块，没有包上下文，
            # 相对导入会直接 ImportError（实测：
            # "attempted relative import with no known parent package"）。
            # 把 __package__ 指到 src.plugins，相对导入才能解析。
            module.__package__ = "src.plugins"
            sys.modules[alias] = module
            # 执行已检查的同一份源码，避免快速修复后命中mtime/大小相同的旧pyc。
            exec(code, module.__dict__)
            return module
        except (Exception, SystemExit) as exc:
            self._fault(mod_name, 'import', exc)
            sys.modules.pop(alias, None)
            return None

    def _load_plugins(self):
        # 内置模块 + 用户自己装的（自定义清单）；自定义排在后面，
        # 万一标识撞了也不会顶掉内置插件的行为。
        modules = list(PLUGIN_MODULES)
        if self.plugins_from_packs():
            # 发行构建：可独立分发的可选插件只从插件包加载，源码里的实现不参与导入。
            # 包没装就等于没装，不能因为缺包就启动失败。
            modules = [name for name in modules if name not in OPTIONAL_PLUGIN_PACKS]
        for custom_id in self.custom_plugin_ids():
            if custom_id not in modules:
                modules.append(custom_id)

        for mod_name in modules:
            if self._skip_fault(mod_name):
                continue
            mod = self._import_plugin_module(mod_name)
            if mod is None:
                continue
            source = 'builtin' if mod_name in PLUGIN_MODULES else 'custom'
            self._load_module_plugins(mod_name, mod, source)

        self.load_packs()

    def _load_module_plugins(self, module_id, mod, source='builtin'):
        """实例化并注册一个模块里的 PluginBase 子类。

        与旧实现一致的失败处理：任何阶段出错都把设置回滚到加载前、把模块名从
        _plugins 里摘掉、记一条故障，然后返回 False——绝不向调用方抛异常。
        返回 True 表示注册成功。
        """
        previous_settings = copy.deepcopy(self._settings)
        post_load_started = False
        stage = 'metadata'
        try:
            found = False
            for attr in dir(mod):
                cls = getattr(mod, attr)
                if (isinstance(cls, type) and issubclass(cls, PluginBase)
                        and cls is not PluginBase and cls.META is not None):
                    found = True
                    stage = 'metadata'
                    meta = cls.META
                    self._validate_meta(meta, module_id)
                    if meta.id in self._plugins:
                        raise ValueError('duplicate plugin id')
                    stage = 'constructor'
                    inst = cls(self)
                    self._validate_meta(inst.META, module_id)
                    if inst.META.id in RETIRED_PLUGINS:
                        continue
                    overrides = self._default_overrides.get(inst.META.id)
                    if overrides:
                        # 只改默认值本身：用户改过的存档不能被默认值覆盖。
                        inst.META = copy.deepcopy(inst.META)
                        for key, value in overrides.items():
                            schema = inst.META.config_schema[key]
                            inst.META.default_settings[key] = _validate_field(
                                schema, value, key, inst.META.id)
                    self._plugins[inst.META.id] = inst
                    self._plugin_ids[id(inst)] = inst.META.id
                    self._set_source(inst.META.id, source)
                    # Seed default settings if not yet saved
                    if inst.META.id not in self._settings:
                        self._settings[inst.META.id] = copy.deepcopy(inst.META.default_settings)
                    # 注册之后再给一次机会（迁移旧设置等）：
                    # 有些插件在 __init__ 里就调 update_settings，那时还没注册会失败。
                    post_load_started = True
                    stage = 'post_load'
                    loaded = inst.post_load()
                    if inspect.iscoroutine(loaded):
                        loaded.close()
                        raise TypeError('post_load must be synchronous')
            if not found:
                raise ValueError('no plugin class')
            return True
        except (Exception, SystemExit) as exc:
            self._plugins.pop(module_id, None)
            self._settings = previous_settings
            if post_load_started:
                try:
                    self._save_settings()
                except Exception:
                    pass
            self._fault(module_id, stage, exc)
            return False

    # ── 插件包（1.3.63） ───────────────────────────────────────────────────
    def _resolve_pack_dirs(self, pack_dirs):
        """默认扫描顺序：程序旁 plugin-packs 优先，其次数据目录 plugin_packs。"""
        if pack_dirs:
            return [Path(item) for item in pack_dirs]
        try:
            dirs = default_pack_dirs(self.data_dir)
        except Exception:
            dirs = [self.data_dir / PACK_DIRNAME]
        return [Path(item) for item in dirs]

    def plugins_from_packs(self) -> bool:
        """WHALE_PLUGINS_FROM_PACKS=1：可选插件只认插件包，源码实现不参与导入。"""
        return bool(getattr(self, '_from_packs', False))

    def pack_install_dir(self) -> Path:
        """安装目标：数据目录下的 plugin_packs（程序旁那份随发布物一起、只读）。"""
        for item in reversed(getattr(self, 'pack_dirs', None) or []):
            if Path(item).name == PACK_DIRNAME:
                return Path(item)
        return Path(getattr(self, 'data_dir', '.')) / PACK_DIRNAME

    def _set_source(self, plugin_id, source):
        sources = getattr(self, '_sources', None)
        if not isinstance(sources, dict):
            sources = {}
            self._sources = sources
        sources[str(plugin_id)] = str(source)

    def plugin_source(self, plugin_id) -> str:
        """这个插件从哪来：builtin（内置）/ custom（单文件自定义）/ pack（插件包）。"""
        sources = getattr(self, '_sources', None) or {}
        if plugin_id in sources:
            return sources[plugin_id]
        if plugin_id in getattr(self, '_pack_ids', set()):
            return 'pack'
        if plugin_id in PLUGIN_MODULES:
            return 'builtin'
        return 'custom'

    def _is_removable(self, plugin_id) -> bool:
        if plugin_id in CORE_PLUGINS:
            return False
        return plugin_id in PLUGIN_MODULES or self.plugin_source(plugin_id) == 'pack'

    def _fault_message(self, plugin_id) -> str:
        fault = (getattr(self, '_faults', None) or {}).get(plugin_id) or {}
        return str(fault.get('message') or '插件包加载失败，已隔离，其他插件继续运行')

    def _drop_stale_fault(self, plugin_id) -> None:
        """插件包已经被删掉时，清掉它上次留下的故障记录，免得界面一直报错。"""
        faults = getattr(self, '_faults', None)
        strikes = getattr(self, '_strikes', None)
        changed = False
        if isinstance(faults, dict) and faults.pop(plugin_id, None) is not None:
            changed = True
        if isinstance(strikes, dict) and strikes.pop(plugin_id, None) is not None:
            changed = True
        if changed:
            try:
                self._save_faults()
            except Exception:
                pass

    def _pack_available_ids(self):
        known = set(PLUGIN_MODULES)
        try:
            known.update(self.custom_plugin_ids())
        except Exception:
            pass
        known.update(getattr(self, '_plugins', {}) or {})
        return known

    def load_packs(self) -> List[dict]:
        """扫描插件包目录，把 status='ok' 的包注册进 self._plugins（source='pack'）。

        返回报告列表：每个包一条 {id,name,version,path,status,errors,meta,warnings,
        installed,enabled,source}。坏包只记进报告，不抛异常、不影响别的包；
        目录不存在时静默返回空列表。
        """
        dirs = [Path(item) for item in (getattr(self, 'pack_dirs', None) or [])]
        # 重扫前先摘掉上一轮的包插件，否则会被重复注册逻辑当成故障
        for pid in list(getattr(self, '_pack_ids', ()) or ()):
            self._plugins.pop(pid, None)
            sources = getattr(self, '_sources', None)
            if isinstance(sources, dict):
                sources.pop(pid, None)
        self._pack_ids = set()
        report: List[dict] = []
        self._pack_report = report
        self._pack_loaded = True
        try:
            entries = discover_pack_dirs(dirs, _host_version(),
                                         available_ids=self._pack_available_ids())
        except Exception as exc:  # 扫描整体失败也要能启动
            logger.warning('扫描插件包目录失败：%s', exc)
            entries = []
        for entry in entries:
            row = dict(entry)
            row['source'] = 'pack'
            row['installed'] = False
            row['enabled'] = False
            row['message'] = ''
            report.append(row)
            if entry.get('status') != 'ok':
                continue
            pack_id = entry.get('id') or ''
            if pack_id in self._plugins:
                # 内置插件与插件包同 id：这是正常情况（包就是内置插件的可选发行形态），
                # 不能报成失败，否则全新安装的插件包页面会全是红色错误。
                if pack_id in PLUGIN_MODULES:
                    row['status'] = 'builtin'
                    row['message'] = '该插件已内置在主程序里，插件包无需再启用'
                    continue
                row['status'] = 'shadowed'
                row['errors'] = ['已存在同 id 的插件（自定义），这个插件包没有生效']
                continue
            meta = entry.get('meta') or {}
            had_settings = pack_id in (getattr(self, '_settings', None) or {})
            loaded = load_pack_module(Path(entry['path']) / str(meta.get('entry') or 'plugin.py'),
                                      pack_id)
            if not loaded.get('ok'):
                row['status'] = 'error'
                row['errors'] = list(loaded.get('errors') or [])
                row['message'] = str(loaded.get('message') or '')
                self._set_source(pack_id, 'pack')
                self._fault(pack_id, 'load', RuntimeError(row['message'] or '插件包加载失败'))
                continue
            if not self._load_module_plugins(pack_id, loaded['module'], 'pack'):
                row['status'] = 'error'
                row['message'] = self._fault_message(pack_id)
                row['errors'] = [row['message']]
                continue
            row['installed'] = True
            plugin = self._plugins.get(pack_id)
            # 默认模式（非发行包模式）下，新发现的插件包只登记、不自动启用；
            # 用户手动开启过（设置里已有记录）就不再覆盖。
            if plugin is not None and not had_settings and not self.plugins_from_packs():
                schema = getattr(plugin.META, 'config_schema', {}) or {}
                if 'enabled' in schema:
                    bucket = self._settings.setdefault(pack_id, {})
                    bucket['enabled'] = False
                    try:
                        self._save_settings()
                    except Exception:
                        logger.warning('保存插件包默认关闭状态失败：%s', pack_id)
            if plugin is not None:
                row['enabled'] = self._enabled(plugin)
                row['name'] = plugin.META.name
                row['version'] = plugin.META.version or row.get('version') or ''
            self._pack_ids.add(pack_id)
        if self.plugins_from_packs():
            present = {row.get('id') for row in report}
            for pid in OPTIONAL_PLUGIN_PACKS:
                if pid in present:
                    continue
                report.append({'id': pid, 'name': pid, 'version': '', 'path': '',
                               'status': 'not_installed', 'errors': [], 'meta': {},
                               'warnings': [], 'shadowed_by': '', 'source': 'pack',
                               'installed': False, 'enabled': False,
                               'message': '插件包未安装'})
                if pid not in self._plugins:
                    self._drop_stale_fault(pid)
        return report

    @staticmethod
    def _public_pack_row(item) -> dict:
        row = {key: item.get(key) for key in (
            'id', 'name', 'version', 'path', 'status', 'errors', 'warnings',
            'shadowed_by', 'source', 'installed', 'enabled', 'message')}
        row['errors'] = list(row.get('errors') or [])
        row['warnings'] = list(row.get('warnings') or [])
        return row

    def pack_status(self) -> dict:
        """插件包总览：哪些装了、哪些坏了、哪些没装（不会因为坏包报错）。"""
        report = getattr(self, '_pack_report', None)
        if not isinstance(report, list) or (not report and not getattr(self, '_pack_loaded', False)):
            if hasattr(self, 'pack_dirs'):
                report = self.load_packs()
            else:
                report = []
        packs = [self._public_pack_row(item) for item in report]
        installed = [item for item in packs if item.get('installed')]
        broken = [item for item in packs
                  if item.get('status') in ('invalid', 'incompatible', 'error')]
        missing = [item.get('id') for item in packs if item.get('status') == 'not_installed']
        dirs = [str(Path(item)) for item in (getattr(self, 'pack_dirs', None) or [])]
        message = '没有发现插件包'
        if packs:
            message = '共 ' + str(len(packs)) + ' 个插件包，' + str(len(installed)) + ' 个可用'
            if broken:
                message += '，' + str(len(broken)) + ' 个有问题'
            if missing:
                message += '，' + str(len(missing)) + ' 个未安装'
        return {'ok': True, 'from_packs': self.plugins_from_packs(), 'dirs': dirs,
                'count': len(packs), 'installed': len(installed), 'broken': len(broken),
                'missing': missing, 'packs': packs, 'message': message}

    def plugin_status(self) -> dict:
        """插件总览（给前端刷新用）：分组计数 + 异常数 + 最近一条错误文案。"""
        counts = {'builtin': 0, 'custom': 0, 'pack': 0}
        broken = 0
        latest = ''
        try:
            rows = self.list_plugins()
        except Exception as exc:
            logger.warning('插件总览读取失败：%s', exc)
            rows = []
        for row in rows:
            source = row.get('source') or 'builtin'
            counts[source] = counts.get(source, 0) + 1
            if row.get('faulted'):
                broken += 1
                fault = row.get('fault') or {}
                latest = fault.get('message') or latest
        packs = self.pack_status()
        pack_broken = 0
        for item in packs.get('packs') or []:
            if item.get('status') in ('invalid', 'incompatible', 'error'):
                pack_broken += 1
                if not latest:
                    errors = item.get('errors') or []
                    latest = errors[0] if errors else '插件包 ' + str(item.get('id')) + ' 有问题'
        message = '共 ' + str(len(rows)) + ' 个插件，' + str(broken) + ' 个异常'
        if pack_broken:
            message += '，' + str(pack_broken) + ' 个插件包有问题'
        return {'ok': True, 'total': len(rows), 'counts': counts, 'broken': broken,
                'pack_broken': pack_broken, 'latest_error': latest,
                'from_packs': packs.get('from_packs', False),
                'packs': {'count': packs.get('count', 0), 'installed': packs.get('installed', 0),
                          'broken': pack_broken, 'missing': packs.get('missing', [])},
                'message': message}

    def _validate_meta(self, meta, module_id):
        if not isinstance(meta, PluginMeta) or meta.id != module_id:
            raise ValueError('plugin id must match module id')
        if not all(isinstance(getattr(meta, key), str) for key in ('name', 'description', 'category')):
            raise TypeError('invalid plugin text metadata')
        if not isinstance(meta.config_schema, dict) or not isinstance(meta.default_settings, dict):
            raise TypeError('invalid plugin schema/defaults')
        if not isinstance(meta.enabled_by_default, bool) or any(not isinstance(key, str) for key in meta.config_schema):
            raise TypeError('invalid plugin defaults or field names')
        if any(not isinstance(value, FieldSchema) for value in meta.config_schema.values()):
            raise TypeError('invalid field schema')
        for field in meta.config_schema.values():
            if not isinstance(field.type, str) or not isinstance(field.label, str):
                raise TypeError('invalid field metadata')
            for bound in (field.min, field.max):
                if bound is not None and (not _is_number(bound) or not math.isfinite(bound)):
                    raise TypeError('invalid field numeric bound')
            if field.min is not None and field.max is not None and field.min > field.max:
                raise ValueError('field minimum exceeds maximum')
            if not isinstance(field.options, list) or not isinstance(field.values, list):
                raise TypeError('invalid field choices')
        json.dumps(meta.schema_dict(), allow_nan=False)
        json.dumps(meta.default_settings, allow_nan=False)

    def get_settings(self, plugin_id: str) -> dict:
        """返回深拷贝，调用方修改不会影响内部状态。"""
        return copy.deepcopy(self._settings.get(plugin_id, {}))

    def validated_settings(self, plugin_id: str, settings: dict):
        """纯校验并构造新值，批量规则不能保存一半后才发现其他字段无效。"""
        if plugin_id not in self._plugins:
            raise KeyError(f"Unknown plugin: {plugin_id}")
        if plugin_id in self._removed:
            raise ValueError('插件已移除，请先恢复再修改配置')
        if not isinstance(settings, dict):
            raise ValueError("settings must be a dict")
        meta = self._plugins[plugin_id].META
        schema = meta.config_schema or {}
        defaults = copy.deepcopy(meta.default_settings or {})
        previous = copy.deepcopy(self._settings.get(plugin_id, {}))

        # 先全部校验并构建新值，再落内存；非法输入在这一步就抛出，内存不会被改。
        merged = copy.deepcopy(defaults)
        for key, value in previous.items():
            if key not in schema:
                continue
            try:
                merged[key] = _validate_field(schema[key], value, key, plugin_id)
            except Exception:
                merged[key] = _fallback_field_default(schema[key], defaults, key)
        for key, value in settings.items():
            if key not in schema:
                raise ValueError(f"{plugin_id}: unknown setting field '{key}'")
            merged[key] = _validate_field(schema[key], value, key, plugin_id)

        self._plugins[plugin_id].validate_settings(merged)
        return merged

    def update_settings(self, plugin_id: str, settings: dict):
        self.update_many({plugin_id:settings})

    def update_many(self, updates: dict):
        if not isinstance(updates,dict) or not updates:
            raise ValueError('请输入要更新的插件规则')
        merged={pid:self.validated_settings(pid,settings) for pid,settings in updates.items()}
        previous=copy.deepcopy(self._settings)
        self._settings.update(merged)
        try:
            self._save_settings()
        except Exception:
            self._settings=previous
            raise

    def list_plugins(self) -> List[dict]:
        result = []
        for pid, plugin in tuple(self._plugins.items()):
            with self.protected(plugin, 'list'):
                row = self._plugin_row(pid, plugin)
                json.dumps(row, allow_nan=False)
                result.append(row)
        seen = {row['id'] for row in result}
        for fault in self.health():
            if fault['id'] not in seen:
                pid = fault['id']
                result.append({'id': pid, 'name': pid, 'description': fault['message'],
                    'category': 'utility', 'enabled': False, 'settings': {}, 'defaults': {},
                    'schema': {}, 'source': self.plugin_source(pid),
                    'removable': self._is_removable(pid),
                    'removed': pid in self._removed, 'faulted': True, 'fault': fault})
        return result

    def _plugin_row(self, pid, plugin):
        m = plugin.META
        return {
            "id": m.id,
            "name": m.name,
            "description": m.description,
            "category": m.category,
            "enabled": self._enabled(plugin),
            "settings": copy.deepcopy(self._settings.get(pid, {})),
            "defaults": copy.deepcopy(m.default_settings or {}),
            "schema": {k:v for k,v in m.schema_dict().items() if not (pid=='chat_controls' and k=='interjectPercent')},
            "source": self.plugin_source(pid),
            "removable": self._is_removable(pid),
            "removed": pid in self._removed,
            "faulted": self.is_quarantined(pid),
            "fault": copy.deepcopy(self._faults.get(pid)),
        }

