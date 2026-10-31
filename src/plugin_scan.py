"""插件源码分级扫描：只判定、不执行、不写盘。

和 src/plugin_installer.py 的 check_source 分工不同，两者都要有：
- check_source 是**阻断闸门**：命中红线直接拒绝安装，宁可错杀。
- 这里是**风险画像**：告诉用户"这个插件可能会干什么"，分 forbidden /
  risky / clean 三档，给预检界面展示，不参与放行判断。

分级口径
- forbidden：能在本机执行任意代码或起子进程——os.system/os.popen/os.exec*、
  subprocess、eval/exec/compile/__import__、ctypes、marshal·pickle 反序列化、
  __globals__ 逃逸。
- risky：会碰文件、网络或动态导入——写模式 open、Path.write_text/write_bytes、
  os.remove/rename/replace、shutil、socket、urllib、requests、httpx、importlib。
- clean：以上都没有。

判定基于 AST 而不是正则：注释和文档字符串里提到这些名字不算风险（本仓库
sticker_steal.py 的说明里就写着 wechatauto.media），而 `from os import system
as _s` 这种别名照样认得出来。
"""
from __future__ import annotations

import ast
from pathlib import Path
from typing import Dict, List, Tuple

#: 三档 + 语法非法时的 unknown（交给调用方自己的阻断逻辑处理）
LEVELS = ("clean", "risky", "forbidden")

MAX_SCAN_BYTES = 512 * 1024      # 单文件扫描上限 512KB
MAX_FINDINGS_PER_RULE = 3        # 同一条规则最多留 3 处
MAX_FINDINGS = 40                # 一次扫描最多留 40 条

# (限定名模式, 档位, 说明)；模式末尾的 * 表示前缀匹配
RULES: Tuple[Tuple[str, str, str], ...] = (
    ("os.system", "forbidden", "调用系统命令 os.system"),
    ("os.popen", "forbidden", "调用系统命令 os.popen"),
    ("os.exec*", "forbidden", "替换当前进程 os.exec*"),
    ("os.spawn*", "forbidden", "起子进程 os.spawn*"),
    ("subprocess.*", "forbidden", "起子进程 subprocess"),
    ("eval", "forbidden", "动态执行代码 eval"),
    ("exec", "forbidden", "动态执行代码 exec"),
    ("compile", "forbidden", "动态编译代码 compile"),
    ("__import__", "forbidden", "动态导入 __import__"),
    ("ctypes.*", "forbidden", "调用底层 API ctypes"),
    ("marshal.loads", "forbidden", "反序列化 marshal（可执行任意字节码）"),
    ("pickle.loads", "forbidden", "反序列化 pickle（可执行任意代码）"),
    ("pickle.load", "forbidden", "反序列化 pickle（可执行任意代码）"),
    ("open", "risky", "以写模式打开文件 open"),
    ("io.open", "risky", "以写模式打开文件 io.open"),
    ("codecs.open", "risky", "以写模式打开文件 codecs.open"),
    ("write_text", "risky", "写文件 Path.write_text"),
    ("write_bytes", "risky", "写文件 Path.write_bytes"),
    ("os.remove", "risky", "删除文件 os.remove"),
    ("os.unlink", "risky", "删除文件 os.unlink"),
    ("os.rename", "risky", "改名 os.rename"),
    ("os.replace", "risky", "覆盖 os.replace"),
    ("os.mkdir", "risky", "建目录 os.mkdir"),
    ("os.makedirs", "risky", "建目录 os.makedirs"),
    ("os.rmdir", "risky", "删目录 os.rmdir"),
    ("shutil.*", "risky", "批量文件操作 shutil"),
    ("socket.*", "risky", "原始网络套接字 socket"),
    ("urllib.*", "risky", "直接发 HTTP 请求 urllib"),
    ("requests.*", "risky", "直接发 HTTP 请求 requests"),
    ("httpx.*", "risky", "直接发 HTTP 请求 httpx"),
    ("http.client", "risky", "直接发 HTTP 请求 http.client"),
    ("importlib.*", "risky", "运行期动态导入 importlib"),
    ("winreg.*", "forbidden", "改注册表 winreg"),
)

#: 只写方法名、几乎总是文件操作的名字（插件里 `state.unlink()` 这种）
_BARE_WRITE_CALLS = ("unlink", "rmtree", "makedirs")

#: 只认全等、不认后缀的内建名。re.compile / pattern.exec 太常见，用后缀匹配
#: 会把每个插件都扫成 forbidden。
_EXACT_ONLY = ("compile", "eval", "exec", "__import__")

_WRITE_FLAGS = ("w", "a", "x", "+")


def _dotted(node) -> str:
    """把 `a.b.c` 之类的调用目标还原成点号字符串；认不出就返回空串。"""
    parts: List[str] = []
    while isinstance(node, ast.Attribute):
        parts.append(node.attr)
        node = node.value
    if isinstance(node, ast.Name):
        parts.append(node.id)
    elif isinstance(node, ast.Call):
        inner = _dotted(node.func)
        if not inner:
            return ""
        parts.append(inner)
    elif isinstance(node, ast.Constant):
        parts.append(repr(node.value))
    else:
        return ""
    return ".".join(reversed(parts))


