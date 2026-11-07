"""生成 plugin-packs/ 里的种子插件包（幂等：重复运行结果完全一致）。

用法：
    ./.venv-wechat/Scripts/python.exe tools/make_plugin_packs.py
    ./.venv-wechat/Scripts/python.exe tools/make_plugin_packs.py --out 别的目录

每个包只做三件事：写清单 plugin.json、写薄封装 plugin.py（真正实现在
src/plugins/<id>.py，这里只转出插件类）、写 README 与设置示例。
不打包 exe、不联网、不碰 data/ 下的用户数据。
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.plugin_catalog import MARKET_DESCRIPTIONS  # noqa: E402
from src.plugin_packs import OPTIONAL_PLUGIN_PACKS  # noqa: E402
from src.plugins import PluginBase  # noqa: E402

PACK_VERSION = "1.0.0"
AUTHOR = "鲸鱼娘"
API_VERSION = 2
# 插件包要求的最低宿主版本：比当前版本低一档，避免每次升版都要重发种子包
REQUIRES = "1.3.0"
HOOK_ATTRS = ("prepare", "tick", "ready", "describe", "generate")


def find_plugin_class(pack_id: str):
    """按 id 导入 src.plugins.<id>，返回 (模块, 插件类)。"""
    module = __import__("src.plugins." + pack_id, fromlist=["*"])
    for value in vars(module).values():
        if (isinstance(value, type) and issubclass(value, PluginBase)
                and value is not PluginBase and getattr(value, "META", None) is not None):
            return module, value
    raise RuntimeError("模块 src.plugins." + pack_id + " 里没有找到插件类")


# 一个钩子都不实现的包也是合法的，但必须说清自己是哪一类（见
# src/plugin_packs.py 的 CAPABILITY_KINDS）。空 capabilities 在严格档下会被判非法，
# 所以生成器必须替这类包补上正确的种类，而不是留空。
NON_HOOK_CAPABILITIES = {
    "sticker_admin": "settings",   # 纯设置门：只是藏起管理入口，不参与链路
    "sticker_memory": "helper",    # 纯工具库：导出指纹函数给别的模块调用
}


def capabilities_of(pack_id: str, cls) -> list:
    """插件自己重写了哪些钩子，作为 capabilities 写进清单。

    钩子一个都没有时，退回 NON_HOOK_CAPABILITIES 里登记的种类；
    仍然没有就返回空列表（严格档下会被判非法，这是**故意的**：
    说不清自己提供什么能力的包，用户没法判断要不要装）。
    """
    names = []
    for name, value in vars(cls).items():
        if name.startswith("on_") or name in HOOK_ATTRS:
            if callable(value) or isinstance(value, (staticmethod, classmethod)):
                names.append(name)
    if not names:
        kind = NON_HOOK_CAPABILITIES.get(pack_id)
        return [kind] if kind else []
    return sorted(names)


# data_policy 从 1.3.106 起是标签列表（见 src/plugin_packs.py 的 DATA_POLICY_TAGS）。
# 生成器按插件的真实能力给标签：联网的加 external_network，看聊天内容的加
# chat_content，其余至少给 own_settings —— 严格档要求非空。
DATA_POLICY = {
    "group_ambient": ["own_settings", "own_state", "chat_content"],
    "group_summary": ["own_settings", "own_state", "chat_content"],
    "image_library": ["own_settings", "own_state", "chat_content"],
    "member_aliases": ["own_settings", "own_state", "chat_content"],
    "reply_pace": ["own_settings"],
    "sticker_admin": ["own_settings", "own_state"],
    "sticker_memory": ["own_settings", "own_state", "chat_content"],
    "sticker_steal": ["own_settings", "own_state", "chat_content"],
    "web_context": ["own_settings", "own_state", "external_network"],
    "web_lookup": ["own_settings", "own_state", "external_network", "chat_content"],
}
def manifest_of(pack_id: str, cls) -> dict:
    meta = cls.META
    description = MARKET_DESCRIPTIONS.get(pack_id) or meta.description
    return {
        "id": pack_id,
        "name": meta.name,
        "version": PACK_VERSION,
        "entry": "plugin.py",
        "author": AUTHOR,
        "description": description,
        "api_version": API_VERSION,
        "requires": REQUIRES,
            "data_policy": DATA_POLICY[pack_id],
        "native": "纯 Python，不依赖外部程序",
        "side": "本机侧运行",
        "upstream": "随控制台源码分发（plugin-packs 种子包）",
        "capabilities": capabilities_of(pack_id, cls),
    }


def wrapper_of(pack_id: str, cls) -> str:
    """薄封装：只把真实插件类转出成 PLUGIN，不复制任何实现。"""
    name = cls.META.name
    return (
        '"""' + name + ' 插件包入口（薄封装）。\n'
        "\n"
        "真正实现在 src/plugins/" + pack_id + ".py，这里只转出插件类：\n"
        "插件包被单独安装时，加载器扫到的就是这个文件。\n"
        '"""\n'
        "from __future__ import annotations\n"
        "\n"
        "from src.plugins." + pack_id + " import " + cls.__name__ + " as PLUGIN\n"
        "\n"
        "META = PLUGIN.META\n"
    )


