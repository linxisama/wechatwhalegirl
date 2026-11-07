# DSH_NEXT — 鲸鱼娘微信控制台 1.3.103 交接

仓库：D:\聊天机器人\微信机器人（**不是** D:\微信机器人）。
HEAD：见 `git log -1`，工作树 clean。当前版本 `1.3.103`。

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
| （1.3.97） | 名录房间数加 LRU 上限（`MemberDirectory(max_rooms=200)`，observe 重插到队尾、从队头淘汰、当前房间显式保护）；新增 `tests/test_member_directory_cap1397.py` 8 例；`交付报告-1.3.97.md` |
| （1.3.98） | 四处审计缺陷：`computer_helper.py:143` 错码 `00000007b`→`0000007b`（0x7B 专项模板原本永远选不中）、`:319` 强信号词加查 `compact`（`INACCESSIBLE_BOOT_DEVICE` / `inaccessible boot device` 原本漏判）、`sandbox_doctor.py` 删掉两个重复且无人调用的 `_sandbox_windows`、`memory_maintenance.py` 新增 `_commit()` 让 commit 失败时回滚并按 0 报告（`maintain` 增 `persisted` 字段，`dialogue_management.py:148` 据此返回 `success: False`）；复核又查出一处回归：`evict` 的提交/回滚会连带丢掉 `ChatState.remember` 还没落盘的 INSERT，改为 `evict(commit=False)` 由 caller 唯一提交；新增 `tests/test_audit_fixes1398.py` 16 例；`交付报告-1.3.98.md` |
| （1.3.99） | 表情库三处淘汰缺陷：`sticker_library.py:299-305` 单图超容量上限原本会把自己淘汰掉、整个收藏夹清空并抛 `KeyError`（改为落图前 `raise ValueError`，库不动）、`_evict()` 新增 `keep` 参数让刚入库那条不被自己挤掉（`:352-372`）、`_evict()` 改为只算不删并返回 `(entries, victims)`，`put()` 在索引落盘成功后才删旧图（`:338-345`，原先索引写失败会留下悬空条目）、新增 `_cap_bytes()` 统一容量算法（`:348-350`）；`sticker_api.py:134` 报错改用 `MAX_BYTES`、`:24` 注释改「单次导入总量上限」；契约 `WECHAT-CONTRACT.md:913` / `:1317` 的 6MB 改 12MB；新增 `tests/test_sticker_evict1399.py` 6 例；`交付报告-1.3.99.md` |
| （1.3.100） | 坏 JSON 一律 400：`wechat_app.py:2595-2605` 在 `create_app` 里注册 `@app.exception_handler(JSONDecodeError)` 与 `@app.exception_handler(UnicodeDecodeError)`，两者都回 `JSONResponse({success: False, detail: BAD_JSON_MESSAGE, message: 同句}, status_code=400)`，文案常量在 `:72`、`from json import JSONDecodeError` 在 `:26`；17 个直接 `await request.json()` 的写接口原本 500（空 body 与非法 UTF-8 同样 500）；`persona_files.py:92-100` 把 `body=await request.json()` 单独包 `try/except (ValueError, UnicodeError)` 抛 `HTTPException(400, '导入请求不是有效的 JSON')`，不再把 `Expecting property name enclosed in double quotes` 英文原文当 detail 返回；新增 `tests/test_bad_json_1400.py` 9 例；`交付报告-1.3.100.md` |
| （1.3.101） | 合法 JSON 但不是对象时：`persona_files.py:121-127`（`:125` `body.get('data')`）与 `wechat_app.py:3327-3334`（`:3331` `body.get('password')`）抛 `AttributeError: 'list' object has no attribute 'get'` 直接 500；六个 plugins 路由（`:3166` inspect、`:3177` install、`:3192` batch-inspect、`:3205` batch-install、`:3230` inspect-local、`:3241` install-local）的 `AttributeError` 被 `safe_error` 包装成「操作失败（AttributeError）」泄漏类名。修法：逐处 `isinstance(body, dict)` 判断（不为 `AttributeError` 注册全局 handler，免得吞掉真 bug），六个 plugins 文案统一「请求内容必须是 JSON 对象」；新增 `tests/test_json_body_1401.py` 6 例；`交付报告-1.3.101.md` |
| （1.3.102） | 审计出两处非原子写：`src/plugins/manager.py:237-246` `_save_custom()` 与 `src/plugins/__init__.py:166-171` `PluginBase.save_state()` 原本都用 `Path.write_text`（先截断再写），写到一半失败留下半截 JSON；读取侧 `custom_plugin_ids()`（`manager.py:224-235`）与 `load_state()`（`plugins/__init__.py:159-164`）为「坏了不崩」静默回落成空清单 / 默认状态，用户装的插件全部消失、插件状态被重置且无任何报错。改用仓库既有的 `persistence.atomic_text`（清单 `backup=True`、状态 `backup=False`）；新增 `tests/test_atomic_state1402.py` 6 例（`mock.patch.object(persistence.os, "fsync", side_effect=OSError(28,...))` 注入写失败，改前 2 例真红 `AssertionError: OSError not raised`）；`交付报告-1.3.102.md` |
| （1.3.103） | 插件清单契约升级：`api_version` 整数化（`SUPPORTED_API_VERSIONS` 由 `("1",)` 改为 `(1, 2)`，新增 `normalize_api_version()` 接受 `1`/`"1"`/`1.0`、拒绝 `"1.0"`/`1.5`/`true`/`null`）+ 严格档 13 字段（`api_version >= 2` 缺一即非法，兼容档维持只判死 id/name/version）+ `CAPABILITY_VOCABULARY` 能力词表（10 钩子 + `settings`/`helper`）；10 个种子包全升严格档、`capabilities` 按真实实现填；`tools/make_plugin_packs.py` 同步 `API_VERSION = 2`；新增 `docs/插件契约改造规格-1.3.103起.md` 与 `tests/test_plugin_contract_1103.py` 20 例 |

