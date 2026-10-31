"""开发期辅助脚本：打印 wechatauto 消息对象的字段与用法。"""
import inspect
import re
from pathlib import Path

from wechatauto import msgs

path = Path(inspect.getsourcefile(msgs))
text = path.read_text(encoding="utf-8", errors="replace")
print("file:", path)
print("lines:", len(text.splitlines()))
print("=" * 72)
print("类定义与字段：")
for line in text.splitlines():
    s = line.strip()
    if s.startswith("class ") or re.match(r"^(type|attr|sender|content)\b", s):
        print("   ", s[:140])
print("=" * 72)
print("含 sender 的行：")
for i, line in enumerate(text.splitlines(), 1):
    if "sender" in line:
        print(f"{i:5}: {line.strip()[:150]}")
print("=" * 72)
print("attr 取值相关：")
for i, line in enumerate(text.splitlines(), 1):
    if "attr[" in line or ".attr" in line:
        print(f"{i:5}: {line.strip()[:150]}")
