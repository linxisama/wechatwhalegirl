## 1.3.80 沙盒诊断升级：分清「服务没启动」和「系统组件真的坏了」

- 诊断报告新增两项证据：容器组件存储（`C:\ProgramData\Microsoft\Windows\Containers` 下的 BaseImages / PortableBaseLayer / Sandboxes / Dumps / Zygotes 是否存在，以及 `UtilityVm` 注册表键）与最近的容器管理器服务错误事件（`wevtutil qe System` 取事件 7023，识别「找不到文件」）。
- 新增结论码 `container_service_broken`：只有「CmService 状态查到了且不是 RUNNING」并且「BaseImages/PortableBaseLayer 缺失，或事件 7023 报找不到文件」两条同时成立才给出。报告里直接写清「这是系统组件损坏，不是控制台的问题」，并给出 sfc/DISM 修复、重装系统、换 Windows 11 24H2+ 三条路径，同时说明本机模式照常可用。
- 为什么不只看服务状态：健康机器上 `PortableBaseLayer` 也可能不存在（本机 Win11 实测如此），只看目录会把好机器误判成坏机器；两条同时成立才判损坏。
- 沙盒面板与 `/api/sandbox/provision/status` 同步：`state=broken` 时标题是「容器管理器服务损坏（Windows 沙盒起不来）」，`ok=false`、`next_step=manual`，不再劝用户去点「启用 Windows 沙盒」或「创建隔离通道」。
- 诊断报告新增 `[容器存储]` 与 `[服务事件]` 两节，对方回传时能直接看到证据，不用再猜。
- 新增 `tests/test_sandbox_doctor_180.py` 16 例，锁定证据采集、结论判定、界面措辞与 `_container_broken` 的保守边界。
## 1.3.79 沙盒环境诊断：一条命令把「这台机器为什么用不了沙盒」变成可回传的报告

- 新增命令行 `--sandbox-doctor`：只读采集系统版本、`WindowsSandbox.exe`、`wsb.exe`、容器服务（CmService/vmcompute/hns）、`hcsdiag list`、DISM 功能状态与管理员权限，写成 UTF-8(BOM)+CRLF 的文本报告，默认落在桌面并自动打开。
- 可选 `--probe-launch`：真的启动一次沙盒，看窗口有没有出现、映射目录里能不能拿到标记文件；探测结束用 WM_CLOSE 关掉自己打开的那个沙盒窗口，不在桌面留 VM。
- 结论码六种，每条都带一句「怎么办」：`ok` / `no_sandbox_app`（没启用沙盒功能）/ `container_service_down`（容器服务没跑）/ `no_cli_but_app`（有沙盒程序但没有 wsb.exe）/ `sandbox_window_only`（窗口能起来但映射命令没生效）/ `sandbox_probe_inconclusive`（探测没看到窗口）。
- 把 Win10 的事实写进结论：健康的 Win10 只有 `C:\Windows\System32\WindowsSandbox.exe`、没有 `wsb.exe`，隔离通道还需要「文件通道」后端（后续版本提供）；控制台本机模式不受影响，收发消息、插件、DSH 全部可用。
- 诊断只读：唯一的写操作是自己的报告文件和 `%PUBLIC%\whale-doctor-*` 临时探测目录，不碰微信数据、不改配置。
- 新增 `tests/test_sandbox_doctor_179.py` 13 例，锁定结论判定、报告格式、探测超时也会关窗口。
## 1.3.78 wsb.exe 解析器改问 Known Folder 与 PATH：环境变量被改写也不误报

- 依据：1.3.77 用「干净数据目录」模拟新电脑时，把 LOCALAPPDATA 指到临时目录，兼容性自检立刻报「System32 与 WindowsApps 里都没有 wsb.exe 命令行」，可命令行其实能用。真实世界同样会发生：控制台被计划任务或服务拉起时，LOCALAPPDATA 会指向 systemprofile 或被启动器改写，于是自检把「能开沙盒」误报成「沙盒不可用」。
- 修复：src/site_profile.py 新增 _known_local_appdata()（用 SHGetKnownFolderPath 问真实的 LocalAppData，问不到才退回环境变量）与 _wsb_alias_dirs()；_resolve_wsb_exe() 增加 local_appdata / path_env 参数，查找顺序改为 System32 → 环境变量里的 WindowsApps → Known Folder 的 WindowsApps → PATH 里逐个目录兜底，来源取值新增 path。
- 口径统一：src/sandbox_provision.py 的 _wsb_exe_path() / _wsb_exe_exists() 改为共用 site_profile 的同一个解析器，沙盒面板与兼容性自检对同一台机器只会给出同一个结论。
- 测试隔离：tests/test_sandbox_path_177.py 与 tests/test_sandbox_win10_176.py 现在把 PATH 与 Known Folder 一并钉住（否则本机真实装着的 wsb.exe 会漏进用例，让「没有命令行」的断言变成看运气）；新增 tests/test_wsb_resolver_178.py 5 例（环境变量被改写仍按 Known Folder 找到、PATH 兜底、两处都没有报 none、System32 仍优先、API 不可用时退回环境变量）。
- 实测：把 LOCALAPPDATA 指到临时目录后跑 _probe_windows_sandbox()，windows_sandbox 从 warn 变 ok，wsb_exe_source=windowsapps，sandbox_available=true。
- 影响：只有「wsb.exe 只存在于应用执行别名或 PATH 里」的机器会从 warn 变 ok；System32 有的机器结论不变；发送、接收、插件、DSH 接入、数据目录结构都没有动。
- 发布：本轮出 dist78 与安装包，并发布到 GitHub Releases，供下载与真机复测。
## 1.3.77 沙盒自检与沙盒面板口径统一：wsb.exe 认应用执行别名

- 依据：1.3.76 交付后用全新数据目录跑「兼容性自检」，windows_sandbox 仍报「没有 wsb.exe 命令行，自动建隔离通道用不了」，可本机 wsb 命令明明能用——真正的 wsb.exe 在 %LOCALAPPDATA%\Microsoft\WindowsApps\wsb.exe（较新 Windows 把它装成应用执行别名），不在 System32。
- 修复：src/site_profile.py 新增 _resolve_wsb_exe()，按「System32 → WindowsApps」顺序找 wsb.exe，返回（路径, 是否存在, 来源）；_probe_windows_sandbox() 改用它，数据里多给一个 wsb_exe_source（system32 / windowsapps / none），warn 文案改为「System32 与 WindowsApps 里都没有 wsb.exe 命令行」。
- 影响：只有「只有 WindowsApps 别名」的机器会从 warn 变 ok（sandbox_available=true）；判定口径与 src/sandbox_manager.py、src/sandbox_provision.py 完全一致；发送、接收、插件、DSH 接入、数据目录结构都没有动。
- 验证：新增 tests/test_sandbox_path_177.py 6 例（System32 优先、只有 WindowsApps 也算有命令行、两处都没有报 warn、缺 WindowsSandbox.exe、家庭版、resolver 优先级）全绿；本机实测 windows_sandbox 从 warn 变 ok，wsb_exe_source=windowsapps。
- 文档更正：1.3.76 段末句原写「不做正式对外发布」，实际已按开源交付令发布到 GitHub Releases，本版把该句改为「已发布」并保留原校验值。
- 发布：本轮出 dist77 与安装包，并发布到 GitHub Releases，供下载与真机复测。
## 1.3.76 按用户《问题日志》修四项：安装器、沙盒口径、学习库自愈、退出码

- 依据：用户在 Windows 10 专业版 19045、1.3.70 上实测提交的《问题日志-2026-11-01》，四项全部复现并定位到代码。
- P0 安装器「错误 5: 拒绝访问」：安装日志里 RedirectionGuard status for current process: Enabled in enforcing mode，而同一账号用 PowerShell 写同一目录完全正常 → 不是 ACL，是安装器进程被系统的写入重定向挡了。installer.iss 增加 RedirectionGuard=no（只影响安装器自身进程，不改安装路径与权限模型）与 SetupLogging=yes（失败自动在 %TEMP% 留 Setup Log 日志，便于回报）；PrepareToInstall 里增加写权限探针，目录写不进去时当场说清「右键以管理员身份运行 / 换个目录」，不再等复制阶段才炸。
- P0 配套：/VERYSILENT 下致命错误也可能退出码 0，所以「装没装上」不再看退出码——安装成功时在安装目录写 install-marker.txt（版本 + 时间），src/install_guard.py 新增 read_install_marker() / marker_mismatch() 与只读接口 GET /api/install-guard/install-marker，用文件核对版本。
- P1 沙盒口径不一致：/api/sandbox/status 说没有命令行，/api/sandbox/provision/status 却说 wsb_exe=true 可以建通道。根因是 src/sandbox_provision.py 探的其实是 System32 下的 WindowsSandbox.exe，而自动建通道要的是命令行 wsb.exe；Windows 10 19045 只带前者。现在 _wsb_exe_exists() 只认 wsb.exe（含 %LOCALAPPDATA%\Microsoft\WindowsApps\wsb.exe 回退），探到「功能已启用但没有 wsb.exe」时统一返回 state='no_cli' / next_step='manual'，面板与自检都给同一句结论，并给出「先用本机模式 / 手动开沙盒 / 换机器」三条下一步。
- P1 升级后学习库空白：旧版把群聊正文存成「wxid_xxx: + 换行 + 内容」，旧提炼规则整条跳过；而补学判据又要求「学习活动窗口为空」，窗口一旦被旧版写进过就永久跳过。现在只要学习库还是空的就补学，且补学走新增的 relearn()：逐条剥掉发送者前缀重新提炼、按消息 id 幂等，跑两次不会重复计数；面板空状态直接提示「升级后老记录要重新提炼一次，点『全部会话一键学习』」。
- P2 记忆口径：状态卡「记忆」原来只统计已确认条数，现在同时给出待确认（candidate）与总条数，避免「学习库看着是 0」的误判。
- 影响：运行行为只在「沙盒探测结论、学习库补学、安装器」三处变化；发送/接收通道、插件、DSH 接入、数据目录结构均未改，升级仍原地覆盖、不卸载旧版、不动用户数据。
- 验证：新增 tests/test_sandbox_win10_176.py（8 例）、tests/test_learn_relearn176.py（8 例）、tests/test_install_marker_176.py（8 例）全绿；安装脚本用 ISCC 实编通过；全量回归与安装包实装见本轮交付报告。
- 发布：本轮出 dist76 与安装包，并已作为 1.3.76 发布到 GitHub Releases（安装包 171,603,477 B，sha256 b1c474cc…），供下载与真机复测。
## 1.3.75 安装包里补上《用户使用说明》

- 问题：安装目录随包文档有 README、微信版使用说明（逐版本变更记录）、功能对照与使用说明、封号风险与功能取舍、构建与发布说明、控制台操作指南，但最完整的《用户使用说明》（738 行，按「装好 → 配好 → 发得出去 → 出问题怎么查」写，含 9 个页签、32 个插件、错误码速查表）没有装进去，用户装完只能在源码目录里找。
- 修法：`installer.iss` 的 [Files] 段补一行 `Source: "用户使用说明.md"; DestDir: "{app}"; Flags: ignoreversion skipifsourcedoesntexist`，安装后位于安装目录根下，与其它随包文档并列。
- 影响：只改安装包内容，运行行为零变化；源码运行、升级安装、卸载逻辑都不受影响（卸载仍只删程序目录，不删数据）。
- 验证：本次安装包装机后安装目录根下 `用户使用说明.md` 存在，内容与仓库一致（64,281 B / 738 行）。
## 1.3.74 账号与登录面板秒开：不再扫盘猜目录

- 问题：点「账号与登录」后对话框长时间停在「正在读取账号状态…」。本机冻结包实测每次 6.6~7.2 秒（7.23 / 6.96 / 6.80 / 6.58），体验上像卡死。
- 根因：读账号昵称时若「微信数据库目录」为空，会走自动探测分支——扫盘并逐个账号开库做密钥扫描，单次实测 8.8 秒；而账号守卫每次打开都要读一遍。
- 修法：新增 `_guard_account_names()` 统一出口——目录为空直接返回空表（宁可显示「未知」也不猜）；目录回退链补上「当前连接的账号目录」；单次查询上限 3 秒，超时按未知处理；成功结果按目录缓存 60 秒，连点不重复扫；每次启动控制台清空缓存。
- 影响：面板打开即出结果；账号判定规则与优先级完全不变（进程打开的库 → 连接中的账号目录 → 未知）；改绑逻辑未改。
- 测试：新增 `tests/test_guard_fast174.py` 5 例（空目录不猜、结果缓存、换 app 不复用缓存、超时不卡面板、目录回退到连接账号）。
## 1.3.73 修复维护类对话框的线程亲和问题（点按钮偶发闪退的隐患）

- 现象：控制台里「检查更新」「Windows 沙盒」「插件包」「DSH 一键连接」「账号与登录」这些对话框，点按钮后 Qt 会打印 QObject: Cannot create children for a parent that is in a different thread。后台线程直接改控件属于未定义行为，机器慢或连点两下时可能直接闪退。
- 修法：src/gui/maintenance_dialogs.py 新增 _ThreadCallback 基类（50 毫秒小队列 + 定时器），DSH 连接、插件包、账号与登录三个对话框改为继承它；_run 的后台线程只把「回调 + 结果」入队，统一由 GUI 线程执行。service 调用仍在后台线程，界面不会因为请求超时卡住。
- 行为不变：按钮禁用与恢复、状态文字、日志内容与 1.3.72 逐字一致，只是回调的执行线程从后台线程改回 GUI 线程。
- 交叉验证：src/gui/sandbox_provision_panel.py 与 wechat_gui.py 的 _run_api 本来就走信号回主线程，本轮无需改动。
- 测试：新增 tests/test_dialog_thread173.py 5 例（入队未排空前不回调、refresh 后控件在排空前保持原样、后端抛异常时结果仍回到 GUI 线程、空队列重复排空安全、三个对话框都必须走延迟投递且源码里不许再出现 on_done(result)）。
## 1.3.72 账号守卫：微信换了号也能一眼看出来并一键改绑

- 新增「账号与登录」：控制台「环境与自检」区点开，一眼看到机器人账号、微信当前登录的账号、两者是否一致，以及不一致时该怎么做（切回机器人账号，或一键改绑）。
- 状态卡片把账号错位顶到最前面：微信换号后连接卡片直接显示「账号不一致 · 发不出去」并写出两边账号，不用等到发消息失败才看见一句「未唯一核验账号与微信窗口」。
- 一键改绑：二次确认后把机器人账号改成当前登录的账号，只改 `wechat_only.account` 与 `wechat.account` 两项；消息库、人设、插件设置、记忆都不动，旧账号未处理的消息仍在库里，切回去还能看到。改绑会重连一次（约 5~20 秒）。
- 发送自检不再只报「发送失败」：账号错位时 reason 变成 `account_mismatch`，正文写「把微信切回机器人账号，或点「改绑到当前登录账号」」，原始原因保留在 `detail` 里备查。
- 判定不猜：读不到微信当前登录账号（多半是控制台没以管理员身份启动）时明确报 unknown，并给出两条出路（用管理员启动控制台 / 到「设置」页手动指定机器人账号），绝不把「读不到」当成「不一致」。
- 新增接口 `GET /api/wechat/account-guard`、`POST /api/wechat/account-guard/rebind`（必须带 `confirm: true`；已经一致时直接拒绝，避免为了改绑做无意义的重连）。测试新增 `tests/test_account_guard172.py` 24 例。
## 1.3.71 发送自检：一键验证「能不能真的发出去」

- 新增「发送自检」：控制台「环境与自检」里点一下，就用当前微信账号往「文件传输助手」真的发一条带版本号的消息（形如 `【发送自检】WhaleGirlWeChat 1.3.71 12:30:16`），把剪贴板、草稿、标题核验、发送键这几步一次跑通。目标写死「文件传输助手」，调用方无法指定会话，绝不碰真人会话。
- 新增接口 `POST /api/wechat/selftest-send`：必须带 `confirm: true` 才会发，没带就一条也不发；同一实例 20 秒冷却（`SELFTEST_COOLDOWN`），连点不会连发两条；只发一条，失败绝不重发、绝不二次点击；失败原因（未确认 / 冷却中 / 微信未连接 / 发送失败）原样回给界面。
- 为什么需要它：接收链路可以只读数据库静默验证，发送链路必须真的发出一条才知道通不通；以前只能让用户找个人发条微信等回复，现在用自己账号的「文件传输助手」就能一次验证。
- 实现：新建 `src/send_selftest.py`（`SELFTEST_TARGET`、`SELFTEST_COOLDOWN=20.0`、`SELFTEST_PREFIX`、`build_text()`、`class SendSelfTest`），自检走机器人平时回复用的同一个通道对象（`src/wechat_transports.py` 的 `send_to`），证明的是「机器人能发出去」而不是「另写了一条能发出去的路」。
- 界面：`wechat_gui.py` 的「环境与自检」新增「发送自检」按钮，点击先弹二次确认（写明会真的发一条、不会发给任何人），结果用对话框 + 状态栏同时显示，带用时秒数。
- 测试：新增 `tests/test_send_selftest171.py` 20 例（正文格式、未确认不发、只发一条、目标不可改、离线、缺通道、失败透传、异常转结果、冷却窗口、冷却到期、失败也计冷却、路由三态、坏请求体）。行为不变边界：不改发送链路本身、不改接收、不新增任何主动发消息入口。
## 1.3.70 修复沙盒面板卡在「需要重启」

- 「需要重启」不再永久有效：`src/sandbox_provision.py` 新增 `REBOOT_GRACE_SECONDS=90.0`、`_system_boot_time()`、`_rebooted_since()`，用系统开机时刻判断记录是否过期——电脑重启过一次之后，旧的 `pending_reboot` 记录自动失效，`needs_reboot` 归零。
- 任何状态都不再把 `next_step` 设成 `reboot`。原来它会把「启用 Windows 沙盒」和「创建隔离通道」同时禁掉，只留一个回答「还没有启用记录」的重检按钮，形成出不去死循环。现在：已启用 → `create_channel`；未启用或组件缺失 → `enable`；不支持 → `none`。
- `src/sandbox_provision_api.py` 的 `check()` 在没有提权日志时（提权已就绪时 DISM 就地执行，本来就没有日志文件）改调新增的 `_probe_verdict()`，按真实探测结果回答「已经启用，可以直接点创建隔离通道」/「还没启用，点启用」/「版本不支持」/「读不到 DISM」，不再回答「请先点启用 Windows 沙盒」。
- `src/gui/sandbox_provision_panel.py`：「我已重启，重新检测」改为只读出口、任何时候都能点；「创建隔离通道」在 `create_channel` 与 `launch` 两种下一步都放行。
- 测试：`tests/test_sandbox_provision165.py` 29 → 37 例（重启后旧记录失效、任何状态都不出现 `reboot` 下一步、无日志时 `check()` 的四种结论、面板按钮新契约）。
- 行为不变：发送与接收链路、插件体系、更新链路一律未改；提权方式与 DISM 调用参数未改。
## 1.3.69 更新源被限流时自动回退

- GitHub 未登录请求按出口 IP 限流（同一 IP 每小时 60 次，超限返回 403/429），原来会让「检查更新」直接报「更新源返回 HTTP 403」。现在 `src/updater.py` 新增 `FALLBACK_FEED_URL`，只在默认的 `api.github.com` 地址被限流时改走 Releases 直链 `.../releases/latest/download/update.json`，检查更新照常成功。
- 新增失败码 `rate_limited`，中文提示「更新源接口的请求次数已用完（GitHub 未登录请求按出口 IP 限流），请稍后再试」。
- 只有默认地址才会回退：自定义更新源被限流时保持原样、不会悄悄换地址（新增测试覆盖该边界）。
- 行为边界：下载、SHA256 校验、安装、插件、接收、发送逻辑一字未改；本次只动了更新源读取与错误分类。
## 1.3.68 更新源指向正式仓库

- 自动更新的默认更新源由占位仓库 `linxisama/WhaleGirlWeChat` 改为正式仓库 `linxisama/wechatwhalegirl`（`src/updater.py:26` 的 `DEFAULT_FEED_URL`），全新安装不必再手工填地址就能查到新版本。
- `tools/publish_github.ps1` 的默认远端同步改为 `https://github.com/linxisama/wechatwhalegirl.git`，`-Push` 直接推到正确仓库。
- 发布包 `update.json` 的 `installer.url` 与 `installer.checksum_url` 现在写成绝对下载地址（`tools/make_github_release.py --base-url`），把更新源指到 `update.json` 的机器也能直接下载安装包。
- 行为边界：接收、发送、插件、记忆、沙盒、DSH 的逻辑一字未改，本版只动了更新源常量、发布脚本默认远端与发布清单里的地址形式。
## 1.3.67 首次在干净机器上装机验收：插件包不再误报失败、兼容建议免绑定

- 插件包页（`src/plugins/manager.py`）：包 id 与内置插件同名时，状态由「shadowed + 红色错误」改为 `builtin`，文案「该插件已内置在主程序里，插件包无需再启用」。理由：`plugin-packs/` 里的 10 个包本来就是内置插件的可选发行形态，全新安装打开插件包页原来会看到 10 条「已存在同 id 的插件，这个插件包没有生效」，看起来像坏了。真正与自定义插件撞名仍报 `shadowed`。
- 兼容建议（`src/compat_advice.py`）：`GET /api/compat/advice` 不再要求先绑定 Agent。原来全新安装（还没接 Agent）点「兼容建议」只会拿到 403「未绑定」，而这恰恰是用户最需要看「这台机器缺什么」的时候。接口仍然是只读诊断，且后端中间件依旧只允许本机访问。
- 本轮验收事实：Windows 沙盒（Windows 10 Enterprise 24H2 26100 容器）从 0 装 `WhaleGirlWeChat-Setup-1.3.66.exe` 静默安装成功（退出码 0，安装目录 623MB，PE 版本 1.3.66，10 个插件包就位，卸载器就位），启动后 `/api/env` 九项自检全部正常返回（缩放 100%、1520x823 单屏、数据目录可写、管理员身份、8091 可用），`/api/update/status` 在无外网时给 `offline` 中文提示而不是崩溃。
- 同一轮发现的环境限制（写进 `控制台操作指南.md`）：该沙盒镜像默认开启 Smart App Control（`VerifiedAndReputablePolicyState=1`），未签名的安装包会被「Application Control policy has blocked this file」直接拦下，`Unblock-File` 无效，必须在 Windows 安全中心关闭 Smart App Control（一次性、不可逆）或对安装包做代码签名后才能安装。
## 1.3.66 开源交付版：插件包体系、自动更新、沙盒一键启用、DSH 零配置连接

- 插件包（`src/plugin_packs.py`、`plugin-packs/`）：一个文件夹就是一个插件（`plugin.json` 清单 + `plugin.py` 入口 + `README.md` + `settings.example.json`）。清单校验 `id`/`name`/`version`/`entry`/`api_version`/`requires`；缺 `author`/`upstream`/`native`/`side`/`data_policy`/`capabilities` 只记告警不拦截。安装支持目录与 zip（拒绝绝对路径、`..`、盘符、符号链接、超过 2000 个文件或 20MB），目标已存在时返回 `exists` 绝不覆盖。主程序**只加载用户自己放进去的包，不会自动下载或安装**；插件页新增「插件包」窗口，可查看、安装、移除、启用/停用，坏包只显示中文原因不影响启动。发行构建设 `WHALE_PLUGINS_FROM_PACKS=1` 时，10 个可选插件只从插件包目录加载（内置 23 个核心插件不受影响）。
- 自动更新（`src/updater.py`、`src/update_service.py`、`src/update_api.py`、`src/gui/update_dialog.py`）：控制台「环境与自检 → 检查更新」与启动后自动检查一次（只提示发现新版本，不自动下载安装）。状态机 `idle → checking → available/up_to_date/error → downloading → downloaded → installing`；下载写 `.part` 分块落盘，完成后按发布方提供的 `.sha256` 校验，**校验不通过绝不安装**；支持跳过某版本、重试、离线与网络中断（断流删残包回退 idle）；更新源可配置（`update.feed_url`/`update.auto_check`/`update.interval_hours`），默认指向 GitHub Releases，绝不内置任何令牌。
- 沙盒一键启用（`src/sandbox_provision.py`、`src/sandbox_provision_api.py`、`src/gui/sandbox_provision_panel.py`）：控制台「环境与自检 → Windows 沙盒」先检测版本/功能状态，未启用时用 `dism /online /enable-feature /featurename:Containers-DisposableClientVM` 提权启用（UAC），把 dism 输出写进 `logs/sandbox-provision/`，用 `last-enable.json` 哨兵保证重复点击不会二次拉起授权、上次失败可重试；重启回来后点「我已重启，重新检测」继续，最后「创建隔离通道」复用既有 prepare 流程。状态含 `enabled`/`disabled`/`absent`/`unsupported`/`unknown`/`pending_reboot`，家庭版等不支持的系统给明确说明，「查看禁用方法」给出 `dism /online /disable-feature` 回滚命令。
- DSH 零配置连接（`src/dsh_autoconnect.py`、`src/dsh_autoconnect_api.py`）：Agent 页「DSH一键连接」把三步一次做完且幂等——刷新/生成接入脚本 → 写入 `whale-workbench` MCP 预设 → 写一条用户级插件行，让**任何 preset 的新会话**都能看见 `whale_wechat` 工具（不改用户的 `selectedDefault`，也不覆盖手写过的配置段：内容等价记 `current`，被本地改过记 `foreign` 并保留原字节）。守护线程每 30 秒探测一次，DSH 重启或端口漂移后自动重新发现并重写配置，异常按 1→2→4…→60 秒退避；`GET /api/dsh/connect` 回 `state`/`issues`/`hint` 供面板直接显示。
- 新增接口：`GET /api/plugins/packs`、`POST /api/plugins/packs/rescan`、`POST /api/plugins/packs/install`、`DELETE /api/plugins/packs/{id}`、`POST /api/plugins/packs/{id}/enabled`、`GET /api/update/status`、`POST /api/update/{check,download,install,skip,settings}`、`GET /api/sandbox/provision/status`、`POST /api/sandbox/provision/{enable,check,channel}`、`GET /api/sandbox/provision/rollback`、`GET /api/dsh/connect`、`POST /api/dsh/connect/{repair,watch}`。
- 配套文档：新增 `CONTRIBUTING.md`、`控制台操作指南.md`，`README.md` 重写为开源门面（安装、首次配置、插件包、自动更新、沙盒、DSH、数据与隐私、安全边界）。发行构建另出 `release/<版本>/`：安装包、`.sha256`、源码 zip、插件包 zip、`update.json`、`SHA256SUMS.txt`、`manifest.json`。
- 行为不变的边界：发送链路（像素/坐标 + 载重校验）、接收（只读 SQLite 增量）、零外发闸门、记忆注入、插件内核语义均未改动；本轮只新增可独立关闭的能力入口。
## 1.3.62 内部候选：状态页自己复检发送环境，不再永久挂着「微信已最小化」

