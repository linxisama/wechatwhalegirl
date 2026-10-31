"""开发期辅助脚本：打印已安装的微信自动化库的可用接口。

用法：.venv-wechat\\Scripts\\python.exe tools\\inspect_wechat_auto.py
只读取接口信息，不连接微信、不修改任何文件。
"""
import inspect
import sys
from pathlib import Path


def dump_module(name):
    try:
        module = __import__(name)
    except Exception as exc:  # pragma: no cover - 仅开发期使用
        print(f"[{name}] 无法导入：{type(exc).__name__}: {exc}")
        return
    print("=" * 72)
    print(f"[{name}] {getattr(module, '__file__', '')}")
    print("=" * 72)
    for attr in sorted(dir(module)):
        if attr.startswith("_"):
            continue
        obj = getattr(module, attr)
        if inspect.isclass(obj):
            print(f"class {attr}")
            for method in sorted(dir(obj)):
                if method.startswith("_"):
                    continue
                func = getattr(obj, method)
                if callable(func):
                    try:
                        print(f"    {method}{inspect.signature(func)}")
                    except (TypeError, ValueError):
                        print(f"    {method}(...)")


def dump_message_attrs():
    try:
        from wechatauto import msgs
    except Exception as exc:
        print("msgs 不可用：", exc)
        return
    print("=" * 72)
    print("[wechatauto.msgs] 已定义的消息类型")
    print("=" * 72)
    text = Path(inspect.getsourcefile(msgs)).read_text(encoding="utf-8", errors="replace")
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith("class ") or "type = " in stripped or "attr = " in stripped:
            print("   ", stripped[:120])


if __name__ == "__main__":
    for package in (sys.argv[1:] or ["wechatauto", "wxauto4"]):
        dump_module(package)
    dump_message_attrs()
