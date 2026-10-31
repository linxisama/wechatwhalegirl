# 给 AI / 后续开发者：本项目的铁律

项目：**鲸鱼娘 · 微信控制台**（`D:\聊天机器人\微信机器人`）
交付形态：**两件套**（`发布\<版本>\` 下的安装包 + DIY 指令包，详见第〇节）；安装包本体仍是 `installer\WhaleGirlWeChat-Setup-<版本>.exe`，双击覆盖安装即用

用户要求（2026-10-15）：更新必须**覆盖式安装**，保留原用户配置与对话记录。正式 AppId 和数据目录不得随版本变化；不得升级前卸载、重置配置或自动裁剪历史。每次发布验证旧版带数据覆盖升级，不能只验证全新安装。

## 最新发布顺序（用户 2026-10-29 明确要求，优先于下方历史流程）

先完成真实功能验收并修复所有已知阻塞缺陷，再制作正式新包。已有 1.3.31 产物仅为内部测试候选，不能因离线或打包检查通过就当作正式交付。调试确实需要冻结程序时，只生成隔离目录内的内部测试构建并核验；在功能验收前不得调用 Inno Setup 或 make_release 制作新的正式安装包、源码/DIY交付包。内部候选不代表已交付。

正式交付仍须安装包与 DIY 两件套、源码、完整回归、冻结自检及旧版带数据覆盖升级验证，且最终包必须对应验收过的同一源码/构建。真实发送需遵守当前单次授权；功能验收包含当前会话真实后台发送及独立回执、实际新入站到自动回复、监听/重连与日志状态，以及列明的已知缺陷。无法承诺所有环境永远零缺陷；不得把尚未验收项目写成可用。测试内部候选必须保留现有配置、历史、配对及登录状态，不强制结束微信或补发旧消息。

---

## 〇、交付模式：每次发布两份产物（用户 2026-10-23 定）

项目已开源，之后每次发布都在**新文件夹 `发布/<版本>/`** 里放两件套，二者缺一不算交付：

| 产物 | 文件 | 给谁用 |
| --- | --- | --- |
| ① 安装包 | `WhaleGirlWeChat-Setup-<版本>.exe` | 普通用户，双击覆盖安装 |
| ② DIY 指令包 | `WhaleGirlWeChat-DIY指令包-<版本>.zip` | 想自己改的用户：把整包丢给 DeepSeek 就能改代码 |
| （附）纯源码 | `WhaleGirlWeChat-源码-<版本>.zip` | 开发者阅读/接管 |

生成方式：

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File build_installer.ps1   # 先出安装包
powershell -NoProfile -ExecutionPolicy Bypass -File tools/make_release.ps1 # 再组装两件套
```

要点：
- DIY 指令包的**说明书源文件在仓库根目录 `diy-src/`**（`00-先读我.md` ~ `05-自测与打包.md`）。改了功能就同步更新这几份，否则用户拿到的指令会过期。
- 打包脚本 `tools/make_release.py` 用白名单遍历，**自动排除** `.venv-wechat`、`build`、`dist`、`data`、`logs`、`发布`、`__pycache__`、`.tmp*`、`.workbuddy` 与大于 2MB 的文件，因此不会把用户聊天记录或安装包本体塞进压缩包。
- 不要把安装包放进源码包/DIY 包里（170MB 级重复）。

---

## 一、最重要的一条：每次改动都要重新打包

用户的要求（2026-10-14 明确）：**「这个打包成安装包使用，之后的更改都继续打包安装包」**。

所以任何一次代码/资源/插件/文档改动之后，都必须：

```powershell
# 1) 先跑源码侧自检（快，几秒到几十秒）
.\.venv-wechat\Scripts\python.exe tools\selfcheck_backend.py
.\.venv-wechat\Scripts\python.exe tools\selfcheck_http.py
.\.venv-wechat\Scripts\python.exe -m unittest discover -s tests

# 2) 再重新打包（脚本内部会跑"冻结 exe 自检"，约 2 分钟）
powershell -NoProfile -ExecutionPolicy Bypass -File build_installer.ps1

# 3) 需要时做安装包往返验证（静默安装 → 装好的程序自检 → 卸载）
powershell -NoProfile -ExecutionPolicy Bypass -File tools\verify_installer.ps1
```

**不要**只在源码上改完就交付 —— 用户用的是安装包，源码改了包没重新打，等于没改。

**有行为变化就递增 `src/runtime_identity.py` 的 `APP_VERSION`**（单一版本来源），
否则用户装了新包也分不清是不是新版。

详细流程、产物位置、失败排查见 [构建与发布说明.md](构建与发布说明.md)。

---

## 二、封号风险红线（不可突破）

用户明确要求：**有封号危险的功能要删掉**。完整说明见 [封号风险与功能取舍.md](封号风险与功能取舍.md)。

- **永不调用**：`SendToLabel`、`ForwardToLabel`、`AddLabelMembers`、`ForwardMessage`、
  `ForwardVoiceMessage`、`SwitchToMoments`、`Moment*`、`Poke`、`VoiceCall`、
  `RecallLastMessage`、`RecallGuard`、`AtAll`；不做加好友/通过好友、朋友圈、拉群、
  群公告、改资料、@所有人。
- **不内置主动外发**：定时多群群发、久未说话问候、群聊总结私聊推送都已删除，不要"顺手加回来"。
  主动发送通道只有在 `wechat.proactive.enabled: true` 时才会放行（默认 `false`），且必须落在群白名单内。