def readme_of(pack_id: str, cls) -> str:
    meta = cls.META
    description = MARKET_DESCRIPTIONS.get(pack_id) or meta.description
    return (
        "# " + meta.name + "\n"
        + "\n"
        + description.rstrip("。") + "。\n"
        "\n"
        "- 怎么装：把整个 " + pack_id + " 文件夹放进程序旁的 plugin-packs\\ 目录"
        "（或数据目录的 plugin_packs\\），重启控制台后在插件列表里启用。\n"
        "- 怎么删：关掉控制台，删掉 plugin-packs\\" + pack_id + " 整个文件夹即可，"
        "已保存的设置不会影响别的插件。\n"
    )


def write_if_changed(path: Path, content: str) -> bool:
    """内容相同就不写，保证重复生成不产生无意义的文件改动。"""
    if path.is_file():
        try:
            if path.read_text("utf-8") == content:
                return False
        except OSError:
            pass
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content.encode("utf-8"))
    return True


def generate(out_dir: Path | None = None) -> dict:
    """生成全部种子插件包，返回 {ok, written, unchanged, packs, errors}。"""
    target = Path(out_dir) if out_dir else ROOT / "plugin-packs"
    written, unchanged, packs, errors = [], [], [], []
    try:
        target.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        return {"ok": False, "written": [], "unchanged": [], "packs": [],
                "errors": ["插件包目录无法创建：" + str(exc)]}
    for pack_id in OPTIONAL_PLUGIN_PACKS:
        try:
            _module, cls = find_plugin_class(pack_id)
            manifest = manifest_of(pack_id, cls)
            files = {
                "plugin.json": json.dumps(manifest, ensure_ascii=False, indent=2) + "\n",
                "plugin.py": wrapper_of(pack_id, cls),
                "README.md": readme_of(pack_id, cls),
                "settings.example.json": json.dumps(
                    cls.META.default_settings, ensure_ascii=False, indent=2) + "\n",
            }
            changed = False
            for name, content in files.items():
                if write_if_changed(target / pack_id / name, content):
                    changed = True
            packs.append(pack_id)
            (written if changed else unchanged).append(pack_id)
        except (Exception, SystemExit) as exc:
            errors.append(pack_id + " 生成失败：" + str(exc))
    return {"ok": not errors, "written": written, "unchanged": unchanged,
            "packs": packs, "errors": errors, "target": str(target)}


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="生成 plugin-packs 种子插件包")
    parser.add_argument("--out", default="", help="输出目录（默认工程根 plugin-packs）")
    args = parser.parse_args(argv)
    result = generate(Path(args.out) if args.out else None)
    print("插件包目录：" + result.get("target", ""))
    print("生成 " + str(len(result["packs"])) + " 个包，改动 " + str(len(result["written"]))
          + " 个，未变 " + str(len(result["unchanged"])) + " 个")
    for message in result["errors"]:
        print("错误：" + message)
    return 0 if result["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