- 现场症状：换到新版本后状态页一直写「发送未就绪：微信窗口已最小化，请手动恢复并打开目标聊天；不会自动弹出窗口」，可窗口其实好好的；点一次「测试发送」立刻又变「发送就绪」。别的电脑上的用户会以为根本发不出去。
- 根因一（状态不自查）：发送环境只在这项检查被调用时刷新（src/wechat_bridge.py 的 inspect_send_environment），而启动后只有严格后台通道会定期复检；正常前台通道（wechatauto）从启动那一帧起再也不刷新，于是把启动瞬间的临时状态永久挂在界面上。
- 根因二（体检文案自相矛盾）：体检接口先从缓存取原因码拼进消息，再去重新探测，于是「发送就绪」和「原因码：minimized」会同时出现在一句话里。
- 改法（src/wechat_app.py）：新增 Runtime.refresh_send_state_if_stale()，GET /api/status 时按需补一次只读复检（队列空闲且距上次检查超过 15 秒才查，探测失败不影响状态页）；/api/test-wechat 改成先复检、再按最新结果拼消息，不再出现陈旧原因码。
- 文案（send_channel_hint）：正常通道下 minimized 的处置提示从「把微信窗口恢复（不要最小化）」改为「发送前会自动恢复显示（只显示、不抢前台），不用手动恢复；若一直显示，点『测试发送』复检」，与 1.3.59 的实际行为一致；严格后台通道（background_send）不会自动窗口，保留原来那句手动提示。
- 边界：复检只读，不输入、不发送、不弹窗、不抢前台；发送中（stage 不是 idle）完全不查，避免和发送抢通道。
- 版本只改 src/runtime_identity.py 的 APP_VERSION 一行。

## 1.3.61 内部候选：发送前自动打开目标会话（Ctrl+F 搜索 + 标题核对）

- 现场症状：微信当前没打开任何会话时，发送探测直接失败（只读布局检查失败：未确认标题栏与正文之间的分隔线），回复全部卡在队列里等过期；别的电脑上用户不可能每次都手动把会话摆好。
- 改法（src/wechat_guia_send.py）：只在**真发送**路径、且发送前核验发现「没有打开的会话 / 标题与目标不一致」时，才 Ctrl+F 调出搜索 → 粘贴会话名 → Enter 打开，再用标题栏 OCR 与目标名逐字核对。
- 安全边界：全程**不点会话列表、不点会话行、不按顺序猜聊天**；打开后标题不一致立刻 Esc 退出并报 target_not_open；搜索框没抢到焦点时会话名会落进输入框，此时先只读检查输入框、有字就 Ctrl+A + Delete 清掉并放弃，**绝不按 Enter**（否则等于把会话名当消息发出去）。
- 只在发送时触发：可用性轮询、状态刷新、健康检查都不会弹窗；环境变量 WHALE_AUTO_OPEN_CHAT=0 可整条关闭。设置页「自动打开会话」开关（wechat.auto_open_chat，默认开）在每次发送前写进发送子进程，关掉后发送端完全不做搜索；环境变量同上，显式 0 才是关。
- 新增阶段码 guia_open_chat，成功/失败都落账（reason=search_unavailable / title_mismatch）。
- 版本只改 src/runtime_identity.py 的 APP_VERSION 一行。

## 1.3.60 内部候选：换一台电脑也能连上 DSH；不是管理员时一键提权

- 现场症状：在别的电脑上点「自动查找桌面DSH」经常一无所获，日志写「无法读取本机监听端口」。原因不是 DSH 没开，而是普通权限的进程读不到别人进程的端口归属：`psutil.net_connections()` 只给 pid 为空的 LISTEN 行。
- 改法（`src/dsh_discovery.py` 重写）：读不到端口归属时不再报错，改为「按 DSH 自己留下的线索端口逐个核验」——已保存端口 → 默认 9175 → `~/.dsh` 下最近改动的 40 个文本文件尾部 64KB 里出现过的端口（`~/.dsh/.credentials*` 一律跳过，文件内容不外传、不落日志）。每个端口都要真正回答 `settings/describe` 与 `agentPresets/list` 才算候选，绝不猜端口。
- 新增两个状态：`dsh_missing`（这台电脑没有 `~/.dsh`，提示先装桌面 DSH）、`dsh_offline`（装了但没在跑）；面板显示「这台电脑没装桌面DSH」「桌面DSH没在运行」，并补一句「当前权限读不到端口归属，已改为按线索端口逐个核验」。
- 探测/连接失败时顺手找一次：`POST /api/dsh/probe`、`POST /api/dsh/connect` 在 `DshError` 之后调 `discovery_hint()`，发现本机 DSH 在别的端口就返回 `state='drift'`，面板直接弹「更新为 9527」横幅；任何异常都不许盖掉原来的失败原因。
- 兼容性自检从 7 项变 9 项：新增 `privilege`（是否管理员）与 `dsh`（桌面 DSH 装没装、在不在跑）。非管理员时自检直接告诉用户「微信 4.x 可能能收不能发」。
- 新增 `src/elevation.py` 与面板按钮「以管理员身份重启」：`ShellExecuteW('runas')` 起自己，只发起不等待；取消 UAC 或返回码 ≤32 时提示手动「右键 → 以管理员身份运行」；启动后若不是管理员，控制台会主动提示一次。
- 版本只改 `src/runtime_identity.py:4`。

## 1.3.59 内部候选：窗口被最小化时，发送前自动恢复显示
- 现场症状：用户发来消息、DSH 用完 5.8 秒就生成了回复，可发送第一步就 `guia_unavailable`「账号与窗口绑定已失效」。抓窗口状态发现微信窗口 `IsIconic=1`、矩形 (-32000,-32000,…)——它缩在托盘里。整条回复没发出去，用户看到的是「发了却没回」，面板只写「发送未就绪」。
- 根因：`src/wechat_guia_send.py` 的 `_assert_binding()` 把 `row.get('minimized')` 和「账号绑定失效」判成同一种失败，直接 `_GuardFailed('guia_unavailable', '账号与窗口绑定已失效，本次不操作微信')`；只读探测 `available()` 也把最小化当成错误码 `minimized` 报给控制台。
- 改法：新增模块级 `RESTORE_WINDOW_SECONDS = 3.0`、`RESTORE_WINDOW_POLL = 0.15`、`RESTORE_SHOW_FLAG = 4`（SW_SHOWNOACTIVATE）与 `_restore_minimized(hwnd, …)`（本来没最小化直接算成功；最小化就 `ShowWindow(hwnd, 4)`，再按 0.15 秒轮询 `IsIconic`，最多 3 秒）；`_build()` 在 `_assert_binding()` 之前调用新增的 `_restore_window_if_needed(gui)`，send / probe / available 三条路径共用。
- 边界：只对已绑定账号的那一个 hwnd 动手（`expected_window['hwnd']`，没有就退回 `gui.main_hwnd`）；`SW_SHOWNOACTIVATE` 只显示不激活，不抢前台、不写微信进程内存、不切会话、不点会话列表、不发送任何按键；恢复失败照旧 fail-closed 报 `minimized`（文案改为「微信窗口最小化且自动恢复失败，请手动恢复后再发」），绝不带着看不见的窗口硬发。
- 日志：每恢复一次写一行 `send phase=guia_restore_window … reason=窗口最小化，已自动恢复｜窗口最小化，自动恢复失败`；`src/logging_setup.py` 的 `SEND_PHASE_CODES` 增加该阶段码。
- 测试：新增 `tests/test_restore_window1359.py` 9 例（没最小化不碰窗口、`ShowWindow(hwnd, 4)` 且轮询到恢复、一直最小化返回失败且耗时不少于预算、`ShowWindow` 抛异常不算崩溃、hwnd=0 直接失败、通道只在最小化时恢复并写日志、恢复失败如实上报、`_build()` 先恢复再校验绑定）；相关回归 `test_*guia*.py` 146 例 OK。
- 已知限制：窗口若是被隐藏（而不是最小化）或所在显示器已断开，恢复可能失败，仍走 fail-closed 并提示手动恢复；窗口被别的程序完全盖住仍由 `blocked_by_window` 拒绝（那是防误点的载重闸门，不放宽）。
## 1.3.58 内部候选：输入框里的草稿由发送前自动清空
- 真机症状：22:01 之后发送卡一直停在「当前输入框已有草稿，发送尚未就绪」，用户发消息也没有回复。抓帧裁图确认微信输入框里悬着一张**上一次粘贴留下的图片卡片**（不是用户打的字），`probe_composer()` 报 `draft_state=text`、`has_text=True`；用户手动清空后立刻恢复。用户据此要求「之后你自动清」（m10623）。
- 改动一（发送前自动清空）：`src/wechat_guia_send.py` 新增 `_clear_existing_draft()`——先按原有三重核验确认「绑定窗口 + 微信在前台 + 标题等于目标」，再发 Ctrl+A 选中、Delete 删除，然后**用同一驱动复核真的空了**才算数；顺序在 `_empty_input_destination()` 里明确写成「先核验标题 → 再清草稿 → 再复核标题」，因为只有先自证还在目标会话，删掉的才一定是本会话的残留草稿。
- 改动二（体检不再把草稿当故障）：`probe()` 是只读体检，仍然一个字都不输入、不点击、不碰剪贴板；自动清空开启时它只把 `draft_present` / `draft_note` 放回诊断，让发送卡显示就绪，真正清空发生在发送前的写入阶段。这样就不会出现「界面一直说没就绪、模型也没机会回」的死循环。
- 绝不越过的边界：只发 Ctrl+A 与 Delete，**永不发 Enter/回车**（否则等于主动发消息）、不写剪贴板、不点会话列表、不切会话；清不干净就按 `draft_not_empty` 拒绝本次输入，绝不带着残稿粘贴；每次清空都写一条 `guia_draft_clear`（成功）或 `guia_draft_clear_failed`（失败并带原因）的运行记录。
- 开关：环境变量 `WHALE_AUTO_CLEAR_DRAFT=0`（也认 false/off/no）可关掉自动清空，恢复 1.3.57 的「见到草稿就拒绝」行为；默认开启。
- 测试：新增 `tests/test_auto_clear_draft1358.py`（4 例：草稿被 Ctrl+A+Delete 清掉后继续、清不干净仍拒绝、开关关掉一个键都不按、标题不符时连 Ctrl+A 都不发）；`tests/test_guia_preflight_draft1316.py` 改成「草稿不再挡住发送卡」+「关掉开关恢复等待」两例；`tests/test_guia_sendcost1350.py` 的陈旧草稿用例仍然要求拒绝（那份替身清完还脏），语义不变。
- 上机验证后的补丁（同日）：真机第一次验证就抓到判据漏洞——Ctrl+A+Delete 确实清干净了，但 PrintWindow 位图里光标层常在，清空后 `draft_state` 仍是 `caret_like`，`_input_box_has_text` 等不到闪灭帧就报「有文字」，于是被误判成「自动清空失败」。改为优先用 1.3.42 为同一种误判引入的墨迹判据：清空前量一次 `composer_ink()`，清空后用 `composer_cleared(box, before_ink, window=DRAFT_CLEAR_WINDOW=3.0)` 判断（`state=empty`，或 `caret_like` 且墨迹确实变少、`columns<=2` 或降到 max(2, 30%) 以内）；驱动没有这两个方法（离线替身/老驱动）时照旧退回 `has_text`。真机复验：粘贴草稿后 `has_text=True` → 清空日志 `guia_draft_clear` → 复核 `has_text=False`。新增 3 例墨迹判据用例（该文件共 7 例）。
- 第二次真机失败后的补丁（同日 22:28）：剪贴板快照改成 best-effort——原来「读不出的标准格式」「只有位图句柄没有 DIB」「值既不是 str/bytes 也不是 HDROP」「一个字节都没存下来」四种情况都会抛 `send_failed` 把整条回复拦下；实测外部程序（截图工具、Excel、Word）持有剪贴板时这些情况很正常，机器人因此反复停在「发送未就绪」。现在四种情况一律只把格式号记进 `_Clipboard.skipped`，能存的照常存、照常恢复，发送继续；`GuiaSendResult.diagnostic` 新增 `clipboard_skipped`（只报格式号，不含任何内容）便于事后核对；非 Windows API 异常（编程错误、测试替身异常）仍原样上抛。`tests/test_clipboard_dib_1355.py` 三例改成「跳过而不拦下」，`tests/test_wechat_guia_send.py` 的句柄用例同样改判（11 + 80 例通过）。最坏情况（一个格式都存不下来，例如剪贴板被别的程序独占）用户的剪贴板内容会丢——那是临时数据，重新复制即可，比整条回复发不出去划算。
- 第三次真机失败后的补丁（同日 22:44）：草稿自动清空第一次没有生效——发送主路径 `_attempt()` 里还留着一道 1.3.22 时代的老闸门（聚焦输入框后直接调 `_input_box_has_text()`，命中就 `send_failed`「输入框已有草稿，请手动清空后重试；本次未输入正文」），清空逻辑只接在预检/探测路径上，真机因此仍被这道老闸门拦下。现在改成：复核完整标题（`before_focus` 里刚做过，这时删掉的一定是本会话残留）→ `_clear_existing_draft()` → 再复核标题与前台 → 才继续粘贴。同时把清空判据放宽到能认光标：真机多行草稿删掉后输入框会变矮，拿清空前那个旧框去量墨迹会被驱动判「输入框布局改变」，而空白框本身又常被判成 `draft_state=text`、`pixels` 62~69、1 列（实测一个汉字 200~400 px）——现在 `composer_cleared` 说没清空（或直接抛异常）时，会重新抓一帧按「只剩光标（≤`CARET_INK_MAX=120` 且 ≤2 列）就算干净」复核，仍脏才拒绝。测试：`tests/test_wechat_guia_send.py` 81 例（含「有草稿时先 Ctrl+A+Delete 再正常发出」的成功路径）、`tests/test_auto_clear_draft1358.py` 9 例。
- 已知限制：清空是不可撤销的——本版不备份草稿内容（读草稿要靠 Ctrl+A/Ctrl+C，会动用户剪贴板，不做）；因此只在目标会话前台且标题核验一致时执行，切到别的会话一律不动。本版仍为内部测试候选，不制作正式安装包、不运行打包与发布脚本。

## 1.3.57 内部候选：回复不再默认拆两条，一次发完快一倍
- 真机症状：18:30 那条自动回复被拆成两条发出去，运行记录里两段各是一次完整发送——第一段 `send timing worker spawn_ms=4 rpc_ms=10319 guest_total_ms=9589`、第二段 guest_total_ms=9583，合计发送耗时 22.17 秒、入队到完成 28.98 秒。用户体感就是「机器人半天不回」。
- 先把钱花在哪量清楚（真机活窗口实测）：一次发送的五个阶段为 定位 0.49 秒 → 标题核验 1.26 秒 → 写入 3.76 秒 → 按发送键 1.35 秒 → 确认清空 2.72 秒，约 9.6 秒；而底层原语都很快——整窗抓帧 14 毫秒、布局分析 140 毫秒、发送键区域 OCR 10 毫秒、输入框有无文字 16 毫秒。也就是说时间不在算力上，而在逐个证据点的 fail-closed 取证和界面收敛等待（确认清空窗口本身就有 6 秒上限）。另有一处设计使然：`wechatauto.rhythm.gate('send')` 是随机人味延时，实测三次分别为 1.3 毫秒、4797.6 毫秒、3626.1 毫秒，属于反检测行为，本版不动它。
- 为什么不是「两段并进一个进程」：把两段合并到同一次发送尝试，只能省掉一次 GUI 初始化 0.19 秒、一次可用性探测 0.49 秒和第二段首次 OCR 变热的 0.8 秒，合计 1.5~2 秒（22 秒→20 秒），代价却是重构 `src/wechat_guia_send.py`（1389 行、80 条用例）。所以这一版换一条更划算的路：默认改成一条发完，22 秒直接变约 10 秒，而且不删任何一个载重校验。
- 改动一（默认值）：`src/plugins/chat_controls.py` 的 `default_settings['replySegments']` 由 2 改为 1；`on_post_reply` 的取值兜底同样改为 1；设置项标题改成「长回复拆成几条发（1=不拆，最快）」，说明里写清「实测每多拆一条就多一次完整发送（约 10 秒），想回复更快就填 1。」
- 改动二（老配置迁移）：`src/plugins/manager.py` 新增 `_migrate_single_segment()`，与 1.3.52 的合并窗口迁移同体例——只有盘上 `chat_controls.replySegments` 恰好等于旧默认 2 时才改成 1，用户自己设的 1/3/4 或非数字一律不动；迁移标记 `single_segment_1357` 写在 `plugin_settings.migrations.json`，如果设置没能落盘就**不写标记**，下次启动还能重来。
- 改动三（两处微小等待）：`src/wechat_guia_send.py` 删掉 `_paste` 之后那次重复的「输入框有没有字」复核（`_paste` 内部已用同一帧 probe 判过，失败文案保留），抓帧次数 24→23（无 probe 的退化路径 17→16）；按键后首帧观察由 0.5 秒改 0.2 秒，判据不变，`composer_cleared` 仍会在窗口内继续抓帧。
- 改动四（真机发现的剪贴板边角）：1.3.55 修好「剪贴板里是图片就拒绝发送」之后，20:48 真机又撞上两种读不出来的格式——CF_BITMAP 是别人的 GDI 位图句柄，去取它会在 GlobalLock 上抛 (6, 'GetClipboardData:GlobalLock', '句柄无效')；从 Excel/Word 复制时，应用私有格式 0xBFxx 会抛 (0, 'GetClipboardData', 'No error message is available')。现在快照根本不去取句柄类格式（CF_BITMAP / CF_METAFILEPICT / CF_PALETTE / CF_ENHMETAFILE），应用私有格式读不出来时跳过并把格式号留在 _Clipboard.skipped 里，只有标准格式读不出来、或者只剩位图句柄又没有 CF_DIB/CF_DIBV5 字节时才 fail-closed 拒绝（文案不变）。
- 测试补充：tests/test_clipboard_dib_1355.py 增 4 例（句柄格式从不去取、私有格式读不出时跳过、标准格式读不出仍拒绝、元文件句柄跳过并留痕），共 11 例。
- 一个没删的次序（有意保留）：`_paste` 里剪贴板快照之前那次「目标输入框是否为空」检查必须留在原处——真机出现草稿时，拒绝必须发生在写剪贴板之前，`tests/test_guia_sendcost1350.py` 的 `refuse_evidence()` 会断言此时剪贴板写入次数为 0。
- 测试：新增 `tests/test_single_segment1357.py`（8 例：全新数据目录默认 1 且不写标记、盘上 2 迁成 1 且落盘并写标记、自定义 3 保留、标记已存在则不再迁移、`_save_settings` 抛 OSError 时盘上仍是 2 且无标记、已经是 1 不写标记、分段为 1 时不再产出 PACE 控制标记）；`tests/test_reply_split.py` 默认断言 2→1；`tests/test_guia_sendcost1350.py` 的抓帧数断言同步为 23 / 16。
- 已知限制：想要「连发几条」的拟人节奏仍可把插件设置里的 `replySegments` 调回 2~4，只是每多一条就多一次约 10 秒的完整发送；本版仍为内部测试候选，不制作正式安装包、不运行打包与发布脚本。

## 1.3.56 内部候选：微信登了别的账号时不再无限重连
- 真机症状：17:03–18:22 之间运行记录每 ~37 秒刷一条「检测到微信切换账号（wxid_rerhua6zafil22_7ba3 → wxid_2rc4z7dz591922_c185），正在重新连接消息监听」，累计 60 条；期间用户发的消息一条也没收到，界面看着像掉了但又不报错。
- 根因：`src/wechat_app.py` 的 `_health` 每 15 秒用 psutil 读微信进程打开的数据库目录，只要与机器人自己的账号不同，就把 `bridge.online` 置 False 并写 last_error，走正常重连流程。而用户把微信登成了另一个微信号时，重连到原账号**永远不会成功**——该账号根本没登录，于是每轮都在拆监听、重建监听，日志刷屏且消息永远进不来。
- 修法：新增模块级判定 `account_mismatch_decision(runtime, current, active)`（`WeChatRuntime._mismatch_decision` 只是它的实例包装），把处置收敛成四态——`enter`（连续两次命中才认定"登录的不是机器人账号"，记下现场与时间）、`hold`（已在错位态且仍未切回，或这一次 psutil 读不到 active）、`clear`（active 变回机器人账号）、`none`（其余，走原有重连判定）；`_health` 只在 `enter` 时改 `online`、写一句人话原因，`enter`/`hold` 期间直接跳过重连，只保留 15 秒一次的只读复查。
- 提示文案：`微信当前登录的是另一个账号（{active}），机器人账号（{current_account}）不在线；请在微信里切回机器人账号后会自动重连（本程序不会自动换号）`；切回后自动写「微信已切回机器人账号，正在重新连接消息监听」并恢复监听。
- 可观测：`GET /api/status` 新增 `account_mismatch` 字段（`{}`=正常；非空时为 `{current, active, since}`），面板与脚本据此判断"该去切账号了"，不必翻日志。
- 刻度克制：一次 psutil 抖动不触发（沿用连续两次判定）；读取失败（active 为空）既不当作"还是别人"也不当作"已切回"，保持错位态不重连——真相不明时宁可不动。
- 测试：新增 `tests/test_account_mismatch1356.py`（6 例：单次抖动不判定、第二次命中才 enter、hold 期间现场稳定、读不到时保持 hold 且随后能 clear、切回后自动 clear 并重置计数、机器人账号未知时永不判定错位）。
- 已知限制：psutil 非管理员读不到他人进程句柄时 active 恒为空，此时既不会误判定也不会自动恢复，需要重启控制台；本版仍为内部测试候选，不制作正式安装包、不运行打包与发布脚本。

## 1.3.55 内部候选：剪贴板里是图片时不再拒绝发送
- 真机症状：17:52 一轮真实回复，标题核验已通过（`send phase=guia_check_title` 无 reason），却在写正文前失败：`send failed channel=guia reason=send_failed title=西莲寺紫`，错误文案「当前剪贴板格式无法安全备份，请先复制一段纯文本后重试；本次未输入」；inbox 置 failed、chat_outbox 0 行。
- 根因：用户刚用截图工具截过图，剪贴板里有 17 种格式，其中 CF_BITMAP(2) 被 pywin32 读成别人的位图句柄（int）。1.3.13 的 `_Clipboard.snapshot()` 见到任何非 str/bytes 就整体拒绝，于是「剪贴板里刚有图」= 所有发送都发不出去。但同一份快照里还有 CF_DIB(8)/CF_DIBV5(17) 的**字节**，恢复后系统会按需把 DIB 合成为 CF_BITMAP，跳过句柄并不丢内容。
- 修法（`src/wechat_guia_send.py` 的 `_Clipboard.snapshot()`）：先原样枚举全部格式再判定；`str/bytes` 照旧保存；CF_BITMAP 只在同一次快照含 CF_DIB/CF_DIBV5 时才跳过；CF_HDROP 的字符串列表按 pywin32 可回写形式保留；其余读不出内容的格式仍 fail-closed，报同一句「当前剪贴板格式无法安全备份…本次未输入」。
- 真机验证（未发消息，只碰剪贴板）：在用户当前那张截图上真实跑 snapshot→copy→restore，前 17 种格式、恢复后仍是 17 种（含 CF_DIB、系统合成的 CF_BITMAP、image/png），剪贴板文本回到原值（None），截图内容没丢。
- 测试：新增 `tests/test_clipboard_dib_1355.py`（6 例：有 DIB 时跳过位图句柄、无 DIB 仍拒、DIBV5 同样顶替、读不出的格式仍拒、CF_HDROP 列表保留、restore 全量回写）。
- 已知限制：CF_BITMAP 的原始位图句柄不会原样恢复（由 DIB 合成，粘贴结果一致）；只有同一份快照里存在 DIB/DIBV5 时才跳过句柄，否则仍拒绝；本版仍为内部测试候选，不制作正式安装包、不运行打包与发布脚本。

## 1.3.54 内部候选：标题识别按像素族分级 + 出站协议标记清洗
- 真机症状与定位：给「西莲寺紫」的私聊回复卡在标题核验（宿主 backend.log：`send phase=guia_check_title who=西莲寺紫 reason=target_not_open` → `send failed channel=guia reason=title_unverified title=`；inbox 置 failed、chat_outbox 0 行）。用该窗口真机帧（1266x749、125% DPI、标题墨迹 18px）跑网格实测发现旧三份变体（nearest 2×+硬阈值150 / smooth 2× / smooth 3×）在同一帧上分别读成错字、正确、残字，三份不一致 → 闸门按设计拒绝，错的是变体表，不是闸门。
- 主族改平滑三份：`src/wechat_pixel_gui.py` 的 `_MEDIUM_TITLE_PASSES` 改为 `((1.75,None,0,'smooth'),(2,None,0,'smooth'),(2.5,None,0,'smooth'))`（只用平滑放大、不二值化）。实测本机帧 `current_chat_title()` 由「6 变体全败」变为 `state=verified / family=medium / 0.86s`。
- 新增第三族覆盖旧真机像素：`_MEDIUM_TITLE_BINARIZED_PASSES=((2.5,150,32,'nearest'),(3,150,64,'nearest'),(4,150,0,'nearest'))`；`_title_pass_families()` 对主表返回 `medium → medium_fallback → medium_binarized` 三族，整族作废才换下一族，族内仍要求三份完整读数逐字一致（错字不算完整），全程不比较期望会话名。`TITLE_READ_BUDGET=8.0` 不变，成功路径只跑主族约 0.86s，全败仍 fail-closed 报 `ocr_budget`。
- 出站文本清洗：真机抓到 DSH 回合的回复以 `<acp tokens="35" type="text">m00006</acp>` 开头（模型把会话协议标记当正文吐出来）。`src/reply_text.py` 新增 `strip_model_markers()`，只删 acp 这一族的成对整段与落单标签，正文里其它尖括号（颜文字、比较符、代码片段）原样保留；接在 `src/wechat_app.py` 回复后处理链最前（PACE 标记清理之后、偏好清洗之前），命中时记 `message['_model_marker_stripped']` 并写一行运行日志「回复里混入会话协议标记，已清除后再发送」，不改动其它后处理。
- 测试：新增 `tests/test_reply_markers1354.py`（6 例：成对/多段/落单标签清除、非 acp 尖括号保留、空值、与 tidy_reply 串联）；`tests/test_pixel_title_medium1341.py` 家族序列改为三族（9 次调用）且 mode 断言变 `['RGB','RGB','RGB']`；`tests/test_pixel_sender1315.py` 期望变体序列含 `medium_fallback` 与 `medium_binarized`；`tests/test_title_evidence1327.py` 的真夹具 18px 标题经 `medium_binarized` 读出并全部通过。
- 已知限制：失败路径最多 9 次 OCR（成功路径不受影响）；协议标记清洗只认 acp 一族，将来出现新标记需再加一族正则；本版仍为内部测试候选，不制作正式安装包、不运行打包与发布脚本。