- **不做键鼠点面板**：发送前不切会话、不截图 OCR 认行、不点表情面板、不点会话列表。
- 群聊默认"必须被 @ 或命中触发词"才回；未被点名时插话概率默认 `0`。
- 发送侧必须保留：单线程串行闸门、发送间隔、25 秒超时且**超时不重发**、连续失败触发重连。
- **发送通道唯一例外（2026-10-26 用户在会话中明确批准）**：微信 4.1.x 恢复窗口后 UIA 树只剩
  `Qt51514QWindowIcon` 与 `MMUIRenderSubWindowHW`，没有任何 `mmui::` 控件，走
  `mmui::ChatInputField` 必然 `send_failed`；Win32 `PostMessage` 直投文本实测无效
  （窗口差异 0.0205 < 空转噪声 0.0272）。因此**只**放行 `wechatauto/guia.py` 的「坐标 + OCR」
  发送：截图 → WinRT/guia OCR 定位输入框与发送按钮 → 真实鼠标点击 → 剪贴板粘贴。
  缺一不可的限定：只能发给**当前已打开的会话**，发送前必须用标题栏 OCR 校验会话名，
  不符就明确失败、绝不发。这一条只放宽"不做键鼠点面板"里的"不截图 OCR"半句，其余照旧。
- 走例外通道时**仍然禁止**：`hot_activate`（写微信进程内存）、自动切换会话
  （点会话行 / 搜索切换 / `open_chat`）、点会话列表、点表情面板；"永不调用"清单与
  "不内置主动外发"不变。实现若需要触碰红线以外的任何东西，继续停下来问用户。

- **自动打开目标会话的唯一例外（2026-11-01 用户在会话中批准：回答「2」选定此路线）**：只有**本次真实发送**在发送前只读核验发现「当前没有打开目标会话」或「标题与目标不一致」时才允许用 **Ctrl+F 搜索 → 粘贴会话名 → Enter** 打开目标会话；打开后必须标题栏 OCR 与目标名逐字一致才继续，不一致立刻 Esc 退出并报 target_not_open，一个键都不往正文里打。可用性轮询、状态刷新、健康检查**一律不触发**（只有真发送会走到）。环境变量 WHALE_AUTO_OPEN_CHAT=0 可整条关掉。仍然禁止：hot_activate、点会话列表/点会话行、点表情面板、任何「猜一个聊天」的点击。

改动如果触碰以上任一条，先停下来问用户，不要自行放宽。

---

## 三、改代码前先读

| 文件 | 作用 |
| --- | --- |
| [instructions/WECHAT-CONTRACT.md](instructions/WECHAT-CONTRACT.md) | **内部契约**：消息字典、回复元数据、runtime/transport API、插件清单、发送可靠性约束。改接口必须先改契约 |
| [功能对照与使用说明.md](功能对照与使用说明.md) | 旧版微信机器人每个功能的落点（哪个文件/插件） |
| [微信版使用说明.md](微信版使用说明.md) | 用户视角的安装/配置/排错（改功能后同步更新） |

架构一句话：`wechat_gui.py`（PyQt6 控制台，纯 HTTP 客户端）→ `src/wechat_app.py`
（运行时 + 本机 HTTP 接口）→ `src/wechat_transports.py`（消息字典 + 发送通道）→
`src/wechat_bridge.py`（读消息走微信本地数据库只读监听，发消息走桌面自动化）。
插件只通过契约里的 runtime/transport 接口接触外部世界，**不允许 import 微信库**。

---

## 四、编码与工程约定（都是踩过的坑）

1. `build_installer.ps1` **必须 UTF-8 带 BOM**：本机只有 Windows PowerShell 5.1，
   无 BOM 会按 ANSI 读，中文变乱码并直接语法报错。（用会去 BOM 的工具改完要检查并补回。）
2. `.bat` / `.cmd` 必须 **UTF-8 无 BOM + CRLF**，首行 `chcp 65001`。BOM 会让 cmd 读坏第一条命令。
3. 冻结（打包）后的程序是 GUI 子系统：PowerShell 里必须 `Start-Process -Wait` 才会等它结束。
4. 数据分两处：只读资源来自 `sys._MEIPASS`（打进 `_internal`），可写数据一律在
   `%LOCALAPPDATA%\WhaleGirlWeChat`。不要往安装目录写文件。
5. 冻结后 OCR 子进程走 `WhaleGirlWeChat.exe --ocr-worker <图>`（`gui_launcher.py` 转发），
   不要再写 `exe ocr_worker.py <图>` 这种源码式调用。
6. 中文注释、注释写"为什么"；测试只加不减；发现真实缺陷不要用改测试的方式绕过。
7. 微信库接口只用 `wechatauto`（微信 4.1.12+）作为主后端，`wxauto4` 为 4.0.x 备用，
   经典 `wxauto` 只支持 3.9.x；本机实测是 `weixin.exe` 4.1.15.13 → 走 `wechatauto`。

---

## 五、后续优化参考 QQ 版，再验证并交付安装包

用户的要求（2026-10-15 明确）：**「之后的优化就参考qq机器人的新功能，然后测试好打包」**。

- 后续功能优化先检查 `D:\聊天机器人\qq机器人` 的最新功能、相关实现和使用说明，确认哪些适合微信版。
- 按微信内部契约适配消息结构、插件、界面及配置；跨平台接口不能直接照搬。
- 第二节的封号风险红线优先，QQ 版新增的主动外发、批量转发、面板操作等功能不能自动移植。
- 同步更新内部契约、功能对照与微信版使用说明；有行为变化递增 `APP_VERSION`。
- 按第一节完成源码自检、完整测试、冻结程序自检和安装包构建；涉及安装或冻结运行路径时做安装卸载往返验证。
- 交付时给出安装包路径、已适配的功能和实际验证范围。模拟测试与真实微信测试分别说明；真实发送遵守已有授权。
