"""开发期辅助脚本：打印 wechatauto 的 DB / 发送 / 媒体下载接口。"""
import inspect

TARGETS = [
    ("wechatauto.db", ["WeChatDB", "Listener"]),
    ("wechatauto.sender", None),
    ("wechatauto.media", ["MediaDownloader"]),
    ("wechatauto.param", ["WxParam"]),
]


def dump_class(cls):
    print(f"  class {cls.__name__}")
    try:
        print(f"      __init__{inspect.signature(cls.__init__)}")
    except (TypeError, ValueError):
        pass
    for name in sorted(dir(cls)):
        if name.startswith("_"):
            continue
        obj = getattr(cls, name)
        if callable(obj):
            try:
                print(f"      {name}{inspect.signature(obj)}")
            except (TypeError, ValueError):
                print(f"      {name}(...)")


for mod_name, names in TARGETS:
    print("=" * 72)
    print(mod_name)
    print("=" * 72)
    try:
        module = __import__(mod_name, fromlist=["*"])
    except Exception as exc:
        print("  导入失败：", type(exc).__name__, exc)
        continue
    if names is None:
        names = [n for n in dir(module) if not n.startswith("_")]
    for name in names:
        obj = getattr(module, name, None)
        if obj is None:
            print(f"  {name}: 缺失")
        elif inspect.isclass(obj):
            dump_class(obj)
        elif inspect.isfunction(obj):
            try:
                print(f"  def {name}{inspect.signature(obj)}")
            except (TypeError, ValueError):
                print(f"  def {name}(...)")
    if mod_name == "wechatauto.param":
        for attr in ("TELEMETRY_ENABLED", "AD_API_URL", "REPORT_API_URL", "DEFAULT_SAVE_PATH", "LISTEN_INTERVAL"):
            if hasattr(module.WxParam, attr):
                print(f"  WxParam.{attr} = {getattr(module.WxParam, attr)!r}")