1.3.101 全量回归：`Ran 4472 tests`（三片 1369 + 1525 + 1578，547.1s，`FAILED (failures=2, skipped=5)`），两例均为 `tests/test_cross_machine_paths151.py` 长路径（本机 TEMP 仅 33 字符，历史上一直失败，单独跑该模块稳定复现 `FAILED (failures=2)`）。

1.3.102 全量回归：`Ran 4478 tests`（三片 1536 + 1391 + 1551，739.3s，`FAILED (failures=2, skipped=5)`），两例均为 `tests/test_cross_machine_paths151.py` 长路径（本机 TEMP 仅 33 字符，历史上一直失败，单独跑该模块稳定复现 `FAILED (failures=2)`）。例数基线 1.3.90 = 4203 → 1.3.96 = 4427 → 1.3.98 = 4451 → 1.3.99 = 4457 → 1.3.100 = 4466 → 1.3.101 = 4472 → 1.3.102 = 4478（+6 = 本批新增 6 例）。

1.3.100 全量回归：`Ran 4466 tests`（三片 1595 + 1453 + 1418，629.9s，`FAILED (failures=3, skipped=5)`），三例均为环境/时序依赖：两例 `tests/test_cross_machine_paths151.py` 长路径（本机 TEMP 仅 33 字符，历史上一直失败，单独跑该模块稳定复现 `FAILED (failures=2)`），一例 `tests/test_title_template190.py::PerformanceTest::test_rank_5600_names_under_three_seconds` 在分片并行下抖动（单独跑该模块两次均 `Ran 21 tests` / OK）。例数基线 1.3.90 = 4203 → 1.3.96 = 4427 → 1.3.98 = 4451 → 1.3.99 = 4457 → 1.3.100 = 4466（+9 = 本批新增的 9 例）。

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
- 1.3.100 本批测试：`tests.test_bad_json_1400` 单独 `Ran 9 tests` / `OK`；与 7 个相关模块一起 `Ran 151 tests in 81.415s` / `OK`；全量回归 `Ran 4466 tests`（三片 1595 + 1453 + 1418，629.9s），唯三非通过项均为环境/时序依赖（未打包、未跑 selfcheck）。
- 1.3.101 本批测试：`tests.test_json_body_1401` 单独 `Ran 6 tests` / `OK`；与 `test_bad_json_1400`、`test_connection_management_fixes`、`test_plugin_market_routes`、`test_persona_import_152`、`test_wechat_app_flow`、`test_install_guard1351`、`test_plugin_installer` 一起 `Ran 185 tests in 64.012s` / `OK`；全量回归 `Ran 4472 tests`（三片 1369 + 1525 + 1578，547.1s），唯二非通过项为 `tests/test_cross_machine_paths151.py` 长路径（未打包、未跑 selfcheck）。
- 1.3.102 本批测试：`tests.test_atomic_state1402` 单独 `Ran 6 tests in 0.594s` / `OK`；与 `test_plugin_installer`、`test_plugin_market_routes`、`test_web_context`、`test_plugin_guardrails147`、`test_install_guard1351` 一起 `Ran 139 tests in 31.103s` / `OK`；全量回归 `Ran 4478 tests`（三片 1536 + 1391 + 1551，739.3s），唯二非通过项为 `tests/test_cross_machine_paths151.py` 长路径（环境依赖，与本批无关；未打包、未跑 selfcheck）。
- **清理教训（1.3.103 实测）**：`.tmp-*` 通配删除前必须 `git grep` 一下文件名在 `tests/` 与 `src/` 里的引用。`.gitignore` 里的 `.tmp-*` 只代表「不纳入版本控制」，不代表「可以随便删」——`.tmp-ink192.png` / `.tmp-ink-191.png` / `.tmp-ink-190b.png` 就是 `tests/test_title_template190.py` 的真机夹具，被删后 20 例 ERROR，且 git 无记录、回收站为空、全盘无同名文件，**不可恢复**。该模块现已改为夹具缺失时显式 skip（`require_fixture()`），14 例 skip、7 例合成图用例照常跑。
- 1.3.103 本批测试：`tests.test_plugin_contract_1103` 单独 `Ran 20 tests in 0.137s` / `OK`；与 `test_plugin_packs163`、`test_plugin_manifest148`、`test_plugin_installer`、`test_plugin_catalog`、`test_plugin_market_routes`、`test_plugin_lifecycle`、`test_plugin_isolation_197`、`test_plugin_settings`、`test_plugin_files`、`test_plugin_batch`、`test_plugin_layering_freeze`、`test_existing_plugins_batch` 共 13 个模块一起跑 `Ran 285 tests in 17.531s` / `OK`；全量回归 `Ran 4498 tests`（三片 1603 + 1493 + 1402，skipped=21），唯二非通过项为 `tests/test_cross_machine_paths151.py` 长路径（环境依赖，与本批无关）。
