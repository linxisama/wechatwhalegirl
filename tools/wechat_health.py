"""微信接入体检（**只读**，不会发送任何消息）。

用法（建议以管理员身份运行）：
    .venv-wechat\\Scripts\\python.exe tools\\wechat_health.py
    .venv-wechat\\Scripts\\python.exe tools\\wechat_health.py --backend wechatauto --db-dir "C:\\Users\\你\\Documents\\xwechat_files"

它会做什么：
1. 报告本机检测到的微信客户端进程、可用的后端、当前是否管理员；
2. 连接所选后端（`wechatauto` 会读取微信本地数据库以获取账号与会话列表 —— **只读**）；
3. 打印账号、数据目录、最近的会话与群列表（各取前 15 条）。

它**不会**发送消息、不会点开表情面板、不会切换会话、不会读写你的聊天记录之外的数据。
发送能力需要真机验证时，请用控制台的「测试微信」按钮或直接发一条消息给文件传输助手。
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))


def main() -> int:
    parser = argparse.ArgumentParser(description="微信接入只读体检")
    parser.add_argument("--backend", default="auto",
                        help="auto / wechatauto / wxauto4 / wxauto / mock")
    parser.add_argument("--db-dir", default="", help="微信 4.x 数据目录，留空自动探测")
    args = parser.parse_args()

    from src.wechat_bridge import WeChatBridge, _client_executable, _module_available, is_admin

    print("=" * 64)
    print("环境")
    print("=" * 64)
    print(f"微信客户端进程        : {_client_executable() or '（没检测到，请确认微信已打开并登录）'}")
    print(f"wechatauto 可用       : {_module_available('wechatauto')}")
    print(f"wxauto4 可用          : {_module_available('wxauto4')}")
    print(f"wxauto(3.9) 可用      : {_module_available('wxauto')}")
    print(f"当前进程是管理员      : {is_admin()}")

    bridge = WeChatBridge(data_dir=ROOT / "data/wechat-only", backend=args.backend,
                          db_dir=args.db_dir, note=lambda text: None)
    print(f"将使用的后端          : {bridge.detect_backend()}")

    print()
    print("=" * 64)
    print("连接（只读）")
    print("=" * 64)
    ok, detail = bridge.connect()
    print(f"连接结果              : {'成功' if ok else '失败'} —— {detail}")
    if not ok:
        print()
        print("排错建议：")
        print("  1) 确认微信已打开并登录（4.1.12 及以上建议用 wechatauto 后端）")
        print("  2) 用管理员身份运行本脚本（右键 PowerShell → 以管理员身份运行）")
        print("  3) 加 --db-dir 指定微信 4.x 数据目录，例如")
        print('     --db-dir "C:\\Users\\你的用户名\\Documents\\xwechat_files"')
        print("  4) 重跑一次「一键安装依赖.bat」确认 wechatauto-replica 已安装")
        return 1

    info = bridge.status()
    print(f"账号                  : {info['account'] or '（未识别）'}")
    print(f"账号 wxid             : {info['self_id'] or '（未识别）'}")
    print(f"数据库目录            : {info['db_dir'] or '（自动探测）'}")
    if info.get("degraded"):
        print(f"注意                  : {info['degraded']}")

    sessions = bridge.list_sessions(limit=15)
    print()
    print(f"最近会话（前 {len(sessions)} 条）：")
    for item in sessions:
        kind = "群" if item["kind"] == "group" else "人"
        print(f"  [{kind}] {item['name'] or item['username']}"
              f"  未读={item['unread']}  {item['summary'][:20]}")

    groups = bridge.list_groups()
    print()
    print(f"微信群（共 {len(groups)} 个，前 15 条）：")
    for group in groups[:15]:
        print(f"  {group['name'] or group['username']}")

    bridge.disconnect()
    print()
    print("体检结束。这个脚本只做了只读操作；发送能力请在控制台里点「测试微信」验证。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