def _aliases(tree) -> Dict[str, str]:
    """收集 import 别名：本地名 → 全限定名（相对导入不参与，认不准就不猜）。"""
    table: Dict[str, str] = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for item in node.names:
                if item.asname:
                    table[item.asname] = item.name
                else:
                    head = item.name.split(".")[0]
                    table[head] = head
        elif isinstance(node, ast.ImportFrom) and not node.level and node.module:
            for item in node.names:
                if item.name == "*":
                    continue
                table[item.asname or item.name] = f"{node.module}.{item.name}"
    return table


def _qualify(name: str, aliases: Dict[str, str]) -> str:
    """按别名表把首段还原成全限定名，最多还原一次（够用且不会绕成环）。"""
    if not name:
        return ""
    head, _, rest = name.partition(".")
    full = aliases.get(head)
    if not full:
        return name
    return f"{full}.{rest}" if rest else full


def _matches(name: str, pattern: str) -> bool:
    """模式匹配：末尾 * 为前缀匹配，其余全等；只写方法名的规则也认最后一段。"""
    if pattern.endswith("*"):
        return name.startswith(pattern[:-1])
    return name == pattern or name.endswith("." + pattern)


def _write_mode(call) -> bool:
    """open 类调用是不是写模式；没写 mode 参数按只读算。"""
    mode = ""
    if len(call.args) >= 2 and isinstance(call.args[1], ast.Constant):
        value = call.args[1].value
        if isinstance(value, str):
            mode = value
    for keyword in call.keywords:
        if keyword.arg == "mode" and isinstance(keyword.value, ast.Constant):
            value = keyword.value.value
            if isinstance(value, str):
                mode = value
    return any(flag in mode for flag in _WRITE_FLAGS)


def _rule_for(name: str, call) -> Tuple[str, str, str]:
    """给一个调用名找规则；返回 (键, 档位, 说明) 或空元组。"""
    if not name:
        return ()
    for pattern, level, text in RULES:
        if pattern in _EXACT_ONLY:
            hit = name in (pattern, f"builtins.{pattern}")
        else:
            hit = _matches(name, pattern)
        if not hit:
            continue
        if pattern in ("open", "io.open", "codecs.open") and not _write_mode(call):
            continue
        return pattern, level, text
    tail = name.rsplit(".", 1)[-1]
    if tail in _BARE_WRITE_CALLS:
        return tail, "risky", f"文件操作 {tail}"
    return ()


def _summary(level: str, findings: List[Dict], error: str = "") -> str:
    if level == "unknown":
        return f"读不懂这段代码，无法分级：{error}"
    if level == "clean":
        return "没有发现起子进程、动态执行、文件写入或外联的用法"
    forbidden = sum(1 for item in findings if item["level"] == "forbidden")
    risky = len(findings) - forbidden
    if level == "forbidden":
        return f"含 {forbidden} 处可执行任意代码的用法" + (f"，另有 {risky} 处文件/网络用法" if risky else "")
    return f"含 {risky} 处文件、网络或动态导入用法"


def scan_source(code: str, name: str = "") -> Dict:
    """扫描一段插件源码，返回 {name, level, findings, summary}。绝不抛异常。"""
    try:
        tree = ast.parse(code)
    except SyntaxError as exc:
        return {"name": name, "level": "unknown", "findings": [], "counts": {},
                "error": f"第 {exc.lineno} 行 {exc.msg}",
                "summary": _summary("unknown", [], f"第 {exc.lineno} 行 {exc.msg}")}

    aliases = _aliases(tree)
    findings: List[Dict] = []
    seen: Dict[str, int] = {}

    def add(key: str, level: str, lineno: int, text: str) -> None:
        if len(findings) >= MAX_FINDINGS or seen.get(key, 0) >= MAX_FINDINGS_PER_RULE:
            return
        seen[key] = seen.get(key, 0) + 1
        findings.append({"rule": key, "level": level, "lineno": lineno, "detail": text})

    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            rule = _rule_for(_qualify(_dotted(node.func), aliases), node)
            if rule:
                add(rule[0], rule[1], getattr(node, "lineno", 0), rule[2])
        elif isinstance(node, ast.Attribute):
            if node.attr == "__globals__":
                add("__globals__", "forbidden", getattr(node, "lineno", 0),
                    "通过 __globals__ 逃逸模块命名空间")
                continue
            # 光取属性也算：ctypes.windll.user32 不是 Call，但它就是"要碰底层 API"的证据
            name = _qualify(_dotted(node), aliases)
            for pattern, level, text in RULES:
                if level == "forbidden" and pattern.endswith("*") and name.startswith(pattern[:-1]):
                    add(pattern, level, getattr(node, "lineno", 0), text)
                    break

    findings.sort(key=lambda item: item["lineno"])
    if any(item["level"] == "forbidden" for item in findings):
        level = "forbidden"
    elif findings:
        level = "risky"
    else:
        level = "clean"
    counts = {"forbidden": sum(1 for item in findings if item["level"] == "forbidden"),
              "risky": sum(1 for item in findings if item["level"] == "risky")}
    return {"name": name, "level": level, "findings": findings, "counts": counts,
            "error": "", "summary": _summary(level, findings)}


def scan_file(path) -> Dict:
    """扫描磁盘上的一个 .py；读不到就按 unknown 返回，不抛异常。"""
    target = Path(path)
    try:
        data = target.read_bytes()[:MAX_SCAN_BYTES]
    except OSError as exc:
        return {"name": target.name, "level": "unknown", "findings": [], "counts": {},
                "error": f"读不到文件：{exc}", "summary": _summary("unknown", [], f"读不到文件：{exc}")}
    return scan_source(data.decode("utf-8", errors="replace"), target.name)