## 1.3.53 内部候选：DSH 记忆注入显式化与可核对
- 长期记忆不再「隐式进提示词」：新增 src/dsh_memory.py（纯标准库），把本轮真正要注入的记忆收成不可变清单（手动记忆 / 已确认长期记忆 / 学习结果 / 旧记录条数），render() 生成中文块钉在 DSH 人设尾部（标题「【本机长期记忆·本轮】」），不再受 prompt_budget 的 plugins 3000 字上限与「plugins 优先丢弃」影响。
- 上限与顺序固定：每条超 200 字截断、最多 12 条 / 1200 字，超限保前丢后并在块尾写「（还有 N 条未显示）」；排序固定 手动记忆 → 长期记忆 → 学习结果。
- 空与畸形一律降级：message 缺失、字段类型不对、清单里混进非字符串 → 整轮空清单（不抛异常、不改人设）；0 条记忆也照样留一行统计，能看出「本轮确实没有记忆可注入」。
- 续聊不再丢记忆：src/dsh_bridge.py 的 references 由「只在续聊发」改为每轮都发（新会话也带），18000 字符裁剪顺序改成先裁 records 再裁 references —— 记忆最后才被砍；records 仍只在新会话播种，避免每轮重复历史。
- 插件侧只做记账：src/plugins/conversation_memory.py 的 on_pre_ai 末尾把三类原文挂到 message 的 _memory_items 键上，既有插入顺序、文案、extra_system 返回值一字未改；插件被禁用或运行时没有 chat_state 时不写。
- 可核对：每轮运行日志一行「DSH 记忆注入 manual=n notes=n learned=n archive=n chars=n empty=n」；新增 GET /api/dsh/memory-injections 返回最近 20 轮统计（进程内环形缓冲，重启清零、不区分人设与会话），只回条数与字数，绝不回记忆正文。
- 面板：控制台绑定页新增一行只读文本，显示最近一轮注入统计与累计轮数，跟随该面板既有刷新节奏，不新增定时器。
- 测试：新增 tests/test_dsh_memory153.py（15 个自有 + 8 个继承，Ran 23 OK），覆盖排序/上限/截断/畸形输入不抛/统计只留条数/请求体在「新会话」与「续聊」都含 references 与人设记忆块/裁剪先丢 records/路由拒绝未绑定且响应不含正文；定向回归 test_dsh_bridge 24、test_wechat_app_flow 25、test_learning_156 19、test_memory_probe_153 20、test_plugin_hook_signatures 11、test_dialogue_rules_flow 17 全绿。
- 已知限制：手动记忆会同时出现在 plugins 块与新的记忆块（重复占少量 token，换来「即使 plugins 被预算整块丢掉，记忆仍在」）；统计是进程内不落库；仍为内部测试候选，不制作正式安装包、不运行打包与发布脚本。

## 1.3.52 内部候选：连发合并窗口 100ms→800ms（含一次性迁移）
- 连发合并窗口默认 100ms 太短：加载稍重时同一人连发两条会被拆成两次模型调用。src/plugins/reply_preferences.py:21 的 default_settings mergeMs 100 → 800，:14 字段上限 max 500 → 2000；src/wechat_app.py:1651 的兜底常量同改 800。最终生效窗口仍是 min(本窗口, burst_merge.capMs)。
- 老机器不用手改设置：src/plugins/manager.py 新增一次性迁移 _migrate_fast_merge_window()（:286-334），只把「reply_preferences.mergeMs 恰为 100 的数字」改成 800，用户手改过的其它值一律不动；迁移标记单独写在 data/plugin_settings.migrations.json（键 fast_merge_800），刻意不塞进 plugin_settings.json（那个文件按「插件 id → 设置」整体遍历）。
- 迁移不会假装成功：_save_settings() 失败只记 warning 且不写标记（避免「标记说已迁移、盘上其实还是 100」的永久半迁移），内存值不回滚；读标记失败按未迁移处理，下次启动重判；同一进程只尝试一次。
- 顺序有讲究：__init__ 里 _load_settings() → _migrate_fast_merge_window() → _load_plugins() → _reconcile_settings_with_schemas()，迁移必须早于 reconcile，否则缺省值先被补成 800，就再也看不出盘上原本是 100。
- 新增 tests/test_merge_window152.py 7 例：新数据目录默认 800 且不写标记、盘上 100 → 内存与盘上 800 且写标记、自定义 300 保留且不写标记、标记已存在则保持 100 不再迁移、_save_settings 抛 OSError 只 warning 且内存仍 800、plugin_settings.json 损坏时仍以 800 启动、只动 reply_preferences 不动 burst_merge。
- 测试侧同时修掉一个负载下的假阳性：tests/test_wechat_app_flow.py 的连发合并用例把两次 ingest 包进冻结时钟（FrozenWeChatTime），不再依赖「两次落地间隔必须小于 0.1 秒」这种与机器负载相关的前提；10 个占满 CPU 的进程下改动前 8 次跑 4 次失败、改动后 8 次全过，四条断言原样保留、窗口未放大。
- 真机只读推演：真机 reply_preferences 原本 mergeMs=100，迁移后内存与盘上均 800 并写出标记，顶层 33 个插件键集合不变；真实 plugin_settings.json 未被改动（size 5251、mtime 2026-10-31T13:29:04 前后一致）。
- 回归：tests/test_wechat_app_flow.py 25 例与 22 个 test_plugin*.py 全绿；wechatauto 包 mtime 仍为 2026-10-14 12:56:20，未改动。
- 仍为内部测试候选：不制作正式安装包、不运行打包与发布脚本（AGENTS.md 红线照旧）。

