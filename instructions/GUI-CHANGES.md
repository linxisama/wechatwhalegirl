# `wechat_gui.py` 改造清单（QQ 控制台 → 微信控制台）

`wechat_gui.py` 现在是 `qq_gui.py` 的原样拷贝（2307 行）。它**只是本机 HTTP 客户端**，
不含任何平台协议逻辑，所以改造量集中在「身份 / 端口 / 目录 / 设置页字段 / 状态页分支」。
下面每一条都必须落到代码里；行号只是参考，**以字符串为准搜索**。

后端契约见 `instructions/WECHAT-CONTRACT.md`；后端已实现为 `src/wechat_app.py`
（`WeChatRuntime` / `create_app`），端口 **8091**，配置 `config.wechat.yaml`，
数据目录 `data/wechat-only/`。

## A. 身份与路径（必须）

| # | 位置 | 现状 | 改成 |
| --- | --- | --- | --- |
| A1 | 模块 docstring | `鲸鱼娘 QQ 机器人 · 原生 Windows 控制台` / `http://127.0.0.1:8090/api/` | `鲸鱼娘 微信机器人 · 原生 Windows 控制台` / `http://127.0.0.1:8091/api/` |
| A2 | `FROZEN` 分支的 `ROOT` | `WhaleGirlQQ` | `WhaleGirlWeChat` |
| A3 | `BASE_URL` | `http://127.0.0.1:8090` | 从 `src.runtime_identity.PORT` 取，即 `http://127.0.0.1:8091` |
| A4 | `backend_error()` 文案 | 「检测到旧版或其他后台占用8090…」 | 用 `PORT` 拼：`f'检测到旧版或其他后台占用 {PORT}…'`（`backend_identity_error` 已由后端模块负责，直接调用即可） |
| A5 | `TitleBar` 标题 | `鲸鱼娘 · QQ 控制台 {APP_VERSION}` | `鲸鱼娘 · 微信控制台 {APP_VERSION}` |
| A6 | `main()` 里 `SetCurrentProcessExplicitAppUserModelID` | `whalegirl.qq.console` | `whalegirl.wechat.console` |
| A7 | `main()` 里 `setApplicationName` | `鲸鱼娘 QQ 控制台` | `鲸鱼娘 微信控制台` |
| A8 | `_start_backend()` 的子进程命令 | `src.qq_app:create_app --port 8090` | `src.wechat_app:create_app --port {PORT}` |
| A9 | `_start_backend_inprocess()` | `from src.qq_app import QQRuntime`；`config.qq.yaml`；`data/qq-only/messages.sqlite3`；`uvicorn.run(..., port=8090)` | `from src.wechat_app import WeChatRuntime`；`config.wechat.yaml`；`data/wechat-only/messages.sqlite3`；`port=PORT`；线程名 `wechat-backend` |
| A10 | `_start_backend()` 的裸探测 URL | `f'{BASE_URL}/api/status'` | 保持（`BASE_URL` 已改） |

## B. 设置页 `SettingsTab`（必须重写字段）

`populate()` / `collect()` / `_toggle_mode()` 三处要一起改，字段与后端
`src/wechat_app.py` 的 `class Settings` **一一对应**：

| 界面标签 | 控件属性 | 类型 | payload 键 | 说明 |
| --- | --- | --- | --- | --- |
| `微信后端` | `backend` | QComboBox | `backend` | 选项：`自动选择（推荐）`→`auto`、`wechatauto · 微信 4.1.12+（推荐）`→`wechatauto`、`wxauto4 · 微信 4.0.x`→`wxauto4`、`wxauto · 微信 3.9.x`→`wxauto`、`模拟模式（不连微信）`→`mock` |
| `微信数据库目录` | `db_dir` | QLineEdit | `db_dir` | placeholder：`留空=自动探测；例如 C:\Users\你\Documents\xwechat_files` |
| `回复群列表` | `groups` | QTextEdit 高 90 | `groups`（按行 split 成 list） | placeholder：`每行一个群名（与微信里显示的一致，也可填 xxx@chatroom）；留空=所有群` |
| （按钮）`读取微信群列表` | `btn_load_groups` | QPushButton（secondary 样式） | — | 调 `api_get("wechat/groups")`，把群名填进 `groups`：**追加不覆盖**，已存在的跳过；失败时 `save_lbl` 提示原因 |
| `回复私聊消息` | `listen_private` | QCheckBox | `listen_private` | 默认勾选 |
| `API 地址` | `api_url` | QLineEdit | `api_url` | 保持不变 |
| `API Key` | `api_key` | QLineEdit（Password） | `api_key` | placeholder `API Key（留空保留已保存值）` |
| `模型名称` | `model_name` | QLineEdit | `model` | 保持不变 |
| `接口格式` | `api_style` | QComboBox | `api_style` | auto / openai / anthropic |
| （只读回显） | `secrets_lbl` | QLabel | — | 只显示 `已保存密钥：API ✓`（微信不需要 AppSecret/令牌） |

