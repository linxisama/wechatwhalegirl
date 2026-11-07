# DSH_NEXT — 鲸鱼娘微信控制台 1.3.96 交接

仓库：D:\聊天机器人\微信机器人（**不是** D:\微信机器人）。
HEAD：见 `git log -1`，工作树 clean。当前版本 `1.3.96`。

## 本轮已交付（1.3.90 → 1.3.93，10 个提交）
| 提交 | 内容 |
|---|---|
| `af1dd73` | 1.3.90 后台失败出口全部带码、标题读取去早停、群成员数后缀剥离 |
| `d334b26` | 1.3.91 自动更新：自动下载 + SHA256 校验（不自动安装）、24h 重查、高级设置折叠区 |
| `b1c3fe6` | 1.3.92 表情包描述不再挤爆提示词预算（1329 字 → 517 字） |
| `cc31fda` | 日志：并发 handler 竞态、凭据落盘泄漏、非法路径抛异常 + `logs/index.jsonl` |
| `cd0771e` | DSH_NEXT 交接文件 |
| `4e1278c` | 人设隔离：bulk/clear/200 条淘汰/overview 四处跨人设泄漏 + 手动记忆预算 |
| `bd8bef6` | UI：附件 tooltip 与空态、会话存档空态竞态、解除暂停防连点 |
| `7cf6b5c` | 记忆与 token：估算器偏差修正（MAPE 134.6%→22.5%）、分块账本、按得分淘汰 |
| `79b5986` | 人格：`_load_style_file` 签名修复、同名冲突留痕、残留状态清理 |
| `b5ef45b` | DSH 连接：错误码分诊、发现缓存单飞、等待者预算与退避 |
| `edf97f6` | 1.3.93 版本号 + 九份文档 + `交付报告-1.3.93.md` |
| （1.3.94） | 五处接线：`record_round` 补 `plan=`、淘汰改按得分、`recall` 接 `rank()`、`build_history` 改 `continue`、`_conflicts` 写进日志；两处陈旧文档订正；`交付报告-1.3.94.md` |
| （1.3.95） | 学习系统优化：expression 自动批准开关（`expressionApproveHits`，默认 0 关）、200 条淘汰插入 `hits DESC`、`view()` N+1 203→4、`overview()` 100→2；`交付报告-1.3.95.md` |
| `f940956` | docs：七份文档补 1.3.94 与 1.3.95 两段 |
| （1.3.96） | 人设冲突界面可见：`GET /api/personas` 增 `conflicts`（`hasattr` 兜底）、人设页 `_apply_personas` 渲染计数 + tooltip 明细；`交付报告-1.3.96.md` |

1.3.96 全量回归：`Ran 4427 tests`（A 半 2198 + B 半 2229，`FAILED (failures=2)`），两例均为 `tests/test_cross_machine_paths151.py` 的长路径环境依赖（本机 TEMP 仅 33 字符），与改动无关；把 TEMP 指到 166 字符目录后该模块 16 例全 OK。例数基线 1.3.90 = 4203 → 1.3.95 = 4413 → 1.3.96 = 4427（+14，恰为 `test_persona_conflict_gui1396.py` 的用例数）。

## 1.3.94 已接线（原「待接线」清单已清空）
1. `src/wechat_app.py:2275-2276` `record_round(...)` 已补 `plan=locals().get('prompt_plan')`。
2. `src/chat_state.py` `remember()` 已换 `memory_maintenance.evict()`（hits 0.5 / 新鲜度 0.3 / 具体度 0.2，`EVICT_GRACE=3600` 保护刚写入行）。
3. `src/chat_state.py` `recall()` 已换 `memory_rank.rank()`（排序 + 去重 + 统计），传入 dict 需带 `hits`/`updated`（`field_of(item,"hits",5)`/`field_of(item,"updated",7)`）。
4. `src/chat_state.py` `build_history` 超预算分支已由 `break` 改 `continue`；**未**引入相关度排序（该函数无 `query` 参数）。
5. `src/style_manager.py` 新增 `conflicts()`；`src/wechat_app.py:320` 在 `load_all()` 后写日志；1.3.96 起 `GET /api/personas` 返回 `conflicts`，`wechat_gui.py` 的 `PersonaTab._apply_personas` 把计数写进 `active_persona_hint`、明细进 tooltip。

## 待用户决定
- 第三层清理 36.16GB 交付物历史（`installer/` 1.3.85 及更早 143 个 Setup、`发布/` 1.3.30 及更早 53 个目录、`release/` 1.3.86 及更早 20 个目录）未删，且整个删除动作未经用户确认。
- `.tmp-token.txt`（40 字节，`gho_Uw…` 前缀，从未提交，`.gitignore:45` 已忽略）去留与是否撤销该 token。
- 两处固定 2.0 秒预算（`src/wechat_background.py:88` / `:308`）是否改自适应。
- 两项策略门槛：后台最小化是否自动恢复 / 后台是否可清草稿。

## 文档
1.3.94 已修掉 `diy-src/03-常见改造任务.md` 与 `diy-src/04-已知限制.md` 里各一处「（只提示发现新版本，不自动下载安装）」，改为「（1.3.93 起自动下载并校验，但仍不自动安装）」。

## 环境（必须照做）
- run_code 的 `process.env` 为空。跑测试：
```python
import json, subprocess
env = json.load(open(r'D:\聊天机器人\微信机器人\.tmp-env.json', encoding='utf-8'))
env['PYTHONPATH'] = r'D:\聊天机器人\微信机器人\tests'
env['PYTHONIOENCODING'] = 'utf-8'
env['QT_QPA_PLATFORM'] = 'offscreen'   # 仅 GUI 测试
subprocess.run([r'D:\聊天机器人\微信机器人\.venv-wechat\Scripts\python.exe','-u','-m','unittest','discover','-s','tests'],
               cwd=r'D:\聊天机器人\微信机器人', env=env)
```
- 全量回归约 506–536 秒，`timeoutMs` 给 580000 仍可能被 spawnSync 杀掉（实测 580.2s 被杀、日志无结论）。可靠做法：按模块清单分片跑（`.tmp-half-a.json` 166 个、`.tmp-half-b1.json` 83 个、`.tmp-half-b2.json` 82 个），配 `.tmp-run-mod.py` 逐模块写进度日志，单片 140–250 秒。
- PowerShell 脚本必须全 ASCII + UTF-8 BOM 写盘，否则 PS 5.1 按 GBK 读报 `ParserError: 字符串缺少终止符`。
- 提交必须带身份：`git -c user.name=linxisama -c user.email=2019117802@qq.com commit -F <msg>`。
- 行尾：`.gitattributes` 是 `* -text`；`src/*.py` 与 `wechat_gui.py` 是 **LF**，`.md` 是 **CRLF**。例外（HEAD 本就是 CRLF）：`src/logging_setup.py`、`src/prompt_budget.py`、`src/style_manager.py`。
- 测试模块名要精确：`tests/test_dsh_autoconnect166.py` 不是 `test_dsh_autoconnect`。
- 本会话 `compress`/`acp_status`/`decompress` 在 run_code 里全部 undefined（77 个工具里没有），无法主动压缩上下文。