## 1.3.51 内部候选：跨机器安装与升级兼容（数据目录回退、升级自检、残留通道、回滚脚本）
- 跨机器兼容做成可见能力：新增 src/install_guard.py（数据目录可写探测与回退、升级数据只读自检、残留通道盘点与显式清理、回滚脚本生成、启动后台探测）与 6 条 /api/install-guard/* 接口（全部走既有管理员绑定），新增 src/compat_advice.py + GET /api/compat/advice 给出分级建议。create_app() 路由数 141 → 152。
- 数据目录写不进去时不再「看起来正常却存不下」：src/persistence.py 新增 probe_directory()，真实做一次「建文件→写→fsync→改名→删」；判定分两档——写不进文件才算不可写（blocking），只有改名被占用则标记「能写但原子替换退化，就地重写兜底」。resolve_data_root 只在确实写不进去时才按固定顺序换到 %LOCALAPPDATA%/WhaleGirlWeChat-fallback 或 %USERPROFILE%/WhaleGirlWeChat，整轮探测 2 秒墙钟上限，超时与探测自身出错一律「不换目录」，原目录的配置与聊天记录既不移动也不删除。
- 覆盖升级前能自证数据还在：upgrade_self_check() 只读统计配置项数、历史库 conversation_log 行数、插件设置键数、人设文件数、隔离通道配对，三态 ok/missing/broken（全新安装的 missing 不算故障，坏 YAML/坏库算 broken）；测试用前后文件快照钉住「自检绝不往数据根写任何东西」。
- 残留通道从静默跳过变成可见可清：channel_inventory() 分「核验通过 / 核验不过 / 忽略」三栏并给中文原因与建议；POST /api/install-guard/channels/remove 要求把 32 位通道编号再填一遍，目录必须直接位于 data/isolated_wechat，能通过核验的一律 409 拒绝，权限不足给「用启动（管理员）重开控制台」的中文指引，删除成功后写审计日志。sandbox_manager 的 prepare 语义与测试一字未动。
- 升级安装本身：installer.iss 120 → 243 行，新增 [UninstallDelete] 清回滚备份、tasklist 运行检测（查不出来按「没在运行」fail-open）与 PrepareToInstall 中文中止指引、以及 ssInstall 前把旧 exe 备份成 WhaleGirlWeChat.prev.exe（已有备份不覆盖，备份失败只提示不中止安装）。
- 回滚（只生成脚本，绝不自动执行）：GET /api/install-guard/rollback 只读报告有没有 .prev.exe，POST /api/install-guard/rollback-script 要求 confirm 严格等于「已停止微信发送并愿意重启控制台」才写出 rollback-prev.cmd（36 行纯 ASCII、CRLF，安装目录不可写时退到数据根，两处都不可写则中文失败原因且不落文件）。后端禁止调用 taskkill、禁止改名或覆盖 exe、禁止自动重启——控制台换不掉自己正在运行的 exe，脚本由用户关掉控制台后双击执行。
- 兼容性建议（GET /api/compat/advice）：8 类判定 data_root/wechat/ocr/windows_sandbox/dsh/port/isolated_channel/upgrade_data 各自独立 try，任何一项探测抛异常只降级成 info 并写明中文原因，接口绝不出 500；每条给 {key,severity,title,detail,fix,evidence}，severity 三档 blocking/degraded/info，evidence 只放版本号/DPI/端口/路径并复用 site_profile._redact 脱敏，不出现用户名、wxid、聊天内容与会话昵称。新增 leftover_residue 只统计数据根里旧版本留下的 *.bak / .bak-hot-* / *.tmp 等痕迹（单层遍历、不删任何文件、超过 200 条只报「至少 200 条」）。
- 控制台新增「环境与自检（换机器、覆盖升级前后先看这里）」区域：兼容性建议按三档分组显示、安装自检显示数据目录/历史条数/残留通道、清理残留通道必须把编号逐字符重填一遍（空列表时控件全禁用、无「一键全清」）、回滚小节先读备份状态再确认生成脚本。所有请求走既有异步 call_api，不在界面线程里同步阻塞。
- 隔离监听在慢机器上更可诊断：start_listening 单次 5 秒预算不变，新增 12 秒总预算内最多重试一次，只重试明确「请求没有落地」的 RpcTimeout/RpcBusy，其它 RpcError 与 generation 守卫、fail-closed 返回值全部原样保留。
- 跨机器路径与换机清单：新增 tests/test_cross_machine_paths151.py 覆盖含中文、空格、# 与 &、总长 >180 字符、盘符根五类路径下的原子写入/探测/自检/通道盘点（并断言 evidence 里不出现用户名），新增《跨机器安装与升级指南.md》（纯 LF）给出干净机器安装顺序、覆盖升级三个卡点、回滚步骤与 12 项可勾选验收清单。
- 仍为内部测试候选（1.3.32 起），不制作正式安装包；回滚只能回到上一版单份 .prev.exe，数据目录不自动迁移，残留文件只统计不自动删除。

## 1.3.50 内部候选：发送取帧合并（24→17）与 DSH 提供商目录兼容
- DSH 适配：settings/describe 的 namespaces 由旧版「{名字: 值}」字典改成 32 项列表（每项 {ns, value, ...}），src/dsh_bridge.py 新增 namespace_items() 做形状归一，describe_catalog 两种形状都吃；认不出的形状仍按原文案报错，旧字典形状行为逐字未变。GET /api/dsh/catalog 因此恢复可用：真机实测提供商 miku/mi/zu/dshapi（分别 3/11/1/13 个模型），默认 {provider: zu, model: deepseek-v4.1-flash}。
- 现场配置修复：这台机器原配置 model=deepseek-v4.1-flash-1017，而 DSH 现在该提供商只有 deepseek-v4.1-flash，session/selectModel 直接返回 session/model-unavailable（对话桥只报「DSH拒绝请求」）。改成 deepseek-v4.1-flash 后 DSH 主聊恢复；DSH 不可用时仍按 1.3.46 的规则由本地模型兜底一次，不重发、不二次点击。
- 发送取帧合并（src/wechat_pixel_gui.py + src/wechat_guia_send.py）：新增 probe_composer()，一帧内同时给出正文框、草稿判定、标题条指纹（need_ink 时顺带量墨迹）；新增 _title_region_cached() 复用标题区域解析；composer_cleared() 增加 first_ink 参数；_verified_title() 增加 signature 参数。同一次成功发送的驱动层抓帧次数 24 → 17，标题全族 OCR 与发送键结构 OCR 次数不变（各 1 次）。
- 判据一条未放宽：正文框越界仍由 _checked_input_box 拒绝；草稿非空仍抛 draft_not_empty；标题仍以全族 OCR 结果为最终比对（指纹只决定「要不要重读」，不参与「读到的对不对」）；发送键仍要求「恰好 1 个发送块」；按键后清空确认的窗口与判据不变（等量残留仍判未清空）。驱动不提供 probe_composer 时整条路径退回原样，调用计数不变。
- 预计节省：按真机「一次抓帧要几百毫秒」折算，保守约 1.6 秒（10.02 秒发送段的 16%），乐观约 3.0 秒（30%）；真机实测需要在启用后做一次真实发送。
- 新增 tests/test_dsh_catalog_shape150.py（12 例）与 tests/test_guia_sendcost1350.py（12 例），既有测试断言一条未改。
- 仍为内部测试候选（1.3.32 起），不制作正式安装包；跨机器安装/更新兼容排在下一版。

## 1.3.49 内部候选：像素定位失败可诊断、自检升级与端口可覆盖
- 新增 src/layout_hints.py：覆盖 22 个原因码，每个原因码给一句可操作的中文建议。
- 发送前的布局失败消息在原有前缀「输入框或工具栏没有唯一像素证据（<原因码>）」之后追加一段诊断：帧尺寸｜像素确认否｜闭合候选总数与可用数｜分栏 x｜被拒原因统计（如 left_of_chat_pane×5）｜box｜建议；同时写一条 warning 日志。
- 判据不变：不改变任何判据、阈值与原因码。典型差异是会话没打开时提示变成「微信当前没有打开任何聊天，请手动点开要回复的群/联系人再试」。
- 兼容性自检升级（在 1.3.48 新增的 /api/env 七项探测上）：Windows 名称改按 build 判断，build ≥ 22000 判为 Windows 11——注册表 ProductName 在 Win11 上至今仍写 "Windows 10"，不能再信它。
- 微信版本兼容门：4.1.x 判为正常；4.0.x 与取不到版本一律给警告「未验证，请先自检再测试一次」。
- 系统 OCR 缺中文包时给出可照做的安装路径：设置 → 时间和语言 → 语言和区域 → 中文 → 语言选项 → 可选功能 → 中文（简体）光学字符识别。
- 多显示器不再误报：点击通道已按虚拟桌面换算，SendInput 用 MOUSEEVENTF_VIRTUALDESK，负坐标副屏也正确。
- 端口可覆盖：环境变量 WECHAT_CONSOLE_PORT（非法值 / 0 / 65535 以上一律回落到 8091）决定控制台端口，src/desktop_startup.py 的 --port 默认值与 src/agent_mcp_connector.py 生成的接入地址都跟随它，不再写死 8091。
- 仍为内部测试候选（1.3.32 起），不制作正式安装包；真机端到端验收与冻结自检在后续步骤完成。

## 1.3.48 内部候选：兼容性自检（机器/环境特征探测）
- 新增只读探测模块 src/site_profile.py：七项固定顺序——scale 系统缩放、screen 屏幕与工作区、wechat 微信进程/版本/主窗尺寸、ocr 系统 OCR 中文语言包、windows_sandbox Windows 版本与沙盒可用性、port 端口占用、data_root 数据根可写与剩余空间。
- 每项独立 fail-soft：单项异常一律降级为 unknown 并附一句原因，绝不向外抛；任何一项失败都不影响其余六项。
- 新增接口 GET /api/env：一次返回七项探测结果，只读、不改配置、不落盘。
- 控制台页签新增「兼容性自检」按钮与只读结果框：点了才探测，结果只展示，不改动任何设置。
- 三条不变式：只读（不写文件与配置）、不联网（不出本机）、绝不调用 SetProcessDpiAwarenessContext 等会改进程 DPI 虚拟化的函数（避免打乱控制台与微信自身的窗口坐标）。
- 脱敏：返回结果里的用户名与聊天标题会脱敏后才外露。
- 仍为内部测试候选（1.3.32 起），不制作正式安装包；真机端到端验收与冻结自检在后续步骤完成。

## 1.3.47 内部候选：插件钩子护栏、清单与安装器加固、分级扫描、群友外号、插件审计
- 钩子超时护栏：on_ingest/on_observed/on_block/on_pre_ai/on_sent 改走有界执行（SYNC_TIMEOUT = 8 秒），describe 30 秒、generate 60 秒（src/plugin_dispatch.py）；超时只记一次故障，不再让一个卡死的插件堵住整条链路。
- 故障记录加厚：每条故障带 failures / timed_out / quarantined 与调用栈帧（src/plugin_health.py 的 _frames 只存栈帧、不存异常全文）；隔离线仍是同一插件同一阶段连续 3 次失败（FAILURE_LIMIT = 3）。
- 清单契约：新增 src/plugin_manifest.py，plugin.json 两种方言归一（规格方言 entry "main.py"、内核方言 "main.py:setup"），api_version 只认 "1"，entry 拒绝绝对路径、盘符、反斜杠与 ..（MAX_ENTRY_DEPTH = 3）。
- 安装器加固：先写临时文件、试加载通过才 os.replace 覆盖，坏插件绝不落盘；overwritten 如实回传；不再依赖 17 条正则黑名单，压缩包里的 plugin.json 会被真正读取（src/plugin_installer.py）。
- 分级扫描：新增 src/plugin_scan.py，按 clean / risky / forbidden 三级静态检查（os.system、subprocess、eval、exec、compile、__import__、ctypes、pickle.loads、__globals__ 判 forbidden；写文件、删文件、网络、importlib 判 risky）；inspect 回执多一个 scan 字段，只作提示，不参与"能不能装"的判定。
- 群友外号：新增插件 member_aliases（默认关闭），只认第一人称自述（我叫X / 叫我X / 我的外号是X），命中才往提示词加几行，口令只在主人私聊里认；状态落 data/wechat-only/plugin_state/member_aliases/member_aliases.json，坏文件按空表处理。
- 插件审计：新增 src/plugin_audit.py，每次插件调入写一行 JSONL（插件、钩子、耗时、成败、异常摘要 200 字），单文件 2MB 滚一次，绝不记消息正文；环境变量 WECHAT_PLUGIN_AUDIT=0 可关；GET /api/plugins/health 多返回最近 100 条 audit。
- 批量安装回执补"首个失败原因：<120 字>"：以前只报"N 个失败"，现在能一眼看出坏在哪。
- 契约测试扩到 12 个钩子入口（tests/test_plugin_hook_signatures.py），新增 tests/test_plugin_reply_guards147.py 锁回复闸门。
- 仍为内部测试候选（1.3.32 起），不制作正式安装包；真机端到端验收与冻结自检在后续步骤完成。

## 1.3.46 内部候选：DSH 主聊提速与本地兜底（预检缓存/超时 30 秒/防复读/失败兜底）
- 预检缓存：DSH 每轮都做的连通性预检改为缓存，成功结果缓存 60 秒（PROBE_CACHE_TTL = 60）；缓存命中时日志打「DSH 预检缓存命中，跳过探测」。
- 缓存失效：DSH 配置（含地址/模型/provider/超时/profile）或人设发生变化即视为不同键，缓存自动失效；探测失败不写缓存，下一次仍真探测。
- 手动动作不变：「探测」（POST /api/dsh/probe）与「连接」（POST /api/dsh/connect）每次仍做真实探测，不吃缓存。
- 默认超时 60 秒 → 30 秒：src/config_manager.py 的 dsh.timeout 默认 30，POST /api/dsh 的 timeout 校验范围 5～120 秒。
- 防复读继承：DSH 回合生成前会把最近已经说过的句子（最多 5 句）拼进系统提示，措辞为「这次绝对不要使用或改写下面这些已经说过的句子：…」。
- 失败兜底：DSH 超时或抛异常时，本轮改由本地模型兜底一次，日志记「DSH 未响应，已本地兜底：<原因>」；兜底不重发、不二次点击发送键。
- 成功路径不变：DSH 成功后仍标记该回合为 DSH 生成并进入原有微信发送队列；分段与节奏（chat_controls 的 post_reply）对 DSH 回合本来就生效，本次未改。

## 1.3.45 内部候选：提示注入预算（分块上限/整块丢弃/注入构成日志）
- 新增 src/prompt_budget.py：把系统提示拆成 persona / continuity / plugins / rules / image 五块，固定顺序组装。
- 分块上限（字数）：persona 3000、continuity 400、plugins 3000、rules 600、image 600；总预算默认 6000 字。
- 超块截断：单块超上限时从尾部截断，并留标记「…（<块名> 超长已截断 N 字）」。
- 超总预算丢块：按 DROP_ORDER = plugins → image → continuity → rules 整块丢弃；persona 永不丢弃。
- 配置覆盖：prompt_budget.maxChars 改总预算，prompt_budget.partCaps.<块名> 改单块上限；不配置时用默认值。
- 注入构成日志：每轮组装后写入一行「注入构成」，列出各块字数、是否截断、是否丢块，便于核对预算。
- 插件归因：插件注入新增 extra_system_parts 记录每个插件的字数归因；原有 extra_system 字符串拼接方式未改，行为与旧版一致。
- 实测（当前人设）：persona 2466、continuity 96、plugins 462～670、rules 172～180、image 299，典型总计 2744～3044 字，回归中无丢块。
- 为什么 persona 上限是 3000：当前生效人设约 2466 字，留约 20% 余量；若压到 2000 会在人设较长时截掉末尾的身份锁。

## 1.3.44 内部候选：DSH 主动上报（地址自愈/安全升级/启动刷新/连接器缺陷）
- 主动上报接口：新增 `POST /api/dsh/announce`，地址一律由控制台自己校验后才落盘，校验不过一律不写配置（fail-closed）；带地址时走一次 probe，不带地址时走一轮端口发现，只有唯一已验证候选才落盘，否则返回 409 并附发现状态与提示（`src/dsh_api.py:109-152`、`src/dsh_discovery.py:67-123`）。
- 控制面登记：新增操作名 `dsh.announce`，沿用既有控制通道 `POST /api/agent/control/execute`，按既有规则需 `manage_console` 能力并进审计，连接器不需要新增专用通道（`src/agent_control.py:49`、`:92`）。
- 连接器启动上报：MCP 连接器启动时用后台线程尽力上报一次，3 秒超时、任何异常静默、stdout 只允许 JSON-RPC 协议内容（失败也不会污染协议通道）；DSH 重启换端口后由此触发控制台自愈（`src/agent_mcp_connector.py:104-116`）。
- 旧连接器安全原地升级：只认「控制台生成的」内容特征，模板首行标题、`def endpoint()`、`def control(name, args)` 三条件同时命中才升级，先落 `.bak-时间戳` 备份再覆盖；用户改过的仍拒绝覆盖，备份失败则不覆盖（`src/agent_mcp_connector.py:167-227`）。
- 控制台启动刷新：lifespan 里尽力把已存在的连接器刷成当前模板；文件不存在绝不新建、用户改过不动、任何异常一律吞掉，不影响控制台启动（`src/wechat_app.py:2345-2352`、`src/agent_mcp_connector.py:234-266`）。
- 上报可见：`GET /api/dsh` 新增 `last_announce`（只存内存，控制台重启回到 null）；DSH 面板新增「DSH 主动上报」只读行，三态为失败 / 已连 / 未上报，已连时显示最近一次地址；stage 不在进行中时给出固定提示：在 DeepSeek Harness 新建会话并选择预设“鲸鱼娘工作台”，控制台会自动重新发现 DSH 地址（`src/dsh_api.py:91-95`、`src/gui/dsh_panel.py:21-22`、`:66-73`、`:410`、`:413-416`）。
- 断连行为明确（文档化，逻辑未改）：DSH 不可达即记失败并继续接收消息，不排队、不重发、不回放。
- 顺带修掉两处缺陷：`src/dsh_mcp_provision.py:209-217` 的写分支把结果赋错变量导致 `KeyError`；连接器升级路径原本会拒绝覆盖控制台自己生成的旧版模板（`src/agent_mcp_connector.py:174-227`）。
- 测试：新增 `tests/test_dsh_announce144.py`（10 例）、`tests/test_connector_announce144.py`（7 例）、`tests/test_connector_upgrade144.py`（9 例）、`tests/test_connector_startup_refresh144.py`（9 例）；实测 `test_*144.py` Ran 35 OK、`test_runtime_identity.py` Ran 9 OK。
- 契约补齐：视频抽帧通道动作 video_frames 早已在实现与插件中使用（通道不支持时优雅降级），本轮把它补进契约第 5 节动作表，并修掉据此硬编码、此前一直失败的安全守卫测试。

应用版本 1.3.44，仍为内部测试候选；功能验收前不制作新的正式安装包。

## 1.3.43 内部候选：控制台接入体验（地址/发现/诊断/模型下拉/凭据可见）
- 地址规范化：此前必须手写 `http://` 前缀，裸填 `127.0.0.1:端口` 直接报错。现在自动补 `http://` 前缀，并按四种原因分别给文案：不是 http、不是本机（带路径或凭据）、端口非法、端口为空；放行范围不变，仍只接受本机 http，且不带路径、查询和凭据（`src/dsh_bridge.py:75-92`）。
- 端点发现分因：`POST /api/dsh/discover` 新增 `state`（`ok`/`drift`/`process_missing`/`port_unreachable`/`protocol_failed`）、`saved_base_url`、`drift{from,to,from_port,to_port}`、`hint`，原有 `success`/`candidates`/`process_count`/`checked_ports`/`unverified_ports`/`message` 全部保留；探测超时同样返回可执行的下一步（`src/dsh_discovery.py:67-123`、`src/dsh_api.py:21-34`）。
- 端口漂移：已保存地址与实测端口不一致时给横幅「更新为 N」，只有用户点了才把新地址写回并立刻重新探测；不自动改配置、不自动启用（`src/gui/dsh_panel.py:319-363`）。
- HTTP 失败可诊断：新增 `DshHttpError`（status/rpc_id/code/body），`POST /api/dsh/probe` 失败返回 `detail{status,rpc_id,code,body,hints}`，按 provider、model、reasoning、凭据四类给建议；报文一律脱敏 `dsh-auth-*`、token、cookie（`src/dsh_bridge.py:33-72`、`src/dsh_api.py:84-91`）。
- 提供商与模型改下拉：新增 `GET /api/dsh/catalog`，数据源是 `settings/describe` 的 `llm-*` 命名空间，合并 providers、去重并附 agent-default-model；取目录失败降级为 HTTP 200 并保留已保存值，当前值不在目录时下拉里显示占位项并提示「保存会保留原值」（`src/dsh_bridge.py:197-343`、`src/dsh_api.py:36-45`、`src/gui/dsh_panel.py`）。
- 术语统一与两面板互跳：凭据对话框可跳 DSH 设置，DSH 设置可跳接入凭据。
- 凭据生命周期可见：凭据行外露 `issued`/`last_used`，界面显示「创建 / 剩余 / 最近使用」，缺失字段显示 —；`last_used` 60 秒节流写盘，写盘失败只记日志、不影响鉴权结果（`src/agent_authority.py:75-84`、`:137`、`:162-171`）。
- 顺带修掉的既有缺陷：`DshError` 继承 `ValueError`，旧代码把「请填写DSH端口」放在 `except ValueError` 内，导致端口为空被误报成「DSH端口无效」。
- 测试：新增 `tests/test_dsh_endpoints.py`（23 例）、`tests/test_agent_authority_lifecycle.py`（5 例）；实测 `test_dsh*.py` Ran 70 OK、`test_agent*.py` Ran 93 OK (skipped=1)、`test_background_199.py` Ran 7 OK、`compileall src` 退出码 0。

应用版本 1.3.43，仍为内部测试候选；功能验收前不制作新的正式安装包。

## 1.3.42 内部候选：隔离沙盒发送成功却被判「结果不确定」
- 现象：隔离沙盒里微信真实收到了回复（客人端截图可见绿色气泡、输入框已清空），控制台却报「已尝试发送但输入框未确认清空，结果不确定，本次不重发」，再被父进程报成「超过发送时限…总耗时约 20 秒」，消息记录停在不确定状态、发送卡 `delivery_verified` 保持 false。
- 定位：确认阶段依赖「光标熄灭」才认输入框为空，而隔离沙盒走 PrintWindow 取帧、光标层几乎一直在位图上，空输入框恒被判成 `caret_like`，`_input_box_has_text` 只能在重试到超时后返回「有文字」；同时点击前已花掉 16.2 秒（客人端日志 `18:40:18.540 guia_check_title` → `18:40:26.277 guia_input` → `18:40:30.883 guia_click_send`），点击后只剩约 4 秒给确认。
- 修改：确认改为墨迹对比——按下发送键前记录输入框墨迹像素与横向墨迹列数，点击后在 6 秒窗口内重新取帧，`empty` 或 `caret_like` 且（列数 ≤ 2 或像素降到原值 30% 以下）即判已清空；单个标点粘贴后墨迹不变，仍判不确定，不会误报成功。点击前另加 2.5 秒预留，剩余时间不足时不按键并明确报「未按发送键」。按键后不再重复标题 OCR（点击前已校验两次），避免把已成功的发送重新翻成不确定。绝不重发、绝不二次点击。

## 1.3.41 内部候选：隔离沙盒里中等字号会话标题读不出来导致发送被拒
- 现象：隔离沙盒内实际发送被拒，报「标题识别未通过，本次不继续输入或提交：未能核验完整标题」，配对停在 waiting_code=title_unverified，消息收得到但发不出去。
- 定位：会话标题墨迹高度 18 像素时走中等字号 OCR 变体表 ((2.5,150,32),(3,150,64),(4,150,0))，在沙盒 125% 缩放下三个变体全部读回 0 行（state=ocr_empty）。
- 修改：中等字号变体表改为真机实测可读的三组（NEAREST 2 倍 + 阈值 150、平滑 2 倍原图、平滑 3 倍原图），OCR 裁剪新增重采样参数；聚合规则改为「所有成功完整读到的文字必须一致且至少 3 个，任何一次读到不同文字仍然拒绝」，空结果不再立即中断。完整墨迹覆盖校验、逐字一致、不按期望会话名挑选结果、读不出就失败，全部保持不变。

## 1.3.40 内部候选：隔离后台不再「超时即掉线」，重启窗口消息不再被当历史跳过

两处接收侧修复，都不改变发送链路。一是隔离通道轮询：以前客体稍微慢一点（单次轮询超过 3 秒）就整条消息监听被拆掉，宿主日志每 60~100 秒重复「隔离监听已停止：隔离请求超时」→「5 秒后重连」，接收在这些窗口里完全停摆。现在区分「变慢」与「客体不可信」：超时按 0.5 秒起、上限 2 秒退避重试，连续 60 秒吃满预算才按原样拆线并置离线；协议、身份、摘要、心跳不符仍立即失败。轮询单次预算 3 秒→10 秒、轮询间隔 0.25 秒→1 秒，降低对共享目录与消息库解密的压力；同一场超时风暴只记一条「隔离通道响应变慢，正在重试」，恢复时记一条「隔离通道已恢复」。二是监听水位基线：库只在成功投递后才落盘水位，冷启动一条都没投递时水位文件根本不存在，于是每次新建监听都把每个会话重新播种成「当前最新序号」，而增量查询是严格大于——停机窗口里到达的那条消息序号恰好等于播种值，被永久跳过。现在播种完成后立刻强制落盘一次基线，重启后从基线继续，窗口里的消息照常投递；安装前的历史消息仍然跳过（首次启动的上下文不变）。超过 wechat.max_message_age（默认 25 秒）的补投消息仍按原规则判过期，不会产生迟到回复。

应用版本 1.3.40，仍为内部测试候选；功能验收前不制作新的正式安装包。


## 1.3.39 内部候选：控制台直接打开 Windows 沙盒

沙盒面板新增「打开 Windows 沙盒」按钮：以前必须自己从开始菜单点开 Windows 沙盒，现在由控制台自己生成 .wsb 配置并启动一台，等实例出现后把实例编号填进面板的实例下拉框并刷新状态。同一排还新增「在沙盒内安装微信」按钮：选一个本机的微信安装包（.exe），控制台把安装包所在目录只读映射进沙盒的 `C:\WhaleInstall`、把宿主日志目录映射成可写的 `C:\WhaleOut`，再执行 `C:\WhaleOut\install.ps1` 静默安装，随后每 5 秒查一次沙盒里的 `C:\Program Files\Tencent\Weixin\Weixin.exe`，最多等 300 秒；沙盒内已装好就直接复用，不重复运行安装包。

启动逻辑在 `src/sandbox_manager.py`，配置文件写在 `data/sandbox_launch/` 下并且只清理过期的旧配置；启动前先查 `wsb list`，已有实例就直接复用、绝不启动第二台，等待新实例的上限是 120 秒、每 2 秒轮询一次。失败一律给出稳定原因码与中文说明：`sandbox_exe_missing`（找不到 WindowsSandbox.exe）、`config_write_failed`（写 .wsb 失败）、`launch_failed`（启动进程失败）、`launch_timeout`（120 秒内没等到实例）。

安装同样是控制台能力：入口是 `POST /api/sandbox/install-client`，干活的是 `src/sandbox_manager.py` 的 `install_client()`，脚本与日志落 `data/sandbox_install/`；安装包必须是已存在的绝对路径 .exe，符号链接和相对路径一律拒绝，同一时间只允许一次安装（并发请求返回 409）。失败同样只给稳定原因码与中文说明：`installer_missing`（安装包不存在）、`sandbox_not_running`（沙盒未打开）、`share_failed`（共享目录失败）、`exec_failed`（客体拒绝执行脚本）、`install_timeout`（300 秒内没出现微信主程序）；已装好时返回 `already_installed`。

版本号升到 1.3.39，仍是内部测试候选：功能验收前不制作正式安装包。本次新增「打开 Windows 沙盒」与「在沙盒内安装微信」两条能力，发送链路与隔离通道逻辑均未改动。

## 1.3.38 内部候选：新会话首条消息丢失与僵尸监听回收

修复新会话的第一条消息永久丢失：库给新会话定初始水位取的是最新一条消息的 `sort_seq`，而增量查询是严格大于，让会话出现的那条消息恰好等于水位，于是永远取不到（用户表现：新加的人或新群第一次说话机器人不理）。`src\wechat_listener.py` 的 `ListenerDatabase` 新增 `get_messages`，只在「取单条最新消息当水位」这一种调用上把 `sort_seq` 减 1；启动时就已存在的会话原样透传，仍不重放历史；真实数据不改动（返回的是副本）。

修复停止接收只关闸门不回收监听线程的僵尸监听：`src\wechat_bridge.py` 的 `stop_receiving` 现在清空并真正 stop 监听实例，stop 放在锁外以避免回调线程等锁导致 join 超时；此前每次重连都会留下一个旧监听线程，每 0.25 秒读库并与新实例争抢同一份水位文件。新增离线回归覆盖新会话首条消息的初始水位与副本隔离、重复投递只发生一次，以及停止接收后旧监听线程被真正回收。

本版仍为内部测试候选。离线回归不代替真实微信环境下的新入站自动回复、监听与重连状态、日志界面和独立发送回执的验收；功能验收前不制作新的正式安装包或交付归档，保留现有配置、历史与配对。

## 1.3.37 内部候选：隔离后台首次失联状态一致性

修复隔离代理第一次失联时，状态已显示断开但发送环境仍残留 `ready=True` 的矛盾。现在先使连接失效，再生成同一次状态快照；首次和连续失联均返回 `online=False`、`read_state=disconnected`、`send_state=disconnected` 与 `send_environment.ready=False`，并保留明确的隔离代理不可用原因。

正常在线状态、发送与监听流程、RPC、当前会话限制和超时预算保持原有规则；代理重新出现不能仅凭旧的就绪缓存自动恢复收发。新增离线回归覆盖首次两类失联、连续失联、正常与发送等待状态、服务恢复及快照副本隔离。

本版仍为内部测试候选。离线检查不代替系统重启后的沙盒稳定性、真实新入站自动回复、独立发送回执和日志界面验收；功能验收前不制作新的正式安装包或交付归档，保留现有配置、历史、配对与原始验收证据。

## 1.3.36 内部候选：升级身份核验与守护诊断

本机旧 1.3.18 配对目录受历史权限限制，需由具有读取权限的管理员控制台核验；准备失败时保留原目录 ACL 和配对。界面单元测试现统一隔离后台启动、网络和进程操作，并在解除隔离前销毁窗口及延迟对象，避免测试启动工程后台或干扰实际控制台。

修复升级等待配置为 `Infinity` 等非有限数时可能永久等待的问题；默认仍为 45 秒，正有限配置继续支持。隔离代理先核对原配对的构建身份，再给出旧版本原地升级提示。构建不一致或 by_build 缺少构建证明时，状态保持 `build_mismatch`，通用“准备”也明确拒绝，不停止、不共享、不启动代理；保留原配对和诊断。合法旧版本与原 legacy 兼容规则保留。

1.3.34 的正常接收来源接管修复仍保留；本次补上日志输出失败时的保护，避免已关闭的终端或异常日志输出中断健康轮询。状态新增独立 `health` 诊断，任务存活与最近完成的检查分开报告；检查恢复后仍保留上次失败类型与时间，不导出异常正文。停止、安全暂停、任务所有者、收发代次和取消规则不变。

新开的 Windows Sandbox 是新的临时实例，不能沿用已关闭实例的 ID 或假定安装和登录仍在。以实时清单为准；当前版本遇到合法旧代理时，第一次“准备”就可能直接升级，不应按旧交接连续重复准备。升级不启动机器人、不连接微信、不发送消息，也不切换会话。

本版本仍为内部候选。旧版后台单次发送成功记录不替代当前版本的新入站自动回复、监听重连和界面验收；正式包须等待功能门槛全部通过，不复用已经消费的单次发送授权。

`GET /api/status` 的 `health` 为独立对象：`task_alive`（布尔，健康任务未结束）、`check_state`（`not_checked/ok/error`，最近完成的守护检查结果）、`last_check_at`（Unix 秒或 0）、`last_failure_type`（最多 64 个 ASCII 标识符字符或空串）、`last_failure_at`（Unix 秒或 0）。`ok` 只代表检查执行完成，不代表微信在线或发送就绪；连接结果仍读取 `connection`。异常恢复后保留上次失败，旧任务不能覆盖接管者的诊断。

## 1.3.35 内部候选：隔离代理原地升级与在线状态真实化

1.3.34 的隔离后台在本机 HTTP 上完成了 1.3.33→1.3.34 的一次原地更换：`POST /api/sandbox/prepare` 复用该沙盒的专属通道并返回 `stage=agent_ready`，随后 `/api/sandbox/status` 该对为 `agent_online=true`、`app_version=1.3.34`，全程不需要管理员权限，也不需要重装或重新配对沙盒。

本候选先修状态真实性：`_pair_status` 现在先读隔离代理心跳（`live_service`）再判版本。心跳新鲜但版本与控制台不一致时，该对仍标 `stage=version_mismatch`（不改写成就绪），但 `agent_online=true`，原因改为「隔离代理在线，版本 X 与控制台 Y 不一致；可原地升级代理，升级不会启动机器人」。心跳过期、沙盒未运行或构建身份不一致时保持原语义：不产生 `version_mismatch`、不声称在线、仍为 fail-closed。

控制台「隔离后台」页对「同沙盒、`stage=version_mismatch`、`agent_online=true`、机器人未运行」的配对显示「原地升级代理到 X.Y.Z」按钮，点击走与 `POST /api/sandbox/prepare` 完全相同的服务端路径（`prepare_or_recover` → `_prepare_sandbox` → `_upgrade_pair`），保留微信登录与当前会话，进度和最终结果显示在面板内。按钮文案写明不会启动机器人、不会自动连接微信、不会切换会话；机器人运行中或 `_sandbox_preparing` 为真时按钮禁用并给出原因，与原有 409 语义一致。`/api/sandbox/status` 同时新增布尔字段 `preparing`，供面板判断配对是否进行中。

升级的停止与启动等待由固定 20 秒改为默认 45 秒，可用 `WHALE_SANDBOX_UPGRADE_STOP_TIMEOUT`、`WHALE_SANDBOX_UPGRADE_START_TIMEOUT` 覆盖，慢机器冷启动不再假失败。epoch 与心跳核验、`_session_probe`、共享失败与超时的 uncertain 语义、`require_bound` 鉴权、运行中机器人拒绝 prepare 的 409 全部不变。

真实限制只有一条：准备用的 `app_dir` 取自当前控制台进程 `sys.executable` 的父目录（`src/sandbox_api.py:119-120`），所以原地升级必须由当前版本的控制台进程自身发起；旧后台占用 8091 时由 `wechat_gui.terminate_conflicting_backend` 结束冲突进程后再准备。不存在「升级需要管理员权限」或「必须人工重装/重新配对」的限制。功能验收前仍只做内部候选，不生成安装包或交付归档。

## 1.3.34 内部候选：监听代次、收件健康与界面状态

1.3.33 已在外层沙盒最小化时完成一次授权发送：接口确认成功，独立只读回执取得唯一新增自身消息及非零服务器消息标识；335 次完整主机采样中前台与物理光标均未变化。这证明本次当前会话发送验收通过，不代表对方已读、所有环境兼容或新入站自动回复已验收。

本候选修复收件与界面状态：旧沙盒 poll 的迟到成功、失败、事件和 finally 必须核对当前 stop 信号、接收代次和实际工作线程，不能覆盖新一代账号或收件状态。监听状态以实际存活且未停止的本地线程为依据，本机 wechatauto 只适配已核验的 Listener._thread/_stop 字段，未知形状不能当作正常。在线但接收线程停止也会触发现有重连守护；退避与重连期间继续核对桥、回调来源、发送代次、停止及安全暂停，不能复活旧来源、补发旧消息或解除暂停。启动返回成功但线程未存活，仍明确判为失败。

刷新监控可以在同一运行中接管接收来源，原健康任务须在下一轮重新捕获当前来源继续检查；桥、传输或发送代次已更换时退出旧轮。健康任务另核对自身所有者，旧任务不能附回旧回调或清除新任务的重连标记；停止和安全暂停仍立即退出。

同次签名沙盒轮询还必须提供布尔型的客体监听状态；未知状态在身份回填和事件分发前拒绝。客体明确停止时，本批有效事件先走原接收和 ACK 路径，再停止主机轮询触发重连，不能以主机线程仍活着掩盖客体停止。

本机和沙盒的控制台均将服务运行、读取连接、接收状态和发送就绪分开显示；运行中但接收停止会显示警示，不再以发送就绪绿灯掩盖。原日志搜索、级别、历史错误日期、暂停滚动、复制与导出保持。线程存活仅表示轮询线程存在，不能证明数据库持续取得新消息；实际新入站到单次自动回复、停止及重连仍需验收。发送核心、当前会话范围、三份标题 OCR、草稿保护、单次提交和 18/24/25 秒预算不变。功能验收前仅制作内部候选，不生成正式安装包或交付归档。

## 1.3.33 内部候选：减少发送确认链路的无效等待

1.3.32 的 R2 单次验收已由独立只读回执确认唯一新增自身消息及非零服务器消息标识，但发送接口仍在确认阶段超时，主机前台与鼠标见证也未通过，因此尚未完成后台功能验收。本候选仅处理已证明等价的耗时：用同一帧的完整 RGB 掩码替代标题墨迹、字号、完整性和草稿的 Python 逐像素集合；仍检查全部像素和孤点，只移除原精确模板核验的展开控件。发送按钮保留全部唯一绿块、字形结构、区域和窗口几何核验，仅停止等待完全不参与返回结果的可选加分 OCR。当前会话标题仍逐次取新帧、每次三份完整 OCR 一致，提交前完整正文回读、空草稿、单次提交和 18/24/25 秒预算均不变。离线等价与速度结果不能代替真实端到端验收；主机无干扰、新入站自动回复、监听/重连与日志状态仍须逐项通过。功能验收前仅构建隔离目录内的内部候选，不制作正式安装包或交付两件套。

## 1.3.32 内部测试候选：保留诊断并避免超时堆栈自身崩溃

1.3.31 的 Q 单次发送返回未提交，独立检查没有新增自身消息；发送进程在十秒堆栈诊断时崩溃，故障位于 Python 的行号计算。同源 Python 的纯离线短命线程对照已两次复现原诊断访问冲突，替代 Python 诊断两次正常完成。本候选仅替换超时堆栈采集方式：在 Python 线程中保留 frame 对象引用，生成有界的文件名、函数名和行号后再写日志，不读取或记录源码、局部变量、正文或凭据。仍在十秒采集，保留原轮转与取消，诊断失败不能改变发送结果；若原生扩展始终持有解释器锁，Python 诊断线程可能无法运行，外层超时终止与阶段文件仍生效；18/24/25 秒发送与 RPC 预算、精确当前会话、空草稿、完整回读和单次提交保护保持。本版尚未通过真实后台发送验收，不生成新的正式安装包或交付两件套；仅允许必要的内部冻结测试构建。

## 1.3.31：后台发送预算内的定位与请求发布

1.3.30 的一次隔离后台发送未取得新增自身消息回执，不能视为发送成功；现有草稿和消费记录保留，失败不补发。1.3.31 处理实测的两处延迟：像素边缘采样仅在前、中、后三点颜色完全相同时直接返回无边缘，这是原差值为零的等价计算，其他采样位置、阈值、输入框闭合、标题、草稿及提交前核验全部保留；只对已预建的 control/request.json 原位独占发布同一份签名请求。不可变 canonical 请求仍先独占落盘，同一 UUID、epoch、摘要和原截止时间不变。仅取得写句柄前的共享冲突可在剩余原预算与 50ms 中较短时间内等待；槽丢失、权限拒绝、写入/截断/刷新/关闭失败都不重建、不重写或回退。原 RPC/发送时限、单次输入提交、未知不重发及当前已打开会话范围保持。离线等价与速度验证不代替真实后台消息验收。

## 1.3.30：隔离 OCR 可变槽的独占发布

真实映射测试中 ReplaceFileW 导致客体缺名缓存，原六秒内纯读取恢复仍可能耗尽；不能用延长发送预算解决。OCR host publisher 仅处理固定 ocr/relay.json 及严格 UUID.result.json：既有槽用 OPEN_EXISTING、GENERIC_WRITE、share-mode 0 取得互斥后一次写入、截断、刷新、关闭。仅取得句柄前 WinError32 允许在同次截止时间内等待；错误2/5、短写、写入/截断/刷新/关闭失败均不重新写入或降级。不扩大到普通 RPC、配对、事件或消费文件，不在输入/发送端增加重试。结果必须保留原签名 OCR 请求的 ID、epoch 和 deadline，最多用剩余时间与100ms中较小预算取得写句柄；心跳取得预算50ms。首次心跳可以独占创建，客体预建的结果槽丢失不能重建。关闭中继只停止线程并保留槽，心跳自然按原8秒期限失效；有效期内可用信号本身不证明识别或提交，仍须本请求新签名回执。客体读取恢复上限与原识别六秒期限保持原规则。54次只读原型识别不替代正式消息验收。

## 1.3.29：Qt 主窗口的样式角色

实测同账号、同 PID、同 Qt51514QWindowIcon、同“微信”标题的功能说明没有 GW_OWNER 或 WS_EX_TOOLWINDOW，原发现策略误报双主窗。只对 Qt 外层候选要求 GWL_STYLE 至少包含 WS_MINIMIZEBOX 或 WS_MAXIMIZEBOX；两者均无的说明窗口不计候选，并单独记录有限计数。NativeWindowAccess.candidates 与 validate_candidate 一致执行。样式查询异常或类型不明仍失败关闭，不用当前前台、窗口面积、标题排序或期望目标任选；两个真实合格主窗仍拒绝。mmui::MainWindow 原规则不变。此角色过滤不替代账号进程代次、标题、草稿与提交前复核，不增加恢复窗口、关闭说明窗、切会话或键鼠操作。

## 1.3.28：草稿区展开控件的实测偏移

1118×809 沙盒窗口的展开控件为完整 25 像素中性灰形状，左上角位于已独立核验的 composer 框右边减 25、上边加 3；其 12 个下端像素进入正文区域。为此仅增加这一固定完整模板，旧两个模板保留。保护区内的全部标记点必须与模板逐点相等、颜色近中性灰，并且恰好一个模板命中；不得只按正文内的碎片识别、忽略矩形、缩放/平移猜测或改变文字阈值。只从正文标记中移除已核验的实际控件点，其他任意标点、图片或颜色标记仍拒绝。光标仍须取得真实闪灭后的空白帧，不因绿色/细竖线而直接认定空草稿。此修复不更改标题、会话、键鼠次数或超时，不代表真实发送验收通过。

## 1.3.27：标题识别的字号处理与失败来源

标题 OCR 不根据期望会话名选择倍率、阈值或结果。同一帧保留全部标题墨迹，按实测墨迹高度选择固定处理表：16～24px 使用 NEAREST 2.5/3/4 倍、150 阈值及输出白边 32/64/0px；其他高度保留原 4/6/8 倍表。每个处理都必须识别完整标题，三份结果逐字一致才可返回；不采用多数表决、模糊匹配或挑选命中目标的结果。实测原灰度标题与历史标题图验证固定方案，不将有限样本称为任意字体保证。比例换算使用实际取整后的宽高缩放，白边不计入原图坐标；逆变换外包取整，越界/无效词框拒绝，完整性仍在原帧标题区核对。

每次发送及标题读取先清空上次证据，仅导出有限状态（not_read/verified/ocr_error/ocr_empty/incomplete/disagreement/capture_or_layout_error/header_missing）、处理次数、行数、字符数、完整性布尔及异常类型，不导出原始标题、截图或异常正文。verified 且标题与目标不同才称 chat_not_open/target_not_open；空值、异常、缺字或三份不一致统一 title_unverified，提示标题未核验，不误导用户反复打开已打开的会话。该码不表示已核实其他会话，不能触发范围不符跳过、改目标或绕过发送前最终标题闸门；保留原等待/过期、不输入/不补发行为。旧未提供结构化证据的测试/兼容适配器保留原字符串接口。

## 1.3.26：最小化沙盒的签名请求镜像

客体在首个心跳前建立固定 `control/request.json` 签名 pending 槽，成功后才声明 `request_mirror_v1` 能力。宿主仍只创建一次不可变 UUID 请求，并将完全相同的签名载荷发布到固定槽一次；这两份是同一个 request_id、原创建时间与原截止时间的单次请求，不是重试。客体可从固定槽接收完整请求，不能要求因共享目录负缓存暂不可见的 UUID 文件先出现。旧能力缺失时保留原通道。仅此精确可变路径增加同次读取内最多 50ms 的 WinError 2/32 只读恢复；大小、路径、签名和配对核验不变。

请求摘要覆盖去签名后含 version/channel_id 的完整规范 JSON。消费记录与响应绑定该摘要，镜像宿主必须核对响应摘要。UUID 与镜像同 ID 不同摘要、已见身份内容改变或已消费摘要不符均拒绝，不能用冲突内容覆盖既有响应；清理 UUID 文件前重读并核对摘要，不能删除不同内容。所有动作在执行前登记有界的 ID/摘要防重表，发送消费标记持久保留；管理请求不能因为短期消费文件已清理就重复执行。防重表只在原 deadline 已过且至少 35 秒单调时间后移除，验证墙钟使用本 epoch 内不倒退的高水位，容量不足明确拒绝而不逐出仍有效身份。新 epoch 拒绝旧槽；停止、取消、二次截止时间核验和单次发送闸门仍生效。进程崩溃后以新 epoch 拒绝旧请求，不补发。

宿主响应等待使用 RPC 入口的原单调截止时间，锁等待、签名核验与文件发布均计入预算；不能在文件发布后重新起算。固定槽发布失败、响应丢失或超时不重新发布 UUID/镜像，不延长发送预算；已有投递可能性的发送仍按 uncertain 取消且不重发。修复通信延迟不等于真实微信发送验收通过。

## 1.3.25：隔离目录的实际用户访问权限

Windows 新建程序暂存目录与配对目录必须在创建时原子设置受保护 DACL，仅授予当前进程 TokenUser 的明确 SID、SYSTEM 和 Administrators 可继承完全控制；不得用 OWNER RIGHTS 代替实际用户 SID。管理员启动时目录所有者可能是 Administrators，沙盒共享进程仍须由同一实际用户读取。禁止先创建宽权限目录再收紧、修改旧目录 ACL、扩大到 Everyone/Users/Authenticated Users 或静默接管旧配对。非 Windows 维持私有 0700。已有目录或无法核验用户/安全描述符时明确失败，保留已有证据。

仅对首次 session 与首次 recovery 均明确停在 share_program、操作为 share、HRESULT 0x80070005 / 返回码 2147942405，且从未进入通道映射、代理启动或业务的配对，允许显式修复目录创建缺陷。修复必须核验同一沙盒交互用户、零代理/worker、旧及派生通道路径均不存在，并选用此前从未尝试映射的新构建路径；不得重复任何未确认的旧程序 share。先在通道根目录独占持久保存修复日志，原目录完整归档且 ACL 不变，再以原 channel_id 与 secret 在新私有目录复制经有界核验的原始元数据字节。路径交接期间及崩溃残留必须由外部日志阻断其他准备/恢复/启用，不能把目录缺失当作全新配对；未知阶段不自动重做。保留原始 session/recovery/映射证据，重新共享派生通道仅能是已证明从未执行的首次映射；共享后重新取得签名随机挑战零进程证明，单次启动并核验新 epoch/当前版本构建/新鲜心跳后最后登记。权限修复不修改旧目录 ACL，不停止微信或沙盒，不启用通道、启动机器人或发送消息。

## 1.3.24：失败配对的有证据续接与有限诊断

已有 recovery.json 不再一律代表结果未知。仅当通道/沙盒身份、原始 failed session、完整程序指纹及已成功的通道和程序映射均可核验时，允许用户再次显式恢复；不得重复 share 已确认的通道，不得删除原恢复证据或另造通道身份。任何未知映射、元数据冲突、业务/消费遗留或进程/停止证据不足仍拒绝。续接须保留独占操作日志，重新取得签名停止或无业务加新随机挑战零代理进程证明；只单次启动代理，核验新 epoch、当前版本/构建和新鲜心跳后最后提交 session。纯读取失败不得产生永久无差别锁；启动或映射结果未知不得以重试掩盖。

状态仅导出固定枚举字段：recovery_state（none/resumable/blocked/ready）、recovery_stage、recovery_failed_operation、recovery_hresult、recovery_returncode、recovery_reason，以及 initial_failed_operation、initial_hresult、initial_returncode。阶段和操作仅接受实现中的有限枚举，HRESULT 仅接受固定十六进制格式、返回码仅为整数；原因须为本地固定文案，不导出原始异常、原始 session、指纹清单、密钥、命令或路径。状态读取失败明确说明证据不足，不能猜为可恢复。API 生命周期锁、管理员修订核验及停止要求不变；诊断不启用通道、不启动机器人、不连接微信、不发送消息。

## 1.3.23：沙盒 OCR 读取恢复与像素点击边界

可变共享槽增加严格固定的 ocr/relay.json 与 ocr/<UUID32小写>.result.json；后者由 pending 替换为一次结果，不等同于不可变业务响应。Windows 错误 2/32 的恢复仍只在同次 read_document 内最多 50ms，每轮重新核验路径/大小/JSON/签名；可选 retry_deadline 必须为有限数字，结果轮询传原六秒单调截止时间。其他错误及 OCR request、普通 response/event/consumed 一律不恢复，不重新投递业务。

PrintWindow 成功后先成功选回原 GDI 对象，再 GetDIBits，任何 NULL/HGDI_ERROR 拒绝。清理失败互不阻断；选回失败也须先解除私有 DC 对位图的占用，再释放位图。

坐标通道输入聚焦和提交的鼠标点击均不改窗口样式。虚拟桌面范围须完整有效、包含落点且在按下前保持一致，SetCursorPos 失败不投递按下。SendInput 必须携带 MOVE|ABSOLUTE|VIRTUALDESK 并按虚拟桌面归一化。send_key 持久回调及悬停之后再核对账号窗口、前台根窗、几何、落点、精确标题、完整正文和截止时间；只点击一次发送键，提交证据保持保守，未知不重发。

refresh_composer 的新 draft_box 不能丢弃；发送后输入框可能收缩，必须重新取得经像素核验的正文区域再确认清空；空白检查前后继续核对精确目标、绑定窗口、前台与原截止时间，切到其他空白会话只能为 uncertain，不得冒充提交已确认。前台兼容成功不能证明严格后台可用；本版本沙盒后台实发仍待验收。

## 1.3.22：发送键的结构闸门与点击投递

`src/wechat_ocr_labels.py` 重写为纯像素结构闸门：`locate_send_button(frame, region, recognize=None)` 在上探 6 行的扫描窗（`SCAN_TOP_MARGIN=6`）内要求**恰好 1 个**绿色块（`r<100 and g>140 and b<180`，4-连通，30..120×15..55，填充率 ≥0.55，长宽比 1.2..4.5，整块内缩于窗口不被裁）＋块内缩区白像素（`min(pixel)>150`）**恰好 2 个**主簇并满足墨量/跨距/间隙/宽度/高度一致/垂直居中/密度一串判据；OCR 退为加分项，读不出或抛异常都不阻塞；任一判据不成立返回 `[]` 并给出 reason 码（`no_evidence`、`region_out_of_frame`、`no_green_block`、`multiple_green_blocks`、`green_block_clipped`、`glyph_inset_too_small`、`no_glyph_ink`、`glyphs_not_two`、`glyph_geometry_invalid`、`glyph_noise_dominated`、`glyph_centering_invalid`），调用方仍按 `len != 1` 报 `send_failed`。新增 `explain_send_button(frame, region, recognize=None)` 返回同一判据链路的 dict（ok/reason/button/glyphs/ink/ocr/green_blocks/scan_window/block/fill），供取证与单测。`PixelGUI.refresh_composer()` 清缓存后重做输入框/工具栏像素核验并返回新的 `draft_box`，`_attempt` 在回读正文之后调用它并重新取 `send_region`——粘贴后区域不得沿用。发送键由 `_send_input_click(user32, x, y, sleep)` 投递（`SetCursorPos` + `MOUSEEVENTF_ABSOLUTE` 左键按下/抬起各一次，归一化用 `GetSystemMetrics`），不再用 `gui.wx_click`（它会清渲染子窗 `WS_EX_TRANSPARENT`，真机点击不被微信处理）；`submitted`/`_submitted`/`_point`/`_require_foreground`/`_send_gate`/单次提交/未知不重发的语义全部不变。

## 1.3.21：坐标通道抓帧来源

坐标+OCR 通道的像素来源改为窗口自绘：`src/wechat_capture.py:window_bitmap()` 用 `PrintWindow(hwnd, hdc, PW_RENDERFULLCONTENT)` 取整窗位图并带回窗口矩形左上角，`render_crop()` 以 `(origin_x, origin_y)` 与渲染区尺寸把屏幕坐标 box 映射成位图坐标裁剪；`PixelGUI._grab_screen(box)` 先走 `grab_render()`，返回 None（自绘失败、尺寸异常、box 越出渲染区）才回退 `ImageGrab.grab(bbox=box, all_screens=True)`。`_pixel_frame()` 抓帧前后各取一次几何、比对帧尺寸并在变化时抛 `RuntimeError('截图期间窗口位置或尺寸改变')` 的约定不变；`_build` 不再覆盖 `_grab_screen`。只读检查失败的 detail 带异常类型与原文，`_inspect_composer` 的 RuntimeError 带 reason 码。

## 1.3.20：发送前窗口身份

主窗候选可排除有 owner 的附属窗与 WS_EX_TOOLWINDOW；不从多个真正主窗中按排序或标题猜账号。原生枚举与账号查询失败须保留计数及错误类型等有限诊断，不输出窗口标题、账号文件路径或消息正文。候选的 hwnd/pid 与可核验进程代次须在选定前复核，无法完整核验不得成为就绪。

严格后台任意成功体检返回前重核桌面、同账号同窗口和非最小化；phase(input)之后、SetValue之前重新读取该窗输入控件，检查精确会话、空草稿和可写模式，再核窗口。未输入时失败为 not_submitted，已有草稿与未知结果不重发规则不变。

坐标通道的前台与落点除 pid 一致，还须 GetAncestor(GA_ROOT) 等于已绑定主窗 hwnd；只允许该主窗或它的渲染子窗，不能用 GA_ROOTOWNER 沿 owner 关系把模态窗当聊天窗。查询为零、异常或另一个顶层根窗都拒绝。不改变发送提交次数、会话切换、剪贴板恢复或窗口激活策略。

## 1.3.19：只读重查和日志来源

共享目录协议的 Windows 读取句柄允许 FILE_SHARE_DELETE；非独占已有文档用 ReplaceFileW 发布，缺目标时沿用 os.replace 首次发布。当前沙盒共享目录不支持 FileRenameInfoEx 的 POSIX 语义，不能将仅本机通过的接口直接用于客体。只有固定可变槽 service.json、control/cancel.json 和 control/stop.json 在 WinError 2/32 暂态时，允许同一次读取内 50ms 总预算、5ms 间隔的只读重查；每轮仍检查路径、有界读取和完整签名。权限、解析、签名或其他错误不重查，一次响应/事件/消费及不可变配对缺失也不重查。该读取恢复不重新投递 RPC、不执行第二次消息、不延长上层截止时间；持续失败仍明确抛出。非 Windows 保留普通文件路径，exclusive 请求与消费文件不走替换。

`POST /api/sandbox/recover` 是与普通准备分离的显式修复入口，仅限唯一未完成配对，复用原 channel_id/secret。它沿用管理员修订核验、取消清理和全程生命周期锁，运行、连接或重连中的机器人必须先停止。客体通道路径由 channel_id 派生，session 仅允许 legacy/by_channel 映射枚举，不能接收任意路径。旧 service 存在时必须取得签名退出证据；不存在时要求无业务遗留并取得本次签名静止检查。同身份命名互斥锁阻止并行代理，新版本、新构建、新 epoch 和新鲜心跳全部核验后才原子登记；恢复不安装微信、不激活通道、不监听、不发送。映射结果不明保留阶段证据，不自动重做。

GUI 后台探测的 Exception/无效三元组必须显式失败，不得默认为成功；GET 超时和连接失败返回结构化错误，不将失败响应当作成功状态或已加载设置。后台服务重连与微信读取连接是两个状态，不能互相证明。

后台输入树的读取异常表示证据不足，不能使用浅树节点数将其判为客户端未开放。仅无错误、查询已穷尽的浅层外壳允许一次 150ms 纯读取刷新，初读与刷新共用两秒输入树查询预算；同账号 hwnd/pid、非最小化、交互桌面必须在等待前后核对。不得借重查调用唤醒、系统读屏标志、hot_activate 或进程写入。提交仍只有一次，已有草稿仍保留；G 的 Invoke 未提交事实不因重查而改变。

Sandbox CLI 输出采用有界临时文件，等待直接 CLI 进程，不因窗口后代持有 PIPE 而在 timeout 后无限 communicate。原 CLI 错误和 JSON 校验继续保留，不扩大进程终止范围。

GUI 摘要日期标签只解释文件日志首行有效日期：早于本地今日为历史日期，同日为今日，未来/无效/缺失不归历史。不推断后台运行代次，不改变读取顺序、ErrorBlock record_id、清空水位或原文。后台完整日志仍按原文展示。

## 1.3.18：后台调用证据与界面状态

`wechat_background` 的 Invoke 路径在 SetValue 后、唯一一次 Invoke 前重新选择同账号 hwnd/pid 的窗口，读取新输入控件、精确会话、完整正文和唯一按钮的新 InvokePattern。重新查询期间桌面失联或证据缺失返回 not_submitted 并保留草稿，不调用旧控件。posted_enter 与 Invoke 继续互斥，不作补发。

体检增加 `submit_verified=false`，控件模式存在仅表示可读取，不证明提交有效。诊断只记录 `invoke_return` 标量和 `invoke_exception_type` 类型，不写异常正文；调用后的清空检查、uncertain、不重发及 delivery_verified=false 不变。未清空只称已尝试调用，不能声称已发出或未发出。

控制台微信体检提示绑定来源，后续当前发送失败使旧成功提示失效，其他操作通知保持独立。日志首次隐藏渲染仍保留跟随意图，布局后跟尾，同时保持暂停、手动滚动与选区。模型主动连接测试状态与实际请求用量分别呈现。

## 1.3.17：收发代次与日志完整性

`WeChatTransport` 发送任务在进入公共发送方法时绑定代次；`halt_sends`、执行器重置和关闭使旧任务失效。间隔、目标解析、串行锁及线程队列等待之后都不能因重新启用而放行旧任务；旧代超时/取消不能取消新代发送。关闭不可由 `resume_sends` 重新打开。`IsolatedWeChatSender` 在准备任务前捕获代次、在启动子进程前复核，停止/恢复之间的慢准备不得启动旧任务。公开发送二元组和插件接口不变，超时和未知结果仍不重发。

桥层继续绑定发送代次：原生桥在等待串行锁、查询名称后复核，断开使旧代失效；沙盒在等待发送锁、RPC 锁及投递请求前复核。停止/恢复不能令上述等待中的旧任务继续投递，旧代 RPC 结束时不能取消新代。

原生、沙盒和 Hook 的运行时接收回调均绑定来源桥及启动代次，停止或替换后的旧回调不入库。原生监听在联系人查询前捕获回调与监听代次，停止立即失效；旧经典轮询保留自己的停止事件和客户端，不随新一轮启动恢复。停止后的沙盒事件不增加收件计数或确认水位，留待合法新监听重投。

监控热更新或连接恢复后，仅当同桥、同传输、同发送代次及当前接收回调仍有效，且运行原本允许发送、没有停止或安全暂停时，恢复底层发送；不由工作线程重新打开宿主发送闸门。迟到恢复落实当前停止意图，不能取消已允许的新运行。

沙盒准备期间启用/停用切换返回 409；当前运行或连接中的同一沙盒须先停止再准备，停止态同一沙盒准备期间占用现有生命周期锁，其他环境配对不因此停止本机机器人。GUI 与 API 一样仅允许 `ready` / `agent_ready`；配对元数据必须核验构建号与映射种类。

日志等长改写不能仅凭相同首尾判断无变化；读取保留改写代次。10MB 内以分块指纹建基线，追加只补新增字节，等长且文件时间变化时重新核验，内容相同不重复显示；更大文件保留前后锚点检测，正常 5MB 轮转日志在完整指纹范围内。诊断 ZIP/TXT 导出在打开输出文件前拒绝覆盖原日志、启动诊断及同一文件的别名，原文件不因导出而截断。离线回归不代替当前会话后台实发验收。

## 1.3.16：默认停用的受控协议候选

本轮像素定位补充：WinRT OCR 按 `OcrWord.text` 与真实词框重组单条行，不使用引擎自动插入空格的 `OcrLine.text`；词内空格保留，含糊词框拒绝，完整标题仍需两次识别相等。标题端点被左侧同高横线合并时，仅在测得窗口栏起点、同一面板竖线上下连续及唯一候选后解歧。草稿检查可排除实测位置、完整形状均精确匹配的展开控件实际点，不排除整个矩形，额外标点及未知装饰仍非空。每帧复核输入布局；仅正文外全部像素与几何完全一致时复用已核验区域，不缓存标题文本。`guia` 只读体检也检查草稿，占用时 `ready=false / draft_not_empty`，不会清理或输入。

WinRT `TextAngle` 不为零时，词框按其角度绕图像中心顺时针映回原图，越界或过大倾斜拒绝。首轮标题裁剪由已确认标题区域内全部真实字形确定，完整性仍在原区域检查；不按期望目标选择字符或修正 OCR 误字。双重识别使用不同缩放与预处理，任一次不一致仍拒绝。

沙盒只读 `inspect_environment` 宿主等待最多 35 秒，为客体 24 秒检查留回执余量；请求启动期限和发送总时限不随之延长。长只读在工作线程执行，停止意图先关闭发送闸门，迟到检查不能重启监听。

客体请求生命周期与 watchdog 超时判断/取消在同一控制锁内切换，完整请求处理结束时刷新宿主空闲租约；不能因冷连接开始时间过旧而误杀下一条检查，也不能让旧超时快照取消新请求。刷新空闲租约不延长原请求截止时间，不调用 `resume_sends`，显式停止/取消保持有效。

同一存活沙盒更新代理构建时，严格核验原配对及同一 epoch 的签名 `active=false` 清理记录，不把失联当停止；新构建添加独立 `C:\WhaleApp-<20位构建号>` 只读映射，旧缓存/映射不修改。冻结代理从自身只读运行清单派生构建号写入签名心跳，只有新 epoch、版本、构建号及新鲜心跳均一致后原子登记 session。失败保留旧配对，映射结果不明不重复映射，不启动微信或机器人。

日志记录身份包含文件截断内容代次，清空显示不能遮住截断后的新错误。`src/diagnostic_redaction.py` 仅净化导出副本中的常见凭据、认证头及随机回调路径，`ZIP/TXT` 共享规则；保留原日志与异常帧，不承诺通用聊天/路径匿名化。

`wechat.environment=hook` 使用 `HardenedHookBridge`，配置在 `wechat.hook`；默认 `enabled=false`、环境仍为 `local`。这是参考 `wxhook-ob11-bridge 1.2.0` 后独立编写的有限适配器，未包含、执行或加载参考包的 DLL、安装器或更新锁定脚本，尚未完成正式真机收发验收。原版模块监听所有网卡且无鉴权，不能通过本版启用核验；本版没有可直接交付使用的合格原生提供方，不能要求用户安装参考包来绕过。

适配器仅接收已绑定账号、人工指定单个稳定目标的文字事件，保留原始消息编号与发送者；不将 CRC32 映射或昵称当作账号/目标身份。模型、记忆、原有群触发规则、插件及日志继续沿用鲸鱼娘内核。仅允许精确账号和目标，不开启多会话自动路由，不发图片/文件，不主动外发，不回落本机或沙盒发送；账号安全暂停继续生效。

身份检查同时要求指定版本、模块指纹、唯一接口所属进程、创建时间、仅本机回环监听，以及该进程实际打开的唯一账号目录。配置或接口响应的账号名称本身不足以放行。回调使用独立随机路径凭据，只接受回环来源与有界 JSON，停止或切换后拒绝旧回调；凭据不写访问日志。入站原始编号按账号持久去重，冲突拒绝；入库成功后确认，未确认事件可再次投递但不重复生成回复。

原版模块指纹明确拒绝，生产允许集合当前为空，不能通过改配置自行登记原版。停止、重连和发送取消有独立代次，旧回调及旧发送线程不能随新一轮启动恢复。迟到的新增服务器记录可保留 `confirmed` 证据，但发送状态仍为 `ready=false` / `late_confirmation`，不解除取消或重投。

每次发送先在本地日志库独占登记尝试，再进行一次发送提交；接口 `ret=0` 仅表示接口接受。必须在同一进程与账号只读数据库中，排除基线旧记录，确认唯一新增、正文和目标一致的自身消息，并取得非零 `server_id`，才标记 `confirmed` / `delivery_verified=true`；不代表收件人已读。发送后的超时、断连、证据不足和重启遗留尝试均为 `uncertain`，绝不重发。保留串行闸门、间隔和总超时。事件日志库含当前账号必要入站数据；运行日志与诊断不写消息正文或凭据。

`GET /api/wechat/hook` 查看候选状态；已绑定本机管理员可调用 `/probe`、`/config`、`/activate`、`/deactivate`。保存仅登记停用配置，核验只读；启用再次核验身份，环境切换先停止旧队列，切换后机器人保持停止。跨等待复核管理员 revision。GUI 显示候选与未实发边界，离线通过不得冒充后台发送已解决。

插件接口仍是本项目 `PluginBase` 与 runtime/transport 契约；OneBot v11 是平台消息通信规范，既不是通用 Agent 插件接口，也不自动兼容 AstrBot 插件或共享其记忆。本候选未实现完整 OneBot 服务。token 节流、Fish Tank 学习与自主笔记的进一步改造，应在发送目标通过验收后继续，不能算作本版已完成。

## 1.3.15：沙盒当前会话文字通道

`wechat.environment=sandbox` 时使用 `SandboxBridge`，模型、记忆、队列、配置与控制台日志继续留在宿主原数据目录；沙盒只运行微信及受限代理。仅映射版本核验后的程序运行目录（只读）和独立配对目录，不映射宿主用户目录、模型密钥或聊天数据库。该桥 `backend=sandbox`、`text_only=true`、`current_chat_only=true`，不回落本机桥。发送保持串行、间隔、25 秒总超时；失败或结果未知不重投。图片/文件不经此通道外发。

通道只回复沙盒微信当前已打开的群或联系人。`resolve_chat_name` 在客体当前账号内解析真实会话名称并检查重名；宿主不得用本机联系人库替代。处理前检查目标标题，发送前继续精确核对标题、窗口归属、空草稿和完整正文；其他会话消息跳过，不自动切会话。沙盒内微信必须可见、可操作并保持客体前台，宿主 Windows Sandbox 窗口可以最小化；不能由此推断宿主微信自身最小化发送可用。

配对目录 RPC 使用 HMAC 签名、通道 ID、代理 epoch、随机 request_id 与截止时间；只接受允许动作。请求过期、代理版本/epoch 改变或签名不符即拒绝。发送先独占落盘消费标记，再操作微信；响应丢失不重做。`cancel`/`stop` 走独立签名控制文件，长请求不阻断心跳和取消；仅停止代理拥有的发送进程，不结束微信。入站事件按水位持久确认，落盘失败/队列满停止收发。共享路径拒绝链接及上级目录跳转。

宿主异步监听回调返回严格 `False` 表示待确认，不能仅因回调已返回就删除客体事件；runtime 完成持久入库后按 `_sandbox_event_id` 调用 `ack_inbound`。乱序确认只推进连续水位，水位先原子保存再发布给客体；失败或停止不确认，重新连接后由客体保留记录恢复。旧同步回调兼容原返回语义。

首次只读 `connect` 的主机响应等待可达 85 秒，以容纳客体数据库冷启动；它仍只投递一次，签名请求必须在原有最多 29 秒期限内开始，等待完成期间心跳及取消控制独立运行。此限时不用于发送，`send_text` 的 RPC 等待仍为 24 秒且外层发送总超时为 25 秒；迟到响应继续核验同一配对与 epoch。

`GET /api/sandbox/status` 查询状态；本机已绑定管理员可调用 `POST /api/sandbox/prepare`、`POST /api/sandbox/activate`、`POST /api/sandbox/deactivate`。准备只启动受限代理，不启动微信或机器人；切换先停止旧任务与发送队列，跨等待复核管理员权限，切换后机器人保持停止。关闭沙盒会丢失其安装和登录状态，需要重新登录并配对；安全暂停不会因此解除。

验收记录（2026-10-27）：一次性候选 D 在宿主沙盒窗口最小化期间，将已授权的现有验收草稿点击发送一次，未再次粘贴。相同账号新增匹配自身消息 1 条且 `server_id` 非零；17 次宿主采样均保持前台窗口和鼠标位置不变。该结果证明这次隔离桌面发送成功，不代表收件人已读，也不替代正式 `SandboxBridge` 全流程验收；正式通道实发验收仍待完成。该次授权已消费，不可复用。以下旧版验收描述为当时状态。

## 1.3.14：发送证据、账号窗口与日志浏览

发送接口继续支持 `(ok, detail)` 解包；原生桥与传输可返回 `SendOutcome` 二元组子类，附本次 `diagnostic`。`submission_state` 为 not_submitted / submitted / confirmed / uncertain；输入框清空不等于服务器送达，`delivery_verified` 仍为 false。明确 not_submitted 且本轮尚未发送任何气泡或附件时落为 failed；缺少证据、已经点击提交、部分气泡或文字已提交但附件失败均为 uncertain。任何一种失败都不自动重发。不能从上次 send_environment 推断本次提交结果。

OCR 当前会话名称必须精确相等，拒绝截断/子串/相似名称；只允许沿用同一账号核验通过的唯一窗口 hwnd/pid。账号或窗口不明确时不能借降级任取窗口。输入后以受限 Ctrl+A/Ctrl+C 回读完整正文，规范换行后必须与本次正文相同，读失败或不一致保留草稿且不点击发送；原剪贴板仍须恢复。UIA 路径已有草稿也不覆盖；开始输入后禁止转到另一通道。OCR 阶段调用同一 worker phase 回调，在点击前持久记录 send_key=true；异常/超时根据这份证据判断提交情况。只读 window-check 不记为消息发送失败，也不能触发发送重连。

日志浏览增加关键词、级别、暂停自动滚动及清空显示；清空仅影响视图，不删除文件。消息与报错按原文转义，刷新保留选区/滚动。当前待处理数使用 pending_current，其他账号积压单独标识。DSH 被独立选用时只校验 DSH 配置，不以未使用的原 API 密钥阻止启动；专用无工具人设的原核验仍保留。

验收边界：这些修复不代表个人微信已支持最小化原生后台发送。隔离桌面内运行当前会话、宿主窗口处于后台的方案仍需新的真实发送验收；旧的一次性授权已撤销。

## 1.3.13：发送前拦截遮挡和焦点丢失

「坐标+OCR」通道在点击输入框和发送按钮前，用 WindowFromPoint 核对落点属于微信进程；点击输入框后最多等 300ms，前台进程仍不属于微信就返回 foreground_lost，正文不写剪贴板、不投递 Ctrl+V。遮挡或未知落点归属返回 blocked_by_window。请手动打开目标会话、最小化遮挡窗口并让微信保持前台。

不再调用 guia.input_text / click_send 的内部重试链：正文只粘贴一次，发送按钮只点击一次；原剪贴板文本及原始字节先完整备份，在粘贴确认或失败后恢复；句柄/文件等无法无损备份的格式会在覆盖剪贴板前拒绝输入，并提示先复制纯文本。已有草稿、窗口移动、定位/OCR 超时都明确放弃；过期的只读定位即使晚返回也不能继续输入。粘贴后及发送前复核前台与标题，按过发送按钮的异常/超时一律结果不确定、不重发。截图使用 all_screens=True；失败日志增加 point_pid、foreground_pid，不写正文。

2026-10-26 真机验收：用户授权后仅尝试向文件传输助手发送一条约定短消息。标题校验通过后，输入框定位截图报 OSError: screen grab failed，流程在点击与粘贴之前停止，发送键未执行；数据库新增匹配消息为 0。没有重试，尚未确认真实送达。最终离线专项 86 项、完整回归 2092 项通过（skipped=3），后端及 HTTP 自检均 EXIT 0。禁止热激活、自动切会话及点击会话列表/表情面板的红线保持原文。

## 1.3.11：发送通道红线例外与发送阶段码契约

发送侧新增唯一红线例外：UIA 不可用时降级到 wechatauto/guia.py 的「坐标+OCR」通道（截图 → WinRT/guia OCR 定位输入框与发送按钮 → 真实鼠标点击 → 剪贴板粘贴）。缺一不可的限定：只发给当前已打开的会话，发送前必须用标题栏 OCR 校验会话名，不符即失败并提示用户手动点开会话；禁止 hot_activate、任何自动切会话（点会话行/搜索切换/open_chat）、点会话列表与表情面板；按下发送键后不再换通道，避免重复发送。失败原因码为 guia_unavailable / target_not_open / minimized / send_failed / invalid_target；1.3.13 新增 blocked_by_window（落点遮挡/归属未知）与 foreground_lost（前台进程不匹配）。

SendResult 既有约束不变：失败不得沿用 ready=true，成功仍以 watermark 确认，不能从窗口存在推断送达；单线程串行闸门、发送间隔、超时且不重发、连续失败重连必须保留。降级只在 UIA 定位失败时发生一次，已经按下发送键后不得再换通道。

日志契约：统一内核 src/logging_setup.py 写 logs/backend.log（5MB×5 轮转），提供 SEND_PHASE_CODES 十一个码（locate_window / check_chat / input_text / press_send / verify_watermark / guia_probe / guia_check_title / guia_input / guia_click_send / guia_verify / fallback_switch）与 send_phase_line()/send_failure_line()/mask_chat_name()。阶段行单行形如 `send phase=<码> who=<会话名前8字+…> len=<正文字符数> reason=<原因码> channel=uia|guia pid=<进程号>`，**正文不得落盘**，只留长度；会话名与窗口标题只留前 8 字符；失败用 logger.error 写一行带 hint 的摘要，异常走 record_exception() 留完整 traceback。驱动 import 时清 root handler 的行为由 restore_backend_handler() 兜住，写前必须自查。

诊断与导出：控制台提供「复制报错」「复制全部」「导出 TXT」「导出诊断包」「打开所在目录」。诊断包为 zip，必含 report.txt（报错原文 + 运行记录 + APP_VERSION + 系统信息）、logs/ 全部日志、send-worker 堆栈、启动诊断与 wechatauto_logs；缺失或超 32MB 的文件只跳过不报错；日志与导出不得回传消息正文。

## 1.3.7：实例隔离与失败状态一致性

参考用户提供的Cockpit Tools源码中的实例目录归属与进程匹配思路，原生适配DSH连接：新增会话的工作目录按现有scope哈希隔离在data/dsh_bridge/workspaces/<scope>，不搬动或删除旧会话。接入探测与保存跨await时必须复核管理员revision；换绑期间的旧请求不得保存到新主人配置。

SendResult失败时必须输出ready=false和真实失败原因，不能沿用“已确认账号窗口”的ready=true；保留已有waiting_code/submission_state，不从窗口存在推断送达。没有更具体原因码时使用send_failed。成功仍保留delivery_verified=false，输入框清空不能证明服务端送达。严格后台的写入/回读/会话变化失败同样失效当前检查，保持草稿、不重发、不回退前台。

后台参考项目WeChat-AI-AutoReply的公开分支包含OCR认行、坐标投递点击、SetForegroundWindow、keybd_event及失败重试；这些路径不符合本项目约束，不移植。只读控件检查、模型生成与真实发送验收分别报告。

## 1.3.6：DSH桌面发现与人设日记

POST /api/dsh/discover需本机绑定管理员，只读本机进程PID/父PID/程序名和已监听TCP端口，从DeepSeek Harness/dsh进程及子进程候选中核验DSH RPC信封，不扫描任意端口、不读取进程内存、不启动或重启软件。发现不会自动启用引擎、签发凭据或改变现有会话；多个可用端点需用户在界面选择，再保存。管理员绑定窗口提供真正DSH引擎入口，区分对话引擎和外部Agent凭据。

POST /api/dsh/connect接受已知DshSettings，本机绑定后先核验临时地址与专用无工具人设，再复用原保存逻辑启用；核验失败不改变现有配置，不回退默认DSH人设。GUI明确按钮“核验后保存并启用”，discovery本身仍只读。DSH成功不代表微信后台控件可用。

日记分离原统计摘要与模型日记：agent_diary_entries保存workspace/day/persona/status/content/evidence/updated/error，旧agent_diaries保留。GET工作区diary只读已写日记；POST生成当前人设第一人称日记，内容来自当日本地主人工作台与同账号当前人设已成功回复记录，最多30轮/8000字，不跨账号、不向群注入。每主人/日期/人设至多一次模型提交；失败/断电未知不自动重试。记录证据范围，模型失败不以统计冒充日记；已生成内容不被统计覆盖。自动日记默认开启，可在工作区关闭；应用运行且空闲时为最近一个有记录的已结束日期补写，过往最多7天、不积压补全，今日可手动提前写。生成走现有模型或DSH无工具人设，最多600输出token、无重试；不外发、不调用桌面工具。

自动执行先等待两分钟无新聊天；来源标记主人/群成员/联系人，密码/密钥等敏感轮跳过。模型提示禁止补造具体动作、对话等事实，稀少记录写短文；日记只作为私人人设表达，不作为事实自动学习，证据原文仍是事实依据。

再次发现的hot_activate旧配置/任务/调用入口一律失效；不得写入微信进程。兼容前台与严格后台互斥仅因焦点策略，前台开关不能物化控件。没有控件时明确不可用并等待，不重新引入进程激活。发布白名单排除*.bak、*.bak-*与备份源文件，避免打包历史危险实现。

图片仅文字响应的_picture_text_only标记须在视觉识别、OCR及自动收藏入口统一生效，不能有旁路下载。沿用已启用的图片文字响应策略时用本地中性确认，不为未获识图资格的图片调用文字/视觉模型；点名或明确识图触发的原流程不变。

## 1.3.5：有界旧记录检索与笔记编辑一致性

Agent上下文优先级为当前消息/当前人设、近期完整问答、与当前问题相关的同会话旧原文、少量相关长期记忆。只在同workspace/session内取最近1000行原文，旧问答引用限当前人设、排除近期已注入轮次、附时间和原request_id，作为非权威引用，不跨会话/群私聊检索、不额外调用模型。无关的最新记忆不再无条件注入；明确称呼/回答方式要求可作为稳定偏好最多2项。

agent.context包含history_turns（默认10，1～30）、max_chars（默认8000，1000～24000）和recall_limit（默认3，0～5）。max_chars统一约束历史与引用文本，不裁剪当前消息和系统人设；参考资料优先让出预算。GET /api/agent/workspace新增context设置，POST /api/agent/workspace/context需本机绑定主人，bridge能力agent.context.configure需manage_console。聊天返回context计数，无权限读记忆时不注入历史或引用；旧调用字段兼容。

记忆查询API新增total/has_more，保持items最多100条；界面可按关键词查询。笔记读取/保存回调按请求代次核对，失败或换文件时不得带上旧正文/版本，读取未确认时阻止保存，旧回调不得改写新选择的编辑状态。诊断不再引导开启已退役的自动打开会话开关。

DSH.generate新增可选context_references，只接收宿主已生成的引用消息；新会话仍只播种一次近期记录，复用会话时通过references传本轮新检索结果（最多8条/8000字），不能因records为空把更新的记忆丢掉。请求总长仍限18000字，引用优先裁掉，不裁当前问题、不重发。普通微信宿主仅挑选已知历史参考JSON，Agent工作台仅传本轮build产生的引用；不增加电脑工具权限。

运行时配置段白名单保留agent与dsh；此前只在内存设置后可用、重启丢弃是缺陷。仅保留本项目已定义段，不加载QQ等其他平台凭据。构造运行时不因此自动启动DSH或微信收发。

## 1.3.4补充：输入框辅助标题与上下文

输入框Name只剥除结尾的已知提示（Ctrl+Win与旧语音占位），真实名称精确相等优先，不按包含/前缀放行。前台与严格后台使用统一解析；worker生产路径禁用最小化恢复及自动切会话，最小化返回waiting_code=minimized，运行时等待而非继续生成/反复重连。旧inspect/prepare工具函数的allow_restore默认保留兼容，但生产worker显式禁止；严格后台在加载UIA驱动前分流，任何失败不回退前台。

Agent同一会话的人设切换保留原文问答意义，当前人设系统提示决定表达；其他会话/换绑工作区不可混入。每主人工作台串行，10轮/8000字预算，超长最近轮保留首尾；短确认承接最近提议。微信Token节流保护最近完整问答与当前消息，预算过小时报告受保护内容超限，不删除提议换取表面节省。人设基础温度±0.15按当前意图及近三轮用户文本平滑调整，范围0～2，浮动0～0.4可配置；DSH不支持逐轮temperature时沿用服务端参数。

参考本机fish-tank-setup/plugins/dsh-yunao-memory与bridge/src/social/memory-policy.js分层、去重、来源和最小共享设计，以原生SQLite适配；未执行安装包代码。Agent原文长期保留，检索最多1000行活动窗口；自动学习限主人直接明确的偏好/名字/表达要求，证据request_id入库，自动条目90天停止注入（不删原文），人工记忆不自动过期，忘记后自动学习不复活。同主人跨端凭据共用workspace，群聊不读取主人私有记忆。memory_meta包含category/importance/expires/evidence，原格式兼容。插件agent_tools与异步agent_invoke(name,args,WorkspaceAccess)受权限与10秒超时限制，故障逐项隔离；不是恶意Python代码沙箱。

白名单中的wxid/群ID只能匹配稳定标识，不能被同名备注命中；新增private_replies.stableIdsOnly，可禁止所有备注名匹配，默认false兼容旧名单。空允许名单拒绝全部普通私聊。主人通道使用self_id而非昵称核对账号，仍受私聊开关与白名单约束。

# 微信版内部契约（施工用）

## 1.3.4：Agent工作台与主人工作区

Agent主界面以人设对话为默认入口，移除并排方案栏；旧设置方案API及应用/撤销保留兼容，改设置作为显式模式，不把普通聊天自动解释成操作。POST /api/agent/chat需要本机管理员绑定、session_id、request_id、text、可选persona；同一request_id不重复调用模型/执行工具，结果未知不重试。人设只影响表达，不授予权限。聊天不连接微信、不解除安全暂停。

主人工作区位于可写根目录data/agent_workspaces/<绑定身份及revision摘要>/；换绑创建独立工作区，旧目录保留但不向新身份开放。支持notes内UTF-8 txt/md读写（单文件64KB、总量4MB、乐观版本冲突检查、备份），拒绝绝对路径、穿越、符号链接/reparse点；不暴露配置、密钥或微信进程。工作区读写是实际本地文件修改；任意桌面执行交给独立本地Agent，宿主不把聊天文本作为shell命令执行。

会话原文、人工记忆和日记保存在本机SQLite，同主人跨端凭据共享workspace，不猜测QQ与微信昵称；同一Agent会话切换人设仍保留最近对话语义，以当前人设提示为准，长期记忆按人设筛选。会话上下文默认10轮且有总字符预算，检索记忆最多8项。不向普通群聊注入Agent私有聊天。/记住、/忘记、/读笔记、/写笔记为显式命令；其他输入仅模型对话，不执行模型输出中的指令。日记只汇总工作台当天计数及已显式记忆，不自动联网或训练权重。

新增能力workspace.info/workspace.files/workspace.read/workspace.write/workspace.memories/workspace.remember/workspace.forget/workspace.diary、agent.chat及agent.history，统一经现有bridge/execute暴露，read_memory/write_memory/manage_console分别限制；本地路径不含凭据，不自动修改DSH默认人设或QQ会话。微信主人通道仅匹配绑定三元组中的wechat账号和稳定subject，且仅私聊的/agent命令；默认关闭，普通聊天、群聊、引用文本、昵称匹配不授权。开启后仍沿用所有微信安全暂停和发送闸门。

bridge操作在排队后、异步调用返回前重新核验凭据是否仍有效；Agent聊天等待锁、模型返回及插件工作区每次调用均重新核验。撤销或到期后不返回旧私有结果、不写入聊天/学习，不自动重做已经发生的操作。插件接口WorkspaceAccess可接收宿主check_access回调，权限撤销属于宿主拒绝，不作为插件故障隔离。主人/agent命令在白名单放行后进入专用队列，优先于普通本地回复插件。

## 1.3.3：账号安全事件后的硬暂停

用户反馈三次强制下线及微信安全提醒；旧兼容模式恢复微信进程写入、自动回复官方通知均已移除/拦截。enable_send_compat仅保留旧签名并恒返回false，restrict_driver不得保存、恢复或调用原_wake_accessibility/_hot_activate_accessibility；检查不得构造兼容唤醒驱动。旧auto_open_chat配置仍可读，但worker实际不执行自动搜索/切会话。此条优先于历史章节中允许这些行为的描述。

固定官方/系统username及gh_公众号不进入模型、归档学习或回复队列；最终bridge发送文字/文件也拒绝这些稳定标识，不按昵称猜测身份。微信团队weixin的安全提醒（包括卡片CDATA）触发data/wechat_safety_hold.json：先停外发、停止接收、取消在途任务和未发送队列，禁止后续连接/窗口检查/媒体读取/自动重连，重启后仍暂停。普通连接失败的原重连规则保留；安全暂停优先。

GET /api/status.safety显示安全暂停。POST /api/wechat/safety/reset仅本机、需完整确认文字“我已完成微信官方安全验证”，不进入Agent能力目录，bridge凭据不能调用。仅解除机器人暂停，不登录、不开始微信测试、不恢复队列，不能解除平台账号限制。不做实际账号重试验证，不能宣称已消除风控。

## 1.3.2：原生图片对话中心

2026-10-25增补DSH原生对话桥：dsh.enabled默认false，base_url仅允许本机http且必须显式端口，provider/model/reasoning_effort/timeout/profile独立保存。GET/POST /api/dsh查询/配置，POST /probe检测协议与专用人设，POST /install-preset先检测依赖，区分旧版目录与新版profile机制，先备份只安装自己的人设；修改需本机已绑定管理员，bridge凭据不能直接调用。不会自动重启用户DSH，不修改QQ会话和原模型配置。

DSH只使用whale-wechat-chat专用人设，内置guard同时restrict allow=[]和执行拒绝，不把普通群消息交给默认桌面编码人设。需服务端登记此人设、本地guard文件和对应目录/profile配置匹配，缺少任何一项都等待配置；不退回默认人设。鉴权只使用本机DSH浏览器会话签名协议，不保存/返回原签名密钥。

session/create/selectModel/prompt采用斜杠RPC与args.request；先在remote.mux订阅session/follow收到snapshot再提交一次requestId。快照/assistant/chunk/thinking均不作为回复；只在同一turn的完整assistant/message及completed的turn/end后交给原宿主后处理、@清理和transport串行闸门。异常、取消、超时不重复prompt或改用原API补问；先清本会话inbox再cancel，取消未确认的会话标uncertain且不复用。宿主对on_observed附临时_observation_only，媒体插件不得在这一步登记下载。

DSH会话按地址/模型、模式、账号、群或私聊目标、人设正文和管理员绑定版本隔离；每20轮换新的活动上下文，原DSH会话和本地原文保留。首次只带有界近期记录，后续不重复注入整段历史；本地检索记忆和当前问题继续传入。映射最多500项，清理映射不删除聊天。DSH用量在DSH统计，不混记为原API用量。保留本地学习1000条活动窗口和事实/表达审批；这属于提示增强，不是训练模型权重，也不开放群聊执行电脑工具或自动联网研究。

2026-10-24用户明确授权新增默认关闭的窗口消息投递试验，仅投递已核对窗口的回车消息，不使用真实键鼠、不抢前台、不点坐标、不切会话；先离线验证。新增wechat.background_submit=invoke|posted_enter，默认invoke。posted_enter只在严格后台中使用，要求正向账号/窗口匹配、原目标已打开、草稿为空、正文回读一致，再投递一次按下/抬起，不调用Invoke、不投递WM_ACTIVATE、不设置全局键态，不借此自动兼容唤醒。缺少控件或账号不能核实时等待；结果未知不重发。此授权不扩展为OCR、鼠标点击或自动打开会话。界面保持实验标识，未经真实发送验证不能宣称已可用。

明确选择前台发送时，子进程foreground_only=true，从头走前台路径，不再先尝试后台Invoke留下半成品草稿；旧任务协议仍保留后台尝试失败不前台补发的保护。前台兼容不承诺不抢焦点。

参考smart_imagechat_hub的入库→标注→检索分层，采用微信原生实现，不加载AstrBot插件源码或订阅源中的代码。图库继续以SHA256寻址；新增auto_tags、manual_tags、features、annotation_source/updated，旧索引缺字段仍可读，人工标签优先且不被自动任务覆盖。聊天已有识图结果直接回填，同内容并发入库/识图的先后顺序不能丢结果；只回填已收集条目，不绕过私聊/黑名单或触发规则。

自动额外识图默认关闭，由图库明确开关启用；上传/回复中收集新图后排入本地持久任务，旧图库补全由用户明确批量发起，每批最多20张、活动队列最多100张。任务单线程标注、独立识图客户端、共享计量，聊天有工作时暂缓开始，不占聊天模型锁。失败逐图跳过，无自动无限重试；关闭中断标为待人工重试，重启不重做结果未知的模型请求。取消/禁用/删除/改配置或人工编辑后不能回写旧结果。只处理本地已登记图片，不后台下载未触发群图，不接触微信发送接口。

GET/POST /api/stickers/annotation-jobs 查询/登记任务，POST同路径/cancel取消任务；GET /api/stickers返回自动标注开关与任务概览，现有逐图recognize继续兼容。人工识图、自动标注共用同一设置解析，尊重独立识图地址/密钥/模型/清晰度/抽帧和超时。多维标签仅为图片参考资料，不执行图中文字指令。

模型选图在本地按当前问题/画面说明与标签、含义做有界检索，最多12个候选、候选说明有长度上限，不将整库原图/全文塞入提示。保留现有手动图库和明确标签命令；只通过现有回复元数据附图，每轮最多一张，严格后台继续跳过图片，不新增主动斗图/连发或鼠标面板操作。

## 1.2.99：后台提交不回退、发送结果核实与点名开关

严格后台分支必须在创建桌面发送驱动前分流，任何失败都不得唤醒控件、恢复窗口、设焦点、切会话、操作剪贴板或模拟键鼠。兼容模式一旦开始尝试后台写入，也必须直接返回本次后台结果；异常、草稿变化、提交后未清空均不得转到前台重发。后台发送只调用一次Invoke，最多2秒只读确认同一聊天的输入框清空；无法确认记为结果待核实，不自动重试。诊断区分not_submitted/submitted/confirmed/uncertain，不将控件可读等同于消息送达。

最终发送前由宿主根据已启用的reply_preferences.hideNicknames或chat_controls.noAtInReply设置内部_suppress_mentions；传输层收到该标记时必须同时清除自动推断点名和mentions元数据。模型每段开头按当前开关清理，固定关键词正文保持用户原文。此标记不得从外部消息原样信任，宿主每轮重新计算。

## 下一阶段：绑定管理员的 Agent 控制面

Agent 是控制台能力的调用方，不另建微信收发链。新增本机管理员绑定、能力目录及结构化执行接口；管理员以 platform/account/subject 三元组标识，QQ号与微信wxid不互相猜测。未绑定时允许查看状态、生成预览，禁止Agent修改配置。解绑或换绑必须使旧凭据失效；绑定、凭据签发不进入模型可调用目录。

控制操作只映射到显式登记的现有控制台接口，沿用参数校验、锁、插件隔离与发送规则；不得接受任意URL、HTTP路径、Python代码或绕过宿主权限。每次写操作携带request_id，重复请求只返回已有结果，执行中或结果未知时不得重做。操作审计不保存密钥/完整请求正文。控制面没有直接发送接口，不开放主动群发、切会话、键鼠/OCR面板或管理员自授权。

跨端bridge使用绑定管理员签发的有范围、可撤销凭据，来源、请求、会话与平台账号分开记录。外部消息和记忆只是数据，不能自称管理员或修改权限。现有GUI基础页面继续保留；控制面故障不得使微信读消息与已授权被动回复失效。

## 1.2.97：插件故障隔离

隔离能力属于宿主加载器，不能依赖另一个插件才能生效。逐模块保护导入、元数据、构造、post_load、配置归并和列表序列化，重复ID不能覆盖已加载插件。普通异常及插件SystemExit只隔离故障项，保留原配置与文件；plugin_faults.json记录插件ID、阶段、异常类别、时间及版本/文件指纹，不记录异常正文或凭据。相同故障版本启动时跳过，更新文件/程序版本或停止机器人后人工重试可重新加载。

运行钩子逐项保护，失败不提交该插件对消息/对话/回复元数据的局部修改；后续健康插件继续运行。异步准备75秒、后处理5秒、tick60秒、on_ready10秒上限，取消机器人任务必须向上传播且不记插件故障；保持原识图独立预算与发送25秒不重发。网络/识图正常失败返回状态不视为插件崩溃。权限、点名、群名单等保护组件故障时暂停外发，不能因跳过插件而放宽发送权限。隔离不是恶意代码沙箱，不承诺拦截原生进程崩溃或不让出执行权的死循环。

插件列表新增fault/faulted字段，故障插件显示为隔离且禁用开关。GET /api/plugins/health返回故障清单；POST /api/plugins/retry-failed仅在机器人停止后重试，不清配置、记忆或插件文件。

generate_reply必须区分插件自身异常与其转发的engine.chat异常；后者原样上抛，不隔离chat_controls，不破坏去重重生成的原回复兜底。上下文事务提交必须保留仍被运行时持有的当前消息对象，识图完成后的注释不能写入已脱离对话列表的副本。

## 1.2.96：可信分气泡与识图准备

仅 chat_controls 的本轮后处理可设置 `_reply_pacing_generated`；模型原文中的 PACE 标记先清除，不作为发送命令。后处理结果转为 `_reply_chunks`（最多4段）及 `_reply_delay_ms`，inbox.reply 和对话归档只保存纯正文。极速模式取消额外模拟打字等待，但保留分段与 transport 的串行闸门、发送间隔、25秒超时不重发。每段前重新校验运行、策略和人设；部分发送失败记 uncertain 与 `_reply_chunks_sent`，不重发整轮；媒体只附首段且每轮最多一张。

独立识图接口根据自身地址选择 OpenAI/Anthropic 格式并补全路径，响应按对应协议解析、计入同一用量统计，不改变文字接口配置。OCR 与视觉并行；视觉成功后本地 OCR 最多再等1秒，未完成则取消其等待、不丢弃其他准备插件。底层 OCR 子进程仍有原超时。下载任务只在所属数据库/账号仍有效时缓存；关闭后旧任务结果不得回填缓存。

## 1.2.56：本地学习库、有界上下文与 WeFlow 历史导入

按用户最新要求，每群学习上下文最多1000条，超出删除最早上下文；私聊也独立使用1000条窗口。只裁剪新的 learning_messages 活动学习表，不自动删除原始 inbox/conversation_log 归档，覆盖升级不裁历史。账号/会话/发送者严格隔离，学习结果单独持久化。只从用户自述提取事实、重复短句提取表达候选；已确认事实需要同一发送者两条独立消息，表达候选须人工批准才引用，不自动改人设/系统提示，不伪称训练了模型参数。学习内容作为 user 历史数据进入有界提示，当前问题与人设优先。支持查看证据、批准/拒绝/删除；不把失败助手回复用于学习，不反复从自己的回复强化自己。

autoRemember=false 同时禁止本地学习库自动提取事实；插件禁用时不提取学习结果。learning_items 保存来源persona，显示、引用、操作遵守 personaIsolation；重读原文保留原人设，未知旧版来源归默认人设。已确认长期记忆与学习事实同文去重，不重复消耗提示预算。

新增本机 learning 查询/操作接口，界面展示窗口数量、候选和已确认结果，空库提供来源说明和读取入口。历史导入不进入 inbox、不触发外发。WeFlow 通过本机已鉴权只读 API 读取 ChatLab 会话，校验 ownerId 与当前稳定账号绑定，再事务写入本地归档/学习窗口；每次最多1000条，重复导入去重。无 ownerId/账号不一致/格式失败不得写入。DLL不是HTTP服务，缺少WCDB.dll或API未启动须明确说明，不将数据库可读假报成微信已登录或发送就绪。

Token 节流新增可选估算token预算，标明估算而非账单值；保护当前问题、人设、工具与多模态，超预算先裁历史。学习不额外调用模型；必要记忆按相关性有界引用，不把1000条整库发给模型。无法满足预算（受保护内容本身过长）须记录原因，不伪称已全部节省。

## 1.2.55：先保存原图再识别，50 张容量清理

用户选择仅处理需要机器人回复的图片。访问/触发规则不变，未点名群聊不因本功能下载或调用视觉模型。image_understanding 在取得本地原图、CDN 原字节或已登记同一原文件后，先保存到可写数据根目录 data/received_images，再读取该副本送视觉模型；GIF/WebP 原字节先保存，之后才抽帧。保存失败不得假报已识别。

ReceivedImageStore 仅管理专属目录中 sha256 命名的合法图片（单张 6MB、16MP，按内容去重）。有效图片数超过 50 时，删除最旧 floor(N/2) 张；51 张删除 25，保留 26，保护本次刚保存的图片。只清理该储藏区，不递归删除、不碰表情库/聊天数据库/微信缓存/其他目录；跳过链接及未知文件，校验目录边界。原始会话/记忆不随图片清理删除。无法删满时明确记录清理未完成，不伪报成功。

本机运行记录记录下载落地、储藏区数量及清理数量，消息 _image_storage 仅作本机诊断，不进入模型正文。插件配置提供打开图片储藏区入口。没有合法原图或可下载地址的加密表情仍明确失败，不用截图或键鼠降级。

## 1.2.54：后台能力诊断、长状态布局与识图追问

严格后台 window-check 保留只读，不置前、不切聊天、不唤醒/修改微信进程。返回 input_search 的节点数/搜索状态及 waiting_code，区分窗口最小化、控件未开放、目标聊天待打开、草稿占用和控件能力不支持；ready=false 不能显示为发送就绪。没有排队消息时只读检查也由健康周期限频刷新，避免旧的“最小化”结果永久停留。

历史识图失败不作为已看到画面的证据，旧助手的责怪/编造回复不得再次纳入模型上下文；归档原文保留。仅针对同一账号、同一会话、同一发送者五分钟内最近一次媒体失败后的明确短追问提供简短本地说明；不覆盖新图片、正常识图、OCR 文字问题或其他技术问题。不以取图失败推断用户发送失败。

控制台长诊断移到按钮行下方自动换行，状态卡片等宽且可收缩；完整消息保留文字与提示，不因长路径/单词撑宽窗口。

## 1.2.52 人设导入兼容与明确启用

导入支持原生 WhaleGirlPersona/v1、QQ/微信普通 YAML/JSON、人设 prompt 别名和 TXT 原文；本地文件按 UTF-8 BOM、UTF-16 BOM、UTF-8、GB18030 顺序识别，大小 5MB，正文 256KB。严格校验人设对象、正文非空、数字有限值、示例和关键词；无正文、未知版本、重复 YAML 键或结构过深时不写入。文件内容仅作人设数据，不能读取文件内的路径或执行指令。

persona-files/import 新增 filename/auto_name，旧参数不变。auto_name=true 时在后台生成合法内部标识并按 _2 等分配副本，不覆盖内置或已有文件，显示名称保留原文字；默认旧接口仍对非法标识/同名报错。返回 display_name/source_format/prompt_chars/warnings/activated=false。导入只保存并选中，不切换当前人设；界面持续显示下一步「设为当前」。导入期间防重复点击，过期的详情回调不能覆盖新选中的人设。重启应能重载同样正文、关键词、示例与可选内嵌图片。

## 1.2.51 账号目录绑定与 WeFlow 只读诊断

显式账号目录、db_storage、其下有限层级和数据库文件统一解析为数据根目录，但连接仍保留路径指定的账号。配置账号与目录账号冲突、配置账号不存在或底层数据库静默换号时拒绝连接，不写入账号记忆、不初始化发送器。自动发现和旧数据根目录的选择顺序不变。账号预览返回 normalized_dir/path_account，不保存草稿。

WeFlow 是独立可选的只读诊断数据源，不替换现有运行时监听，不自动启动、不接入外发或模型。新增 /api/weflow/config、probe、sessions、messages；配置 Token 复用现有加密存储，响应仅返回 token_set。仅允许回环 HTTP 地址、固定查询端点，拒绝 URL 用户信息、重定向和超大响应；错误不透出 Token 或响应正文。草稿检查不保存配置；空 Token 沿用已保存密钥。消息预览和 JSON 导出不进入 inbox，不触发回复。反向 WebSocket / AstrBot 适配器仍属于后续集成，不能把本诊断入口标为完整机器人通道。

状态新增 read_state/send_state/client_login_state；数据库可读与客户端登录分开，client_login_state 为 unknown（模拟除外），不能由旧库文件推断客户端在线。未连接时不能沿用旧的发送 ready 状态。

## 1.2.50 本地群聊记录、被动总结与人设切换

新增本机 conversation_log：监控范围内的入站记录按 mode/account/kind/target 隔离，只有发送成功才写回复。后台不自动删除旧记录；历史回填不重新发送。模型只读当前会话最近 N 条记录（chat_controls.historyTurns，默认 10），将其他成员发言和同人设的已发送回复作为带发送者标识的参考数据；当前问题优先。跨群原文不混入模型。管理命令不归档。

本机 HTTP 增加 dialogue/archive 查询、export、import、delete；导入校验完整后事务合并/覆盖，只作用于当前账号选定会话，不能进入 inbox 或外发队列。覆盖不接受截断备份。清空设置历史引用起点，不删除 inbox 原始运行记录。导出包含时间、会话、用户、角色、内容、人设及已发送回复，可人工审查后组织训练样本，不自动训练模型。

group_summary 为微信原生适配（仅参考上游功能，不加载 AstrBot/OneBot 或复制上游 AGPL 源码）。/总结群聊 [1–168小时] 是明确的被动命令，只总结当前群本机已记录范围，统计活跃成员与时段，按有界样本调用已配置模型生成主题。必须经过监控名单、暂停、过滤、发送队列和严格后台等待；无定时推送、私聊推送和跨群发送。消息内 _group_summary 为内部已验证命令标记，不从原始外部字段透传。

人设切换必须实际成功才确认与持久化；本轮生成固定人设快照，提交发送前再次核对人设指纹。切换后的旧生成回复取消，不补发。界面明确区分当前生效人设与正在编辑的人设。

本地即时回复也在入队时绑定人设指纹。quick_reply 的鲸鱼娘内置模板仅适用于默认鲸鱼娘，其他人设交给当前角色模型；不得通过替换一个称呼继续复用整段旧人设台词。单条对话记忆删除写入隔离的 tombstone，常规 inbox 历史查询必须排除它；清空以引用时间屏障保留原始运行日志，显式重新导入对应记录时可恢复该记忆。

## 1.2.49 识图超时与严格后台选择回归

设置接口新增可选 `send_mode=background|foreground|legacy`，缺省 legacy 保留旧客户端布尔组合的兼容口径。新 GUI 显式提交用户选择：background 强制 background_send=true、send_compat=false；foreground 强制 background_send=false。加载旧配置同时勾选两项时，GUI 保留严格后台、取消兼容选项。不得静默取消严格后台以换取发送成功。worker 的后台提前分流、等待、过期取消及不回退约束继续执行。

识图遵守 image_understanding.visionTimeout，不再被文字极速档的 6 秒隐式截短。真实 TimeoutError 优先于引擎上一轮 last_error；诊断记录实际模型、画面数和耗时，不记录图片 base64 或密钥。图片落盘延迟只在首次读完所有图片之后有限重查两轮（0.3/0.6 秒），保留原消息图片编号，共用现有下载缓存，不操作微信窗口。API 发送前校正 EXIF 方向、透明区域铺白、按比例限制长边 1536 后转 JPEG；长图分段与动画抽帧仍由原流程完成。权限/模型不存在错误不能自动更换接口、密钥或随意猜测模型。

识图失败区分取图失败、视觉超时、接口权限和不支持图片；不能把失败说成图片全黑或用户没发图。纯图片且完全识别失败时，发送前以实际状态生成本机说明兜底；含文字提问、OCR 或部分成功结果时保留原有模型回复。真实服务测试使用程序生成色块；模拟 UIA 不等同真实微信发送验证。

## 1.2.41 托盘恢复控制台

托盘菜单增加“显示控制台”，与单击/双击图标共用恢复路径。恢复只显示本窗口并解除最小化，保留原最大化状态；不启动、停止或重连机器人。后台暂时断开、开始/停止请求执行中仍可恢复控制台。沿用 1.2.40 的目录草稿预览、重连保留发送开关及退出等待约束。

## 1.2.40 目录预览、连接配置与托盘

账号列表 `GET /api/wechat/accounts?db_dir=...` 使用界面草稿目录，不保存配置、不重连；省略参数使用已保存目录，不能优先用上一次连接的目录缓存。空值与真实连接共用有限范围自动发现；显式无效目录返回具体原因，不能悄悄换库。账号目录和 db_storage 均规范化为数据根目录。

send_compat/auto_open_chat 仅在 bridge 初始化时给默认值，连接和重连不得覆盖 runtime 已设置的值。保存发送设置后清除旧发送体检结果，下一轮重新检查。

系统托盘沿用本机 HTTP 的 start/stop，不另起监听器。关闭窗口只隐藏，后台和请求继续；双击托盘恢复窗口，右键提供开启机器人、停止机器人、退出程序。显式退出先停止机器人，等待 GUI 请求收尾后退出；系统托盘不可用时保持正常关闭，不留下无法操作的隐藏窗口。

用户 2026-10-18 选择严格后台模式：新增 `wechat.background_send`（默认 false，保留已有发送方式），设置接口同名字段。开启后只调用已绑定账号、已打开目标会话的 ValuePattern.SetValue 和发送按钮 InvokePattern，必须回读正文和发送后清空；缺少能力、草稿、锁屏、最小化或目标不符时等待原队列期限，不自动切会话、不激活/恢复窗口、不碰剪贴板、键鼠或 OCR。只支持文字；附加表情本轮跳过并记录，不能把文字成功改成整轮失败。此模式不调用兼容唤醒路径；如控件尚未暴露明确提示先手动准备。发送键阶段之前/之后的失败仍按既有分类处理，不重发。体检可附 who 检查目标；运行时生成前及提交前都检查，等待停止可取消、过期不补发。

严格后台开关不允许回退到 wxauto/wxauto4 的前台驱动；连接前拒绝不支持的后端，已连接后切换开关也必须等待并阻止发送。没有文字、只有被跳过的图片时返回未发送，不能记录为成功。关闭程序期间延迟启动/配置回调不得重新启动后台。

## 1.2.31 动画表情的可读来源（CDN 直链）

微信 4.x 的 `emotion` 本地文件是加密的，`MediaDownloader` 也没有表情下载接口。但消息正文是
XML，含 `cdnurl` / `md5` / `len`：实测 4.1.15.13 下 `cdnurl` 返回 **200 + JPEG**，
字节数与 `len` 一致。因此约定：

1. `message_from_raw`：`emotion` 与 `image` 的 XML 段必须解析出 `url`
   （`cdnurl` 优先、`thumburl` 兜底，**必须 `html.unescape`**，否则 `&amp;filekey` 会请求失败）、
   `md5`、`size`；拿不到 url 时不填该键（调用方据此判断"读不到"）。
2. 识图插件的取图顺序：**直链优先** → `image` 才回落到 `download_media` →
   `emotion` **不要**调 `download_media`（加密文件，只会白等 60 秒）。
3. 直链必须走 `sticker_library.fetch_image`（公网校验 + 6MB 上限），
   并用 `frames_for_vision` 归一成 PNG data URL（顺带处理动图首帧）。
4. 状态文案：只有"确实有读不到的表情/图片"才写"部分缺失"，不得因为存在表情就一律报缺失。

## 1.2.30 自动打开目标会话（有意放宽旧红线）

原约定"发送前不切会话"来自 `AGENTS.md` 第二节的防误发考虑。用户 2026-10-17 明确要求
恢复"最初的程序"行为（搜索框搜索并打开会话后再发），故放宽为：

| 配置 | 行为 |
| --- | --- |
| `wechat.auto_open_chat` = `true`（默认） | 当前会话不是目标时，调用 `WeChatUIA.open_chat(who)`（搜索框输入 → 收结果 → 优先精确匹配 → 点击 → 校验标题）；**打开后再校验一次**，名字对不上仍不输入正文 |
| `false` | 回到旧行为：只发给当前已打开的会话，窗口对不上就放弃本次发送 |

实现约束：`send_text()` / `send_picture()` 都要走同一个 `open_target_chat()` 助手；
打开失败或校验不符时必须返回**可操作**的原因（含目标名），并且**绝不在校验失败后输入任何字符**。
同时修正检查口径：`inspect_window()` 不再把"最小化"判成未就绪（真实发送的
`prepare_window()` 会先恢复窗口），改为 ready + 提示"发送时会自动恢复窗口"。

## 1.2.29 发送兼容模式（微信 4.x 无障碍门）

微信 4.x 主界面自绘，**默认不物化 UIA 控件树**：`input_lookup.visited` 只有 3 个节点、
类名仅 `Qt51514QWindowIcon` / `MMUIRenderSubWindowHW`，`mmui::ChatInputField` 不存在。
物化的唯一开关是 `Weixin.dll` 里的一个"Qt 无障碍门"字节（实测 4.1.15.13：`RVA 0xb135c38`，
默认 0）。只打开 Windows 读屏标志（`SPI_SETSCREENREADER`）**不足以**物化。

因此新增配置 `wechat.send_compat`（布尔，**默认 false**）：

| 取值 | 行为 |
| --- | --- |
| `false` | 不唤醒。能收消息、能生成回复，**不向微信发送任何内容**；检查原因里直说"去设置 → 发送兼容模式" |
| `true` | 发送子进程在发送前调用 `_wake_accessibility()`（写 1 字节 + 校验 + 失败回滚），`window-check` 也会先唤醒，避免"检查说不就绪、发送又在等检查"的死锁 |

实现约束（后续改动必须保持）：

1. `restrict_driver(uia)` 返回被禁用的两个入口 `{'wake','hot'}`；其余禁用项
   （`ensure_materialized`、`_auto_login`、`ensure_window(wake=True)`）**永远不恢复**；
2. `enable_send_compat(uia, entrance)` 只在 `wake()` **这一次调用**期间放行
   `_hot_activate_accessibility`，`finally` 里必须恢复禁用（成功与异常都一样）——
   注意 `_wake_accessibility()` 内部会调 `_hot_activate_accessibility()`，
   只留前者不禁后者会互相打架（本轮踩过）；
3. `IsolatedWeChatSender` 通过函数取开关值（改配置下一次发送即生效，不必重连微信），
   取不到值或抛异常一律按 `false`（fail-closed）；
4. `send_text()` 的"当前会话必须等于目标会话"这条**不得放宽**：不自动搜索、不切换会话。

## 1.2.28 关键词设置填写引导

只调整 `keyword_trigger` 的显示文案和专用表单，配置字段和含义不变：enabled/keywordsEnabled/mode/includePersona/keywords，mode 仍为 append/replace。说明与示例直接展示，示例只追加到当前草稿，不请求接口；取消不写配置。草稿摘要明确关闭本页恢复原规则、关闭关键词为仅 @、旧词表与人设称呼的组合，以及人设专属词表优先。单字词在本机提示，并保留后台原校验。不得因为界面引导放宽原有群监控、匹配规则或发送限制。

最常填写的关键词放在滚动表单首项；页首展示一次完整填写和触发示例。策略或关键词关闭时，仅禁用不相关控件与示例按钮，不能清空草稿，重新开启后保留原值。保存按钮固定于滚动区域之外，小屏幕仍可保存/取消。

## 1.2.27 监听 @ 元数据与控件查找回归

微信 4.x `source` 可能是以 TEXT 储存的 Zstd 二进制。当前依赖的文字解码会不可逆替换字节，因此监听适配只在带 @ 的群消息上，以同一读库查询器按原 shard/local_id/sort_seq 取 `CAST(source AS BLOB)`，不增加解密副本或更改 text_factory。查询器能力缺失时兼容公共 `get_message_row` 的未损坏 source；未知/损坏编码不猜测 @。压缩输入、解压输出与窗口均有限制，不接受 DTD/实体，不保存原始 source。读取失败不丢入站消息，限频记录异常类型。

控件查找保留单窗口与节点/时间上限，优先广度遍历浅层输入框，深度上限 40 与依赖兼容；诊断区分只剩外壳、未打开聊天和查找达到上限，不能一律归因于微信未提供无障碍控件。真实无输入控件仍拒绝发送，不启用内存改写、读屏标记或会话切换。

## 1.2.26 核心回归修复验收

消息归一化保留非 XML 图片说明；@ 判定在媒体占位替换之前完成，文本昵称必须完整匹配，明确的 `at_ids`/`atuserlist` 仅在命中本账号稳定 ID 时作为 @ 信号。不把其他成员的 @ 当成机器人点名。OpenAI 兼容响应中的文字内容块合并为正文，思考块不进入回复。

识图成功不再受可选缓存/图库描述写入失败影响；这些失败单独记录。插件开关读取异常须隔离到该插件，不能打断后续回复变换或周期任务。发送环境等待期间仍须检查队列消息时效，过期后由既有领取流程标记 expired，不能让第一条旧消息永久挡住队列。沿用全部发送红线，不用放宽闸门掩盖真实微信控件阻塞。

图片准备阶段在同一模型锁内并行执行本地 OCR/附加准备与视觉请求，共享原下载缓存；全部完成后构造对话，识图结果只注入一次，失败仍保留 OCR 的可用文字。

快速回复只优化程序内部重复工作，四秒作为实际端到端测量目标，不截断模型回答、不伪造固定确认。发布验收分离离线协议/界面、真实模型和真实微信；真实微信仍未跑通时不得声称全链路通过，按用户本轮要求暂停正式打包交付。

## 1.2.25 多账号窗口绑定与发送环境体检

`IsolatedWeChatSender(data_dir, account_path='')` 将已连接账号的实际账号目录传入所有任务。发送端通过候选 PID 打开的 `db_storage` 文件只读确认账号归属；必须唯一匹配，不能按前台、大小或候选顺序挑窗口。目录名称相似、无权限读取和同账号多个窗口均拒绝，不关闭其他微信。

`window-check` 工作任务只读检查已绑定窗口与聊天输入锚点，不恢复、不激活、不聚焦、不读取聊天正文。`POST /api/test-wechat` 增加 `connection_ok`、`send_ready`、`send_environment`，真实后端读库成功但无输入控件时返回 success=false 并解释“读消息在线、发送未就绪”，模拟后端不检查真实窗口。bridge.status 同步提供最近一次发送环境检查结果。

启动连接后执行只读发送体检；已知缺少输入控件时，模型准备和发送前进入 `waiting_wechat_input`，每五秒只读复查，不生成无处发送的回复。仅最小化时允许进入既有发送恢复路径，恢复后更新诊断状态；锁屏仍进入 `waiting_desktop`。重新连接清空旧账号诊断；等待结束复用已生成正文且复查时效，不补发失败记录。

子进程结果增加 `diagnostic` 和 `requires_reconnect`。未执行发送键的本地窗口/控件阻塞不累计为数据库连接连续失败，不因同一窗口问题反复重连或重发。实际原生退出、超时与已开始发送后的不确定结果仍保留原闸门和重连约束。

## 1.2.24 最小化窗口与锁屏等待

数据库在线与桌面可发送是两项独立状态。真实 wechatauto 后端在领取队列及发送前检查当前 Windows 会话/输入桌面；锁屏、断开远程会话或安全桌面时显示 `waiting_desktop`，不调用模型、不创建发送进程、不按键。解锁后仅处理未过期消息，已生成正文只复用一次；停止会取消等待，不补发已经失败/待核实的记录。

发送进程通过窗口句柄、实际进程名和主窗口结构发现微信，不以能否枚举 Weixin.dll 模块作为 UIA 定位的前提。只允许恢复唯一、已验证的最小化微信主窗口（Win32 显示状态），不打开托盘菜单、不切会话、不自动登录；多窗口或无控件时明确拒绝。原生窗口类与 UIA 主控件类可以不同，在该句柄子树内有限查找，禁止全桌面乱找输入框、OCR、内存/读屏标记修改。

工作进程在写入与发送键前再次检查桌面，避免模型运行期间锁屏后继续输入；外层 25 秒超时不重发、串行闸门与发送间隔不变。控制台及未回复诊断显示等待解锁，不误报为模型/数据库离线。

## 1.2.23 连接依赖、目录与会话定位诊断

`wechat_only.db_dir` 保留原配置内容，连接时将数据根目录、账号目录或 `db_storage` 规范化为账号父目录；支持环境变量、中文空格及其他盘符。显式无效路径明确失败，不偷偷改连其他账号。自动发现只读取微信配置、注册表和有限的已知目录，不递归扫描全盘；手选目录在账号列表及实际连接中共用规范化规则。账号列表沿用库的自动发现，未发现时提示先选目录，不主动扩大账号读取范围。

会话名称优先当前账号的只读联系人映射（备注优先），发送时刷新；群名不得借用最后发言者名称。稳定 ID 与显示名分开，不能将未解析的 `wxid_`、`@chatroom` 或内部 scope 交给界面搜索。同名候选不能唯一确定时拒绝发送，保持精确校验，不新增面板、OCR 或会话切换操作。

冻结包显式携带 `win32ui`、pywin32 DLL 和同架构 MFC 运行库；源码及冻结自检实际导入原生依赖，冻结发送子进程校验应用本地 MFC。依赖异常提供安装版/源码版各自的排错提示，不建议在用户系统 Python 中修补冻结程序。

发送子进程以不含正文/密码的状态文件记录阶段、耗时和是否执行过发送键；失败与 24 秒子进程超时显示最后阶段。超过 10 秒保留该子进程线程堆栈，任务收尾删除请求、结果和阶段临时文件。外层 25 秒超时不重发、停止只回收本程序子进程及既有发送闸门不变。

## 1.2.22 单次回复、停止收尾与功能精简

启停使用同一生命周期锁；同时启动只创建一个监听器和回复任务。worker 先以数据库条件更新 `pending -> processing` 领取消息，只有领取成功的任务可以生成和发送。停止时先关闭发送入口并终止本程序的发送子进程，再取消任务和停止监听；已进入发送的记录变为 uncertain、不重发，尚未发送的 processing/pending 取消且重启不补发。启动恢复遗留 processing 为 cancelled，不删除历史。

停止后不再接收旧监听回调；取消或超时也必须清理隔离发送进程，停止入口不可触发第三方动态属性查找或定位微信。保留单线程闸门、发送间隔、25 秒超时不重发与连续失败重连。

保留文字私聊/群聊回复、人设与关键词、记忆、图片识别、管理员、日志及自动表情包；按用户最后确认每轮最多一张图片。移除分段打字、随机错字等附加互动，旧参数保留但不再执行；后台自动收图关闭，不为无触发消息下载媒体。正常回复正文合成一条，不把段落拆成数条发送。

同一入站事件优先按原始 `server_id/sort_seq` 与稳定账号去重（`source_event`），兼容旧版 local_id 键，昵称变化及分片变化不得重复回答。现有重复语句整理仍保留。

移除九个插件：`send_pacing/typo_sim/emotion_state/mood_guidance/computer_helper/turn_complete/quote_reply/mention_nudge_guard/chatter_guard`，产品共 25 个组件。旧源码仅用于历史离线回归；产品加载、自定义安装和冻结包均排除退役标识。

发送子进程禁用驱动的 `_auto_login/_wake_accessibility/_hot_activate_accessibility/ensure_materialized`，`ensure_window` 强制 `wake=False`。不得通过改写微信进程内存或设置全局读屏标记获取控件；不可用时明确失败，不能降级 OCR。发图不再调用 `WeChat()/gui.send_image`：父进程临时保管剪贴板文件格式并在收尾时恢复（包括子进程被停止），不保存剪贴板正文到磁盘；用户中途复制了新内容则保留用户内容并拒绝粘贴。UIA 确认空输入框、精确会话与焦点后，只粘贴一次；必须读到图片草稿标记才按一次发送键，不截图、不重试、不点表情面板。

## 1.2.21 图片与表情识别修复

基于 1.2.20 保留本地图库、自动收集和等比例缩放。图片下载只读本机：优先 `download_image(tier='best')`，不调用驱动界面下载原图的方法。导出路径按会话与分片隔离；下载器在同一数据库连接内复用且串行执行，避免重复找密钥和同号文件覆盖。传输层按连接、账号、会话、类型、分片、消息号合并并发下载并短时缓存成功文件；失败不缓存，允许微信落盘后再次读取。

聊天识图与图库管理采用一致的 GIF/WebP 抽帧（总输入最多三张），长截图仍按原分段规则，不把局部识别缓存为全图。成功描述同步回填已收集条目；不因识别而绕过私聊收集开关。仅有 `emotion` 段且取不到字节时写明不支持，不下载为文件、不假装看过；混合消息必须标明缺失。模型失败保留人工说明并按配置、权限、模型或图片输入不支持等给出不含凭据的诊断。

`PluginBase.on_observed()` 提供默认空实现；未实现观察钩子的普通插件不得每条消息报错。媒体校验在解码像素前检查像素上限，避免超大图先分配内存再拒绝。

## 1.2.18 桌面启动与窗口恢复

安装版默认入口由纯标准库启动监护进程拉起 `--gui-worker`，图形进程异常退出时保存退出码和不含凭据的系统诊断，并使用 Windows 原生错误提示显示日志位置。`--backend-worker --root <数据目录>` 在独立进程内运行本机 HTTP，微信库/数据库的原生异常不得带走图形进程；OCR/发送工作模式及发送超时约束不变。后台退出必须在控制台显示失败并可手动重连，不自动循环重启。

启动恢复窗口须根据当前显示器可用逻辑区域限制大小与位置，边缘/角落支持缩放，窄窗口有滚动条保证操作可达。外观配置的版本号、有限数值、坐标和编码异常不可使启动崩溃，也不重置模型配置或对话。控制台操作明确显示忙碌，配置首次读取成功后才允许保存，读取失败可重试。

## 1.2.17 插件取舍、移除与恢复

删除 `context_chime`、`repeat_echo`、`question_relay`、`at_mention` 的产品加载与打包入口，阻止旧自定义清单及重新安装恢复这些标识；旧设置保留但不执行。群聊控制不再提供随机插话，无论旧设置概率多大，未被 @ 或命中触发词均不生成模型回复。普通文字中用户明确填写的 @昵称不变。

`DELETE /api/plugins/{id}` 对可移除内置插件执行逻辑移除（原子保存 `removed_plugins.json`，保留配置），立即停止其钩子；`POST /api/plugins/{id}/restore` 恢复原启用状态。插件目录不可写时不删除 exe 内资源；清单提供 `removable`、`removed`、`source`，已移除项在“已移除”筛选中恢复。会话策略、记忆、管理员等核心组件保留，避免破坏依赖。自定义插件仍删除已确认的独立文件。读取和写入失败不得静默恢复被移除的功能。

## 1.2.15 安装版贯通验收

冻结自检必须额外运行真实本机 HTTP 与 Qt 后台请求，逐项保存四组规则、手动记忆、个人档案、管理员设置并重建后台核对持久化；验证关键词实际进入模拟发送记录。独立临时数据目录、随机本机端口和 mock 微信，禁止接入用户微信或真实模型。

## 1.2.14 用户规则、记忆管理与持久诊断

- `GET/POST /api/dialogue/rules` 提供触发与名单、连续跟聊、人设隔离、跨会话偏好四组规则。仅接受白名单内的插件字段，批量先全部校验、一次原子保存；非法字段/值不部分生效。不得新增主动插话或绕过群触发/发送闸门。跟聊只作用于已通过正常触发的消息，按账号、人设、会话和发送者隔离。
- `POST /api/dialogue/notes` 支持添加、编辑、启停和删除；客户端携带所编辑的人设，后台拒绝过期人设或不存在的记录，失败不清空编辑内容。
- `GET /api/dialogue` 返回规则生效状态、可选择的已观察联系人、可搜索的结构化个人记忆和当前账号持久诊断。`POST /api/dialogue/facts` 支持本机用户明确添加/修改/确认/删除个人记忆，限制为当前账号和当前人设可见范围。自动记忆仅按用户配置的明确自述前缀识别，仍需至少两条独立消息；人工确认可直接生效。
- 关闭人设隔离后可引用同一人的已有各人设记忆，开启跨会话后可引用同账号同发送者已有会话的已确认事实；关闭时恢复隔离，不迁移或删除原数据，不共享群聊原文。
- 与 QQ 版一致，`privateToGroup` 默认 false，私聊偏好或旧版来源不明的全局偏好不注入群聊；用户可单独允许。新记忆保留原会话来源，跨会话开关仅控制读取范围。
- 发送者标识不得回退为显示昵称：私聊缺少 sender 字段时使用实际会话 username，群聊身份不明则 sender_id 留空，不写个人记忆、不建立个人跟聊窗口。
- 跟聊窗口归属回复生成时的 `_persona_key`，发送期间切换人设不能把旧回复记到新的人设；固定回复不建立模型跟聊窗口。
- `dialogue_decisions` 持久保存观察结果；密码验证先脱敏再记录。诊断合并 inbox 的实际状态，重启仍可查询，pending 必须解释未启动/微信离线/前序处理/当前阶段，不能假称已发送。旧版仅在 inbox 的记录仍可显示。点名闸门中的用户规则维持既有黑名单语义，界面准确命名；限制同样适用于固定回复。
- 本机记录最后确认的账号身份用于离线查询，不连接微信也不外发；显式改为其他账号时不得借用此身份。新账号待处理配额不计入其他账号积压，历史记录保持原样。

## 1.2.13 覆盖升级与用户数据保留

正式安装 AppId 永久保持 `{CC9C9945-D704-468B-94E5-30AB08B37A21}`；覆盖安装沿用上次安装路径，不调用旧卸载器、不删除或替换用户数据目录。安装文件仅允许 exe、`_internal` 只读资源、启动工具及明确列出的文档，禁止把构建目录里偶然存在的配置或 data 一起发布。卸载记录追加，卸载仍保留用户数据。

冻结版始终使用当前 Windows 用户的 `%LOCALAPPDATA%\WhaleGirlWeChat` 保存配置、人设、插件、管理员摘要、记忆、SQLite 对话和媒体。新默认人设仅补缺失文件，绝不覆盖已有文件；不因升级或重启自动裁剪 inbox 历史和已存 outbox 记录。发送中断改为 uncertain 的恢复约束不变，上下文读取仍有窗口限制。

升级验证必须隔离正式安装、快捷方式和数据：旧版安装 → 种入自定义配置/人设/插件/密码摘要及超过 20000 条历史 → 新版不指定目录覆盖安装 → 文件哈希与记录逐项核对 → 冻结程序离线重载核对 → 卸载后再次核对数据。只验证“目录存在”不足以证明配置和记录保留。

## 1.2.12 插件列表响应与快捷入口

`GET /api/plugins` 按既有后台接口返回 JSON 插件数组。GUI HTTP 层仅对该端点将数组规范化为 `{plugins: [...]}`，兼容已包装的字典响应；其余端点仍拒绝意外数组、非 JSON 与错误 HTTP 状态。插件页先显示已读取的内置清单，自定义来源信息随后补充，未确认来源不得显示删除按钮。刷新合并重复请求，旧请求结果不得覆盖新列表；未加载时点击快捷配置，应在加载成功后自动打开对应配置，失败则明确提示并可重试，不静默丢弃点击。

## 1.2.11 管理员密码格式

新设或更改的管理员密码为 1–128 个字符，仅允许中文汉字、ASCII 英文字母和数字；空格、标点、下划线、表情等特殊字符均拒绝，校验时不裁剪或替换输入。GUI 与 `AdminSessions.set_password()` 共用校验，HTTP 入口同样执行；拒绝时不写盘、不修改凭据或登录状态。已保存密码的摘要及旧版验证规则不改写，避免升级导致原有密码失效；再次设置时执行新格式。

## 1.2.9 配置持久化、人设文件与管理员会话

- 人设切换、当前人设改名/删除的最终选择必须原子写入 `style.active`，磁盘失败不得返回成功。配置、人设与插件设置保存采用临时文件替换并保留上一份有效备份；启动优先有效主文件，损坏时恢复备份，不以打包默认资源覆盖用户文件。
- 人设新增 `keywords`（独立唤醒词，非空时替换通用词表）、`image`（本机管理目录中的可选图片标识）、`image_note`（模型使用的外观描述）。图片只在用户明确点击“识别形象”时发往已配置视觉接口；仅上传图片不会假称已理解。人设导入导出支持 YAML/JSON，带图通过受限嵌入数据携带，不允许导入文件指定任意本机读取路径。
- 本机 `POST /api/persona-files/import` / `GET /api/persona-files/{name}/export` 交换人设文件内容；图片接口存储在可写用户目录。聊天切换与本机切换共用 `runtime.activate_persona(name)`。
- 管理员按微信稳定标识绑定，可本机手填 wxid。密码仅存随机盐 PBKDF2 摘要；管理员私聊“进入管理员模式”后下一条作为密码验证，不进入模型、上下文、入站正文或日志；验证态按账号与私聊隔离，30 分钟后过期，5 次失败锁定 5 分钟，重启/改密/解绑后失效。
- 密码配置后所有管理员指令均需登录；未配置密码时兼容已有绑定管理员的旧查询/暂停指令，扩展的人设/插件/聊天记录操作要求先设置密码并登录。群聊与未绑定账号永远不能验证或执行管理操作。
- 管理员允许查询本账号状态、队列、日志、最近回复记录，切换已有的人设，启停普通插件，暂停/恢复回复，退出管理员模式。禁止电脑控制、任意文件/命令操作、主动群发和变更发送安全闸门。
- 新增对话与记忆页：手动记忆按人设保存并注入下一轮；群友档案与未回复诊断只展示当前账号，诊断依据队列状态与实际观察，不能把没有回复的消息标成已发送。管理页/弹窗使用主窗口背景和面板透明度。
- 长期记忆插件增加 `personaIsolation`（默认开启，旧无标记记忆留给默认鲸鱼娘）与 `crossChatPreferences`（默认关闭；开启后同一发送者可跨会话召回个人偏好）。不合并群聊原文，手动人设备忘和模型历史仍分别隔离；切换策略保留原数据。

## 1.2.8 回复称呼与速度

- 新增 `reply_preferences` 插件：默认不自动用微信昵称/@昵称起句；默认快速模式。称呼修正仅针对模型回复和相应历史，不改用户固定回复规则，不删除正文正常提到的人名。
- `runtime.fast_replies()` 表示快速模式；快速模式默认只等 100 毫秒合并同人消息，跳过大于 100 毫秒的模拟打字标记并合成正文，显式微小间隔保留。原插件设置保留，关闭快速模式恢复原节奏。串行闸门、发送间隔、25 秒超时不重发均不变。
- 模型输入只移除开头指向本机器人的 @ 称呼，原消息、目标标识、发送者身份与观察记录保留。`build_history(..., renderer=None, reply_renderer=None)` 可用宿主 renderer 渲染模型历史；按人设指纹隔离不变。
- `AIEngine.chat(thinking='auto_fast')` 是内部选择策略：仅对可切换思考的 Kimi K2.5/K2.6、DeepSeek 聊天系列 OpenAI 兼容请求尝试 `thinking.type=disabled`。未知模型及强制思考模型沿用服务商默认；自动模式遇到明确不支持的参数错误时学习并去掉参数重试一次，不切换服务商或模型，不保存思维链正文。
- 三秒是端到端优化目标，不是强制截断或假回复时限。日志区分微信消息时间、回调到发送完成、模型/发送耗时；不以固定“收到”替代真实回答。当前已入传输层的发送不改变。

## 1.2.7 管理插件补充契约

- 新增 `robot_admin`（绑定管理员的私聊查询、暂停/恢复自动回复）、`private_replies`（私聊允许/排除名单）、`keyword_trigger`（AI 唤醒词）、`group_monitor`（监控群与只监控模式）。均由插件中心配置，不访问微信库。
- `runtime.reply_allowed(message)` 在本地秒回之前以及实际发送之前执行私聊、群聊和暂停策略；`_robot_admin` 只由绑定身份验证后的宿主写入，不从入站数据继承。管理员通道不受自动回复暂停和私聊名单影响，但仍要求开启私聊监听。
- 管理员只能从本账号实际收到过的私聊会话中绑定，鉴权使用私聊会话 username 和 sender_id 一致的标识，不用显示昵称。管理指令只识别明确的“机器人 …”；日志查询脱敏，不支持电脑控制或执行任意命令。
- 宿主写入 `_management_account`（实际机器人 wxid，缺失时回退 account），管理统计及发送前重验按此隔离；不作为模型提示内容。
- 暂停持久保存；尚未发送的普通回复取消，恢复不补发暂停期间消息。进入传输层的发送仍遵守 25 秒超时不重发，不能撤回已开始的发送。
- `group_monitor.mode`：`settings` 跟随原设置，`selected` 只监控名单（空名单不监控任何群），`off` 停止监控群；`replyEnabled=false` 时只记录群消息，不生成/发送回复。禁用此插件回到原设置。
- `keyword_trigger.mode`：`append` 保留旧触发词，`replace` 只用插件关键词；`includePersona` 控制人设称呼；`keywordsEnabled=false` 只由真正的 @ 信号唤醒。固定回复仍由 `keyword_reply` 管理，支持私聊/群聊范围和包含/整句匹配。
- `GET /api/management/sessions` 返回当前账号本地实际观察到的最近 200 个会话，累计计数，元数据最多保留 1000 个会话，回复状态仅统计仍在 inbox 内的记录。管理员与会话元数据按本机微信 wxid 隔离（拿不到时回退现有 account 标识）；不扫描或发送其他账号消息。GUI 用其展示群监控及选择管理员。`POST /api/management/owner` 仅本机 HTTP，参数 `{target, bind}`。
- 插件监控设置保存后，主后端重建只读监听器（保留水位），不重新创建微信 GUI。非主后端沿用现有连接重建流程。

本文件是「鲸鱼娘 微信控制台」各模块之间的接口约定。任何插件、运行时、传输层的实现都必须与
本文件一致；改契约必须先改本文件。

版本：1.1.0 ｜ 平台标识：`WhaleGirlWeChat` ｜ 后端端口：`8091` ｜ 配置：`config.wechat.yaml`
数据目录：`data/wechat-only/`（sqlite、插件设置、自定义插件）｜ 微信接入：`wechatauto`（微信 4.x，首选）

---

## 1. 消息字典（入站）

运行时把微信消息统一成下面的字典，插件只看这个字典，不接触微信库对象。

| 键 | 类型 | 说明 |
| --- | --- | --- |
| `id` | str | 稳定去重键，形如 `"{shard}:{local_id}"` |
| `local_id` | int | 微信本地消息号（下载媒体要用） |
| `shard` | str | 消息所在分片库名（下载媒体要用） |
| `kind` | `'group'` / `'private'` | 群聊 / 单聊（由会话 username 是否 `@chatroom` 判定） |
| `target` | str | 会话 username（群 `xxx@chatroom`，单聊 `wxid_xxx`）。**稳定标识，用于隔离上下文与记忆** |
| `target_name` | str | 会话显示名（群名 / 好友备注），用于白名单与展示 |
| `text` | str | 正文；媒体以占位符出现：`[图片]`、`[语音]`、`[视频]`、`[文件:名字]`、`[表情]`、`[链接]` |
| `media` | list[dict] | 媒体段，见下表 |
| `sender` | str | 发送者显示名（昵称/群昵称） |
| `sender_id` | str | 发送者稳定标识；私聊缺失时使用会话 username，群聊缺失时为空，不使用显示昵称代替 |
| `sender_name` | str | 发送者群昵称/备注（拿不到时同 `sender`） |
| `at` | bool | 本条是否 @ 了机器人 |
| `at_ids` | list[str] | 被 @ 的 wxid（拿不到时为空列表） |
| `raw_type` | str | 微信原始类型名：`文本`/`图片`/`语音`/`视频`/`文件/链接/卡片`/`动画表情`/`系统消息` |
| `notice` | None | 微信侧不产生通知事件（入群/撤回等一律忽略），恒为 `None` |

媒体段（`media` 元素）：

| 键 | 说明 |
| --- | --- |
| `type` | `image` / `voice` / `video` / `file` / `emotion`（动画表情）/ `link` / `other` |
| `local_id` | int，下载要用 |
| `shard` | str，下载要用 |
| `name` | str，文件名（有则填） |
| `text` | str，链接标题等（有则填） |

## 2. 消息字典（宿主内控键）

这些键由宿主写、插件读，不要往上传给模型：

| 键 | 写入者 | 含义 |
| --- | --- | --- |
| `_reply_meta` | 插件 `on_ingest` | 立即回复时附带的段（`{'media': [...]}`），宿主发送时使用 |
| `_burst_handled` / `_merged_ids` | 宿主合并逻辑 | 连发合并标记与已并入的消息 id |
| `_persona_key` | 宿主 | 人设指纹，历史轮次按它隔离 |
| `vision_description` / `vision_status` / `vision_cache_hit` | 识图插件 | 图片识别结果与状态 |
| `voice_text` | 语音转写插件 | 语音转文字结果（同时会追加进 `text`） |
| `forward_status` / `forward_description` | 转发类插件 | 微信版暂未接入，保留键名 |

## 3. 回复元数据（出站）

插件在 `on_post_reply` 里可以返回 `(文本, meta)`；`meta` 支持的键：

| 键 | 说明 |
| --- | --- |
| `mentions` | 要 **@** 的群成员 **名称** 列表（微信按昵称 @）。元素可为 `"小明"` 或 `{"name": "小明"}` |
| `media` | 附加媒体段，元素形如 `{'type': 'image', 'data': {'file': '<本机绝对路径>'}}`；也接受 `{'type': 'file', 'data': {'file': ...}}` |
| `text` | 覆盖正文 |
| `append_mentions` | True 时 @ 放在正文之后（默认之前） |

正文里写成 `@某人` 只是普通字符，对方收不到提醒；要真 @ 必须走 `mentions`。
`src/plugin_segments.py` 的 `ALLOWED_TYPES = {text, at, image, file}`。

## 4. 运行时（`runtime`，插件能用的全部 API）

| 成员 | 说明 |
| --- | --- |
| `runtime.mode` | 恒为 `'wechat'` |
| `runtime.running` | bool |
| `runtime.ai` | `AIEngine`：`chat(messages, **kw)`、`describe_images(sources, model=)`、`configured`、`model`、`api_url`、`last_error` |
| `runtime.ai_lock` | `asyncio.Lock`，只有运行时内部用 |
| `runtime.db` | sqlite3 连接；表 `inbox`、视图 `messages` / `bot_messages`、表 `chat_outbox`、`chat_ambient`、`chat_memories` |
| `runtime.config` | `ConfigManager`：`get('wechat.target_groups')` 等 |
| `runtime.style` | `StyleManager`：`get()` → 当前人设（`.name`/`.display_name`/`.prompt_text()`/`.temperature`/`.max_tokens`） |
| `runtime.chat_state` | `ChatState`：`observe/ambient/remember/recall/forget/people` |
| `runtime.transport` | 见第 5 节 |
| `runtime.self_id` | 机器人 wxid（可能为空字符串，插件必须容忍） |
| `runtime.account()` | 机器人显示名（群白名单/自身消息过滤用） |
| `runtime.note(text)` | 写一条运行记录（会显示在控制台） |
| `runtime.recent_messages(limit)` | 最近入站消息（供需要语境判断的插件使用） |
| `runtime.aliases()` | 机器人"被叫到"的称呼集合 = 人设显示名与标识 + 配置 `wechat.trigger_keywords` + 插件 `chat_controls.aliases`。**群聊触发判定就是"被 @ 或正文含其中任一词"**，插件判断"是否在跟我说话"也用它 |
| `runtime.plugins` | `PluginManager`（`_plugins` 字典是私有约定，既有插件在用） |
| `runtime.can_send_proactive(kind, target)` | 只有 `wechat.proactive.enabled: true` 且目标在白名单内才为 `True`（默认关闭） |
| `runtime.queue_outgoing(key, kind, target, text)` | 走上面那道闸门排入主动发送队列；关闸时返回 `False` |

异步钩子可以 `await`；同步钩子（`on_ingest`/`on_block`/`on_pre_ai`/`on_sent`）不能 `await`。

## 5. 传输层（`runtime.transport`）

```python
online: bool                 # 微信是否可用（数据库可读 + 客户端在线）
self_id: str                 # 机器人 wxid
account_name: str            # 机器人昵称
backend: str                 # 'wechatauto' / 'wxauto4' / 'mock'

async send(message, text, meta=None)                       # 回复某条入站消息
async send_to(target, text, kind='group', target_name='')    # 主动发给某个会话（按 username 或显示名）
async send_files(target, paths, kind='group', target_name='')# 主动发图片/文件
async download_media(chat, local_id, msg_type, shard='') -> str | None
video_frames(chat, local_id, shard='', want=3, max_side=768) -> dict  # 经 call('video_frames', {...}) 调用，同步跑在线程里
async call(action, params) -> dict                          # 见下
async close()
```

`call` 支持的动作（插件只能用这几个）：

| action | params | 返回 |
| --- | --- | --- |
| `send_to` | `{target, text, kind, target_name}` | `{ok, message}` |
| `download_media` | `{chat, local_id, msg_type, shard}` | `{ok, path}` |
| `video_frames` | `{chat, local_id, shard, want, max_side}` | `{ok, frames: [...], message}` |
| `get_member_names` | `{chat}` | `{ok, names: [...]}` |
| `get_self_info` | `{}` | `{ok, wxid, name}` |

`video_frames` 的降级语义：通道（后端）没有抽帧能力时返回 `{"ok": false, "message": "当前通道不支持视频抽帧", "frames": []}`，即 `ok=false` 且 `frames=[]`；抽帧抛异常时同样 `ok=false`，`message` 为异常名与描述（`src/wechat_transports.py:597-604`）。

**不允许**插件直接 import 微信库，也不允许调用 `Poke`、`SendToLabel`、`ForwardToLabel`、
`AddLabelMembers`、`SwitchToMoments`、`VoiceCall`、`ForwardMessage` 等能力（见风险清单文档）。

## 8. 连接、账号与保存配置的约定（1.2.0 起）

这几条是"界面卡死 / 连不上微信 / 不回消息"修完之后必须守住的约定，改动前先读：

**连接是慢操作（5~20 秒），任何地方都不许在 Qt 主线程里等它。**

- `runtime.start()` 期间 `runtime.connecting` 为 `True`，`status()` 会暴露它和 `last_connect_ms`；
  界面据此显示「正在连接微信…」而不是看起来卡死。
- 界面侧一律用 `wechat_gui.call_api()` / `_run_task()` 发请求：请求在后台线程跑，结果通过
  `ApiJob.deliver`（对象属于主线程，Qt 自动排队）回到主线程再碰控件。**不要**把结果信号连到
  普通 lambda —— 那会在工作线程里执行回调。
- 没有顶层窗口的场合（单测、脚本、自检）`call_api()` 自动退化成同步调用。

**账号选择是三级优先，不许退回"按 .db 修改时间挑"。**

1. 用户在设置里指定的账号（`wechat_only.account`，可写账号目录名或 wxid）；
2. `active_database_account()`：正在运行的 `weixin.exe` 打开的库属于哪个账号目录；
3. `account_hint.json` 里上次判定的账号（带 `source`，弱来源不参与这一档）；
4. `newest_message_account()`：哪个账号还有会话在动（会话时间不会被我们读库污染）；
5. 库自己的启发式（`WeChatDB._pick_account`）。

`bridge.status()['account_dir']` 给出**库最终选中**的账号目录（可能被库内部"账号自愈"改过），
`status()['account']` 是它的显示名。多账号目录时必须把"依据"写进运行记录，并在依据不可靠时
提示用户改用管理员启动。

**`disconnect()` 绝不许结束用户的微信进程（1.2.2 起，红线）。**
`wechatauto` 的 `WeChat.ShutDown()` 实现是 `taskkill /f /pid <微信主进程>`，名字却像收尾清理。
`disconnect()` 只停监听、放引用；任何"断开/关闭"路径都不许调用名字带
`ShutDown` / `Shutdown` / `Kill` / `Exit` / `Quit` 的客户端方法。`tests/test_active_wechat_account.py`
的 `DisconnectSafetyTests` 用假客户端钉住这条。

**连接守护只在两种情况下重连**：`bridge.online` 为假，或 `consecutive_fail >= FAIL_LIMIT`。
"换了账号"要 `_note_account_switch()` **连续命中两次**才算（单次 psutil 读取会抖动）；
`connecting` / `reconnecting` 期间不再发起第二次连接（两个微信自动化对象会互抢窗口焦点）。

**保存配置只在"微信接入"那几项真的变了才重连微信**（`WECHAT_SETTING_KEYS`：
backend / db_dir / groups / listen_private）。比较用 `_same_setting()`，要容忍配置缺键读到
`None` 而界面提交 `""`/`[]` 的差异。只改模型/API 时只热换 `AIEngine`，机器人保持在线。

**模型名只校验"非空 + 不超过 120 字符"，不许再按形状猜**（1.2.19 起）。
`AIEngine.model_id_problem()` 曾经拦"带空格的名字"（当成网页显示名），实测有中转服务的
model id 自带空格，那条判据会把合法配置挡在保存与「测试连接」之外。名字对不对由服务商判断：
`GET /api/models` 给出可用清单，`chat()` 的 404 报错必须带上是哪个模型、哪个接口，
并附上服务端原话（见下一条）。GUI 的模型名输入与下拉填充只做首尾 `strip()`，不得改动内部空格。

**服务端说了什么，必须原样带给用户**（1.2.1 起）。`AIEngine._service_error_message()` 取
`error.message` 并附在 `last_error` 后面：`HTTP 400：请求参数或模型不兼容；服务端说：invalid
temperature: only 1 is allowed for this model`。历史上正是"丢掉响应体"让一个参数问题查了很久。

**AI 调用要能自愈这三类服务端差异**（都在 `AIEngine.chat()` 里，重发不消耗重试次数）：

| 现象 | 处理 |
| --- | --- |
| 400 且报文说模型不接受 temperature（实测 Kimi 思考模型只接受 1） | 记入 `_no_temperature`，去掉该字段重发；后续请求不再带 |
| 200 但正文为空且 `finish_reason == "length"`（思维链吃掉了 `max_tokens`） | 放大额度（×3，上限 8192）重发一次 |
| 429 | 用 `_rpm_from_message()` 从报文里学 `max RPM`，写进 `_rpm_limit`；`_wait_for_rate_slot()` 在本地按窗口排队；退避取 `max(Retry-After, 3×2^attempt)` |

内置人设的 `max_tokens` 不得低于 800（思考模型会把它吃光）；`styles/custom/` 会覆盖同名内置文件，
改额度时两处都要看。

**发送后必须还原被最小化的窗口**：`wechatauto` 发送前会最小化与微信重叠的窗口且不还原，
`WeChatBridge._after_send()` 调 `_restore_minimized_windows()`（`release_foreground(True)`），
并作废库的 `_last_visible_ok` / `_last_visible_ts` 缓存 —— 不作废的话下一次发送会以为微信还在
最前面，键鼠可能点到别的窗口上。


发送的可靠性约束（插件不需要知道，但别绕过它直接发）：

- 所有发送都在**一个只含单线程的执行器**里排队 → 任何时刻只有一次界面操作，不会两次点击互相打断；
- 每次发送套 25 秒超时；超时后**不重发**（结果不确定），记为一次发送失败；
- 连续失败由运行时的连接守护触发重连，重连时 `transport.reset_sender()` 换掉卡死的线程；
- 两条消息之间至少间隔 `wechat.send_interval` 秒（默认 1.5），这是给微信风控留的余量。

## 1.2.20 机器人本地图库与钩子补充

**新增插件（2 个，共 34 个）**：
`sticker_admin`（表情包管理：本地图库窗口的开关与动图抽帧数，默认开）、
`sticker_steal`（自动收集别人发来的图片，默认开、默认不收私聊）。

**新增钩子 `PluginManager.run_on_observed(message, runtime)`**（同步，无返回值）：
在 `WeChatRuntime.ingest()` 里、**访问策略之前**调用，**每条被观察到的消息都会走到**
（包括没点名的群消息）——`on_ingest` 只在"准备回复"的消息上触发，收不到群里的表情。
钩子内**不允许**做 IO/await：只登记"看到了什么"，下载放到 `tick`/`on_prepare` 等 async 钩子。

**新增接口（`src/sticker_api.py`，全部在本机管理中间件之后）**：

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| GET | `/api/stickers?q=&page=` | 每页 12 条；返回 `items[{id,label,description,uses,thumbnail(base64 PNG),width,height,animated}]`、`total`、`pages`、`page`、`collecting` |
| POST | `/api/stickers/import` | 体 `{items:[{name,label,path}]}`；1–20 张、单张 ≤6MB、单次 ≤64MB；**路径必须落在 `data/plugin_uploads/sticker_admin` 内**；先逐张解码校验再统一入库（坏图整批拒绝） |
| POST | `/api/stickers/{id}` | 体 `{label,description}`；标签 1–40 字且不含 `[]\|` 与换行，说明 ≤1000 字，标签不可重复 |
| POST | `/api/stickers/{id}/recognize` | 用 `image_understanding.visionModel`（留空则 deepseek 端点用 `deepseek-flash`，否则跟随聊天模型）识别，**只改说明不动标签**；失败时保留原说明 |
| DELETE | `/api/stickers/{id}` | 只删本地收藏的文件与索引 |

存储：`data/stickers/index.json` + `images/`，主键 = 图片字节 sha256（与 QQ 版同口径），
索引另有 `index.json.bak`（损坏时先尝试恢复）。缩略图/抽帧用 **Pillow**，后端不依赖 PyQt6。
容量上限（数量 800 / 96MB，插件可调）超出时按 `(last_used, created)` 淘汰。

**发送计次**：只有**发送成功**后 `image_library.on_sent()` 才会按 `_reply_meta.library_ids`
给自动收集库的条目 `uses += 1`（手动图库的标签不计数）。

**回复文本整理**：`src/reply_text.py` 的 `tidy_reply()` 在发送前压掉汉字之间的半角空格、
去掉同一条回复里重复的长句（≥6 个实义字符）、去首尾空白；**发送层保持"无标记分支原样发送"**
不变（`tests/test_pacing_markers.py` 保护该行为）。跨条复读由
`WeChatRuntime._avoid_repeating()` 处理：与 `reply_dedupe.recent_replies()` 高度相似时
带"禁用句"重生成**一次**。

**等比例缩放**：`gui_config.json` 新增 `proportional_resize`（真布尔，非法值回退 false）；
`src/gui/window_sizing.keep_ratio(rect, base, ratio, edges, min_w, min_h)` 是纯函数，
**锚点必须取起始几何**（拖"上+右"角时若取自由拖动后的矩形会把下边一起拽走）。

## 6. 插件清单（微信版）

当前保留与适配（共 34 个）：
`conversation_memory`、`chat_controls`、`emotion_state`、`image_library`、`sleep_mode`、
`burst_merge`、`send_pacing`、`anti_repeat`、`reply_dedupe`、
`chatter_guard`、`keyword_reply`、`typo_sim`、`short_followup`、`mention_nudge_guard`、
`reply_guards`、`quote_reply`、`turn_complete`、`sticker_cadence`、`addressee_guard`、
`image_understanding`、`mention_gate`、`sticker_memory`、
`reply_preferences`、`robot_admin`、`private_replies`、`keyword_trigger`、`group_monitor`，以及下列五个适配插件。

新增（5 个）：
`mood_guidance`（情商规则）、`quick_reply`（寒暄秒回）、`computer_helper`（电脑故障排查）、
`speech_to_text_wechat`（本地语音转文字）、`image_ocr`（本地 OCR 读图）

**删除**（有封号风险或平台不存在，见 `封号风险与功能取舍.md`）：
`probability_reply`、`poke_counter`、`face_decode`、`face_names`、`sticker_cap`、`icebreaker`、
`affinity_proactive`、`pixiv_illust`、`forward_summary`、`speech_to_text_qq`、`proactive_chat`、
`silence_ping`、`scheduled_announcer`、`typing_indicator`、`share_card`

1.2.17 进一步从产品与安装包删除：`context_chime`、`repeat_echo`、`question_relay`、`at_mention`。

## 7. 微信消息类型 → 媒体映射

| 微信类型 | 我们的处理 |
| --- | --- |
| `文本` | `text` = 正文；识别 @ 机器人 |
| `图片` | `media` 加 `image` 段；`text` 补 `[图片]`；可下载后走 OCR / 视觉模型；**自动收集表情图会把它收进本地图库** |
| `语音` | `media` 加 `voice` 段；`text` 补 `[语音]`；可下载后本地转写 |
| `视频` | `media` 加 `video` 段；`text` 补 `[视频]` |
| `动画表情` | `media` 加 `emotion` 段；`text` 补 `[表情]`。**微信 4.x 的表情内容在本地库是加密的**（`wechatauto` 无表情下载接口），因此不识别、不收集，也不要去试 `download_file` |
| `文件/链接/卡片` | 按正文开头判断：`[链接`/`链接` → `link`；`文件`/`[文件` → `file`；`位置` → `other`；名片 → `other` |
| `动画表情` | `media` 加 `emotion` 段；`text` 补 `[表情]`（4.x 表情内容加密，不做识别） |
| `系统消息` | 直接丢弃，不进入插件管线 |
| 自己发的消息 | 直接丢弃（`attr == 'self'` / sender 是自己） |

## 1.2.4：故障诊断与发送兼容

wechatauto 的 mentions 转成正文 `@昵称`（没有微信 @ 提醒），禁止调用其 OCR 成员面板路径。
发送失败保留服务端说明，不做精确/模糊二次发送。其他后端沿用原接口。
回复准备（合并、插件、模型锁等待、模型重试）总预算由 `wechat.reply_timeout` 控制，默认 90 秒；超时标记 failed 并继续下一条。发送仍为 25 秒且不重发。
日志记录监听到达、事件循环投递、队列等待、模型生成、发送各阶段耗时；监听到达时间差包含微信写库延迟，不代表纯轮询耗时。
后台日志在配置目录的 logs/backend.log，按 5 MB 滚动保留 5 份；不记录消息正文或密钥。
Qt 请求结果回调不释放线程；仅 finished 后清理，关闭窗口时仍有线程则推迟关闭。

监听通过数据库代理过滤 get_sessions 的结果，保留增量读取、水位与发现新会话语义。模型诊断仅保存 has_reasoning、reasoning_tokens、completion_tokens、finish_reason，不保存思维链正文。


## 1.2.5：输入失败与关键词链路

wechatauto 主进程不再创建原生微信 GUI 对象，SendMsg 通过独立进程运行 UIA 文本发送：同一线程内创建控件、精确会话校验、输入框焦点确认、正文回读匹配，然后只按一次发送键。原生崩溃不传播到控制台。独立进程在外层 25 秒超时前终止，不重发；仍受原发送单线程、间隔与连续失败重连约束。文本不走 OCR 降级；普通正文 @昵称沿用 1.2.4 的用户确认。
固定规则和群聊触发词统一使用 NFKC、casefold 和零宽字符规范化的包含匹配；YAML 触发词可为列表或逗号/换行分隔字符串，仍忽略单字触发词。关键词规则兼容带缩进的 v1 JSON，不因 JSON 空格而默默失效。


1.2.5 速度补充：搜索框和聊天输入框只在微信窗口子树中查找，跳过逐个旧类名 Exists 超时；群聊默认合并窗口从 8000ms 改为 3000ms，用户显式保存的窗口不覆盖。私聊不等待群聊合并。队列在入库时唤醒，完成一条后仅让出事件循环，不再额外 sleep(0.8)；发送间隔仍由 transport 闸门执行。

微信宿主通过 PluginManager 的 default_overrides 为 burst_merge 设置 3000ms 的默认值；覆盖只复制当前实例的 META，不修改共享插件类，也不覆盖用户已保存参数。插件列表与“恢复默认”返回宿主的 3000ms。


## 1.2.10：连接测试与管理反馈

`POST /api/ai/probe` 和 `POST /api/ai/models` 接受当前表单的 `api_url/api_key/model/api_style/request_timeout`（3–300 秒，可省略以使用已保存 ai.timeout）；Key 留空使用已保存密钥。独立临时 AIEngine 不保存配置、不占用回复模型锁，不连接或发送微信消息；结束必关闭客户端。probe 并行读取模型列表和最小对话，两者分别使用当前请求超时，返回有效端点、超时、分项结果和耗时；列表失败不掩盖对话成功。GUI 等待预算为表单超时加 5 秒。请求不做一般错误重试，保留引擎的参数兼容处理。

## 1.2.53：本地聊天记录检索与上下文节流

`conversation_log` 保留各账号、群聊/私聊的原始归档，不因提示词预算或升级清理。模型输入按当前问题、人设与近期直接对话优先；`conversation_memory` 在同一账号、同一会话内补充近期群聊背景和最多 3 条相关旧记录（可关闭/配置）。检索采用本地中文相邻双字/英文词匹配，不调用额外模型；历史仅作为带发送者的引用数据，不作为系统指令。其他人设的机器人回复不得进入提示词。

`build_history(..., included_ids=None)` 可收集本次实际纳入的原始消息 ID；`ConversationStore.context(..., excluded_ids=())` 和 `recall(..., excluded_ids=())` 排除这些记录、当前消息与合并消息。近期记录与旧记录共享 contextChars 剩余预算；直接对话预留部分预算给检索。Token 节流保护所有 system/tool/多模态消息以及当前问题；字数预算在消息条数未超限时也生效，引用摘要使用 user 角色，统计仅报告字符差额，不宣称精确 token 节省量。不修改磁盘原记录。

群聊触发的 `chat_controls.reply_decision(message,runtime)` 返回 `(allowed, reason)`，`should_reply` 保留布尔兼容接口。原因区分未确认 @/关键词、概率未命中、每分钟数量、冷却剩余秒数；不得为了排错默认放宽触发/限流。冷却独立覆盖其完整配置窗口，不因一分钟统计窗口截断。

加密表情无 CDN 时先用消息 MD5 与本机表情库原字节匹配，匹配后的 SHA256 必须同时符合本机内容 ID；不能仅凭标签猜图。找不到原字节仍视为未识别。纯加密表情且原文件不可得时使用简短本地提示，跳过无意义的文字模型调用和自动配图；详细错误仅写本机诊断。普通图片/带文字的问题保留原模型链路。

管理页操作显示进行中、成功或失败，刷新不覆盖操作结果；空输入有明确提示，密码保存失败保留输入，成功才清空。本机 robot_admin 设置更新不等待微信连接锁，原有身份验证、队列取消和发送闸门保持有效。status 新增 pending_current（当前模式与账号）和 pending_other_accounts（其余积压），原 pending 仍保留总数；管理页展示当前账号数量，不补发其他账号记录。

## 1.2.6：安装快捷方式图标

桌面与开始菜单入口显式使用程序 exe 的内嵌图标。升级时，桌面快捷方式已存在就更新，即使没有再次勾选“创建桌面快捷方式”；管理员启动目标仍为启动脚本。安装往返自检使用独立的验证 AppId 及安装目录内的快捷方式目录，不覆盖真实安装的卸载信息和桌面入口；验证会模拟旧快捷方式缺失图标并检查修复结果。
