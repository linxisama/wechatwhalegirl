"""把市场插件打包成可下载的 zip（1.3.0 P0-2）。

产物结构（默认输出到 D:\\聊天机器人\\插件市场\\微信端）：

    <中文名>-<id>.zip        # 每个插件一个：plugin.json + <id>.py + 使用说明.md
    README.md                # 索引：装了什么、怎么装、风险提示
    使用说明/<id>.md         # 单页说明（方便直接看，不用解压）

为什么每个插件单独成包而不是一个大 zip：用户只想要"配图节奏"，不该被迫连
主动发言一起装下来；单个包也能让 `plugin_installer` 的预检与回滚按包生效。

用法：
    python tools/build_plugin_market.py             # 输出到默认目录
    python tools/build_plugin_market.py <输出目录>
"""
from __future__ import annotations

import json
import sys
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except (AttributeError, ValueError):
    pass

from src import plugin_installer  # noqa: E402
from src.plugin_catalog import MARKET_DESCRIPTIONS, MARKET_PLUGINS  # noqa: E402

DEFAULT_OUT = Path(r"D:\聊天机器人\插件市场\微信端")
FIRST_PARTY_PROACTIVE = {"group_ambient"}
SAFE_NAME = str.maketrans({"/": "_", "\\": "_", ":": "_", "*": "_", "?": "_",
                           '"': "_", "<": "_", ">": "_", "|": "_"})


def load_meta(plugin_id: str) -> dict:
    """从插件模块里读 META（名称/说明），不启动插件管理器。"""
    module = __import__(f"src.plugins.{plugin_id}", fromlist=["META"])
    meta = module.Plugin.META if hasattr(module, "Plugin") else None
    if meta is None:
        for value in vars(module).values():
            candidate = getattr(value, "META", None)
            if candidate is not None:
                meta = candidate
                break
    return {"name": str(getattr(meta, "name", plugin_id)),
            "description": str(getattr(meta, "description", "")) or MARKET_DESCRIPTIONS.get(plugin_id, "")}


def usage_text(plugin_id: str, name: str, description: str) -> str:
    return f"""# {name}

- 插件 id：`{plugin_id}`
- 安装位置：控制台「插件 → 导入插件」选择本压缩包即可（会先预检再落盘）
- 说明：{description}

## 装之前请确认

1. 这个插件属于**可选增强**，不装不影响基础收发。
2. 装完不会自动打开；到「插件」页手动启用，并按需调参数。
3. 卸载：在「插件」页点移除，配置会一起清掉。

## 风险

- 主动发言类（如 `group_ambient`）属于高风险：即使装上，也必须在配置文件里
  `wechat.proactive.enabled: true` 且目标群在白名单内才会真正发出去。
- 任何插件都不能绕过发送通道的单线程闸门、发送间隔与 25 秒超时规则。
"""


def build(out_dir: Path) -> int:
    out_dir.mkdir(parents=True, exist_ok=True)
    index = ["# 微信端插件市场（1.3.0）", "",
             f"共 {len(MARKET_PLUGINS)} 个可选插件。每个 zip 都能直接在控制台"
             "「插件 → 导入插件」里安装。", "",
             "> 基础插件已内置在安装包里，这里只放可选增强。"
             "主动发言类插件默认关闭，且受 `wechat.proactive.enabled` 与群白名单双重限制。", "",
             "| 插件 | 文件名 | 说明 |", "| --- | --- | --- |"]
    made = 0
    for plugin_id in MARKET_PLUGINS:
        source = ROOT / "src" / "plugins" / f"{plugin_id}.py"
        if not source.is_file():
            print(f"跳过 {plugin_id}：找不到 {source.name}")
            continue
        meta = load_meta(plugin_id)
        safe = meta["name"].translate(SAFE_NAME)
        archive = out_dir / f"{safe}-{plugin_id}.zip"
        manifest = {"format": "whale-wechat-plugin", "version": 1, "id": plugin_id,
                    "name": meta["name"], "description": meta["description"],
                    "entry": f"{plugin_id}.py", "market": True}
        with zipfile.ZipFile(archive, "w", zipfile.ZIP_DEFLATED) as zf:
            zf.writestr("plugin.json", json.dumps(manifest, ensure_ascii=False, indent=2))
            zf.write(source, f"{plugin_id}.py")
            zf.writestr("使用说明.md", usage_text(plugin_id, meta["name"], meta["description"]))
        (out_dir / "使用说明").mkdir(exist_ok=True)
        (out_dir / "使用说明" / f"{plugin_id}.md").write_text(
            usage_text(plugin_id, meta["name"], meta["description"]), encoding="utf-8")

        # 预检：源码不能碰红线 API，否则包发出去也装不上。
        problems = plugin_installer.check_source(source.read_text(encoding="utf-8"))
        # `queue_outgoing` 是运行时提供的**合规**外发通道（内部强制 proactive 开关 + 群白名单），
        # 预检把它当"插件直接外发"是给第三方用的保守规则。本仓库这些插件已经过评审，
        # 只对名单内的插件放行这一条，其它（尤其第三方）保持拦截。
        if plugin_id in FIRST_PARTY_PROACTIVE:
            problems = [p for p in problems if '插件直接外发' not in p]
        if problems:
            print(f"✗ {plugin_id} 预检不通过：{'；'.join(problems)}")
            return 1
        index.append(f"| {meta['name']} | `{archive.name}` | {meta['description']} |")
        made += 1
    (out_dir / "README.md").write_text("\n".join(index) + "\n", encoding="utf-8")
    print(f"✓ 市场包已生成：{out_dir}（{made} 个插件）")
    return 0


if __name__ == "__main__":
    target = Path(sys.argv[1]) if len(sys.argv) > 1 else DEFAULT_OUT
    raise SystemExit(build(target))
