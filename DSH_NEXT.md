# DSH_NEXT — 鲸鱼娘微信控制台 · 12 小时自主优化交接

仓库：D:\聊天机器人\微信机器人（**不是** D:\微信机器人）。

## 已提交
- `af1dd73` 1.3.90 后台失败出口全部带码、标题去早停、群成员数后缀剥离
- `d334b26` 1.3.91 自动更新：自动下载+校验、24h 周期重查、高级设置折叠区
- `b1c3fe6` 1.3.92 表情包描述不再挤爆提示词预算

## 表情包 bug 已闭环（用户点名的缺陷）
20 条×120 字极端场景：extra_system **1329 字 → 517 字**，保留 3 条 + 「已按相关度省略 18 条」；正常场景 353 字不截断。
- `src/prompt_budget.py` :40-53 `STICKER_SECTION_CAP=800`/`STICKER_MEANING_CHARS=40`/`STICKER_MAX_ITEMS=3`；:114/:120/:126 配置读取；:132 `clip_note`；:141 `sticker_cut_count`；:253 `PromptPlan.sticker_cut`；:267-268 `log_line()`
- `src/plugins/image_library.py` :30-50 `sticker_section(notes, cap, dropped=0)`；:243-257 `on_pre_ai`
- `src/plugins/sticker_memory.py` :157-182 `known_notes(..., meaning_chars=None, max_items=None)`；:298-310 `notes_for(..., config=None)`
- `tests/test_sticker_budget1392.py` 17 例；复核 `Ran 92 tests OK`

## 工作树未提交（各子代理半成品）
- `M DSH_NEXT.md`
- ` M src/dsh_api.py`
- ` M src/dsh_autoconnect.py`
- ` M src/dsh_discovery.py`
- ` M src/gui/agent_attachments.py`
- ` M src/gui/conversation_archive.py`
- ` M src/gui/safety_panel.py`
- ` M src/gui/send_panel.py`
- ` M src/local_learning.py`
- ` M src/logging_setup.py`
- ` M src/style_manager.py`
- `?? tests/test_logging_opt1392.py`

## UI 子代理 016beb6d 中途失败
改了 4 个文件、**没建测试**：src/gui/agent_attachments.py、src/gui/conversation_archive.py、src/gui/safety_panel.py、src/gui/send_panel.py（合计 +58/-11）。
待决：派新子代理补测试并复核，或 `git checkout` 丢弃重来。

## 仍在跑
10439a73 人设隔离+独立学习区 / a489eb23 日志 / c1a4beee DSH 连接 / 3eceb50b 记忆+token / fc382d36 人格冲突

## 待办
1. 收齐后逐个独立复核（读 diff + 跑新测试 + 跑它声称保护的既有模块）
2. 版本号 `src/runtime_identity.py:5` → 1.3.93（1.3.92 已被表情包修复占用）
3. 九份文档：`CHANGES.md`、`交付报告-1.3.92.md`、`功能对照与使用说明.md`、`微信版使用说明.md`、`diy-src/00-先读我.md`、`01-喂给AI的指令.md`、`02-项目地图.md`、`05-自测与打包.md`、`instructions/WECHAT-CONTRACT.md`
4. 全量回归（基线 `Ran 4203 tests in 464.639s`，2 例长路径环境失败 `tests/test_cross_machine_paths151.py:85`/`:149`）
5. 最终提交

## 环境（必须照做）
- run_code 的 `process.env` 为空。跑测试：
```python
import json, subprocess
env = json.load(open(r'D:\聊天机器人\微信机器人\.tmp-env.json', encoding='utf-8'))
env['PYTHONPATH'] = r'D:\聊天机器人\微信机器人\tests'
env['PYTHONIOENCODING'] = 'utf-8'
env['QT_QPA_PLATFORM'] = 'offscreen'   # 仅 GUI 测试
subprocess.run([r'D:\聊天机器人\微信机器人\.venv-wechat\Scripts\python.exe','-u','-m','unittest','tests.xxx'],
               cwd=r'D:\聊天机器人\微信机器人', env=env)
```
- PowerShell 脚本必须全 ASCII + UTF-8 BOM 写盘，否则 PS 5.1 按 GBK 读报 `ParserError: 字符串缺少终止符`。
- 提交必须带身份：`git -c user.name=linxisama -c user.email=2019117802@qq.com commit -F <msg>`（否则 `Author identity unknown`）。
- 行尾：`.gitattributes` 是 `* -text`；**`src/*.py` 与 `wechat_gui.py` 是 LF，`.md` 是 CRLF**。
- 本会话 `compress`/`acp_status`/`decompress` 在 run_code 里全部 undefined（77 个工具里没有），无法主动压缩上下文。

## 未决（等用户）
- 第三层清理 36.16GB 交付物历史未删，且删除动作未经用户确认（两次 ask_user_question 均超时）。
- `.tmp-token.txt`（40 字节 `gho_Uw…` 前缀，从未提交）去留。
- 两处固定 2.0 秒预算（`src/wechat_background.py:88`/`:308`）是否改自适应。
- 两项策略门槛（后台最小化自动恢复 / 后台清草稿）。