要点：

- **删掉** QQ 的 `mode` / `app_id` / `app_secret` / `sandbox` / `onebot_token` 控件、
  `official_frame` / `onebot_frame` 两个分组、`_toggle_mode()`、以及 `连接地址：ws://…/qq/onebot …` 那行说明。
- 分组标题：`01 · 微信接入`、`02 · 模型与 API`。
- `populate()` 读：`backend`、`db_dir`、`groups`、`listen_private`、`api_url`、`model`、`api_style`、`api_key_set`。
- `collect()` 发：`backend`、`db_dir`、`groups`、`listen_private`、`api_url`、`api_key`、`model`、`api_style`（共 8 个键）。
- 「微信后端」下拉要加一行小字提示：`wechatauto 需要微信 4.1.12+ 且建议以管理员身份运行控制台；模拟模式不发送任何消息。`

## C. 控制台页 `ConsoleTab`（必须）

| # | 现状 | 改成 |
| --- | --- | --- |
| C1 | 状态卡标题 `QQ 接入` | `微信接入` |
| C2 | `refresh()` 读 `official{}` / `onebot{}` 两个分支（qq_gui.py:539-555 附近） | 读新的 **`connection{}`** 结构：`online`（bool）、`account`、`backend`、`received`、`sent`、`send_failed`、`db_dir`、`admin`、`degraded`、`error` |
| C3 | 卡片值/详情示例 | 在线：值 `已连接`，详情 `{backend} · {account} · 收 {received} / 发 {sent}`；离线：值 `未连接`，详情用 `error`；若 `admin` 为假，详情追加 `｜无管理员权限`；若 `degraded` 非空，追加它的前 60 字 |
| C4 | 按钮 `测试 QQ` → `POST /api/test-qq` | 按钮 `测试微信` → `POST /api/test-wechat` |
| C5 | `refresh()` 里消息渲染用的键 | 后端返回的 `messages[]` 现在是 `{text, sender, room, state, reply, error, time}`：渲染成 `[时间] 群/会话 发送者：正文` + 回复/错误行。`state` 映射表补上 `pending/sending/sent/failed/uncertain/expired/merged` 的中文 |
| C6 | `运行记录` 区 | 保持不变（`events[]` 结构没变） |

同时把 `MODEL` 卡片逻辑保留不动。

## D. 其他必须/建议

| # | 位置 | 改动 |
| --- | --- | --- |
| D1 | `api_get/api_post/api_delete` | 非 2xx 时把后端返回的 `detail` 提升成 `message`（现在 400/404 会显示成"操作完成"，把真实错误吞掉）。做法：`response.status_code >= 400` 时取 `response.json().get('detail')` 作为 message，`success=False` |
| D2 | `PersonaTab` 文案 | 提示里的「默认是鲸鱼娘」保留；placeholder `傲娇鲸鱼娘` 保留；把任何 `QQ` 字样改成 `微信` |
| D3 | `PluginTab._delete_plugin` 的确认文案 | 把错误的 `src/plugins/{id}.py` 改成实际位置 `data/wechat-only/custom_plugins/{id}.py` |
| D4 | 全文件 | 搜索 `QQ`/`8090`/`WhaleGirlQQ`/`qq` 字面量，除本清单明确保留的以外全部改掉；`--self-test` 分支与 `--no-backend` 开关保持原样 |

## E. 不要改的东西

`BackgroundFrame`、`TitleBar` 的拖拽/最大化逻辑、`StatusCard`、外观系统（`AppearanceTab` 与
`_apply_*`/`apply_mica`/`overlay_color` 等函数）、`icon_row`/`decorate_button`/`_card`、
`PluginConfigDialog`、`PluginInstallDialog`、`KeywordRulesEditor` 的使用、
`src/gui/config.py`/`appearance.py` 的调用方式、`plugin_files` 的上传逻辑、`--self-test` 入口。

## F. 交付要求

1. 改完必须能通过语法与导入检查：
   - `D:\聊天机器人\微信机器人\.venv-wechat\Scripts\python.exe -m compileall -q wechat_gui.py src`
   - 在项目根目录跑 `set QT_QPA_PLATFORM=offscreen` 后：
     `.\.venv-wechat\Scripts\python.exe -c "import wechat_gui; print(wechat_gui.BASE_URL)"`
2. 报告：逐条对应本清单的改动（A1–A10 / B / C1–C6 / D1–D4），列出你改动的每一处
   关键代码片段与最终 `BASE_URL`、设置页字段名、状态页读取的键；
   以及你跑过的检查命令与结果。**不要修改** `src/wechat_app.py`、`src/wechat_transports.py`、
   `src/wechat_bridge.py`、`src/plugins/**`、`instructions/**`。
