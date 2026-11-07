# DSH_NEXT — 鲸鱼娘微信控制台 1.3.93 交接

仓库：D:\聊天机器人\微信机器人（**不是** D:\微信机器人）。
HEAD：`edf97f6`，工作树 clean。

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

累计 66 files changed, 6804 insertions(+), 362 deletions(-)；新增 9 个测试模块共 177 例。
全量回归 `Ran 4380 tests in 506.425s` / `FAILED (failures=2, skipped=5)`，两例均为 `tests/test_cross_machine_paths151.py` 的长路径环境依赖（本机 TEMP 仅 33 字符），与改动无关。基线 1.3.90 为 4203 例。

## 待接线（实现已备好，需放开文件边界）
1. `src/wechat_app.py:2249-2252` `record_round(...)` 加 `plan=prompt_plan` —— 分块账本落地的最后一公里，加上后每轮真实产生分块数据。
2. `src/chat_state.py:116-117` 换 `memory_maintenance.evict()`（按 hits 0.5 / 新鲜度 0.3 / 具体度 0.2 打分，替代纯 FIFO）。
3. `src/chat_state.py:151-166` `recall()` 换 `memory_rank.rank()`（排序 + 去重 + 统计）。
4. `src/chat_state.py:198-222` `build_history` 按相关度挑选，替代从后往前塞。
5. `_conflicts` 需在 `src/wechat_app.py` 暴露接口 + GUI 展示 —— 目前冲突只进内存和日志，界面看不到；仓库自带 `styles/tsundere_whale_girl.yaml` 与 `styles/custom/tsundere_whale_girl.yaml` 同名，每次启动都会产生一条用户看不见的冲突记录。

## 待用户决定
- 第三层清理 36.16GB 交付物历史（`installer/` 1.3.85 及更早 143 个 Setup、`发布/` 1.3.30 及更早 53 个目录、`release/` 1.3.86 及更早 20 个目录）未删，且整个删除动作未经用户确认。
- `.tmp-token.txt`（40 字节，`gho_Uw…` 前缀，从未提交，`.gitignore:45` 已忽略）去留与是否撤销该 token。
- 两处固定 2.0 秒预算（`src/wechat_background.py:88` / `:308`）是否改自适应。
- 两项策略门槛：后台最小化是否自动恢复 / 后台是否可清草稿。

## 待补文档
`diy-src/03-常见改造任务.md:172` 与 `diy-src/04-已知限制.md:174` 各有一处「（只提示发现新版本，不自动下载安装）」陈旧句，1.3.93 时因 10 文件上限未改。

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
- 全量回归约 506 秒，需 `timeoutMs` ≥ 560000，`maxBuffer` ≥ 48MB。
- PowerShell 脚本必须全 ASCII + UTF-8 BOM 写盘，否则 PS 5.1 按 GBK 读报 `ParserError: 字符串缺少终止符`。
- 提交必须带身份：`git -c user.name=linxisama -c user.email=2019117802@qq.com commit -F <msg>`。
- 行尾：`.gitattributes` 是 `* -text`；`src/*.py` 与 `wechat_gui.py` 是 **LF**，`.md` 是 **CRLF**。例外（HEAD 本就是 CRLF）：`src/logging_setup.py`、`src/prompt_budget.py`、`src/style_manager.py`。
- 测试模块名要精确：`tests/test_dsh_autoconnect166.py` 不是 `test_dsh_autoconnect`。
- 本会话 `compress`/`acp_status`/`decompress` 在 run_code 里全部 undefined（77 个工具里没有），无法主动压缩上下文。
