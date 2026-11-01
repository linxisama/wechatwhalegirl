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
- 测试侧：把「回读 3 次」的硬编码期望改为不变式（所有正文完整性回读必须早于发送键、每次剪贴板写入都必须恢复）；产品代码未改、不递增版本。
- 耗时归因（纯观测，1.3.42 内追加）：真机一次发送实测 27~34 秒，日志只有总数看不到分段。本次给发送链加时间戳——客人端 `guest_service.send_text` 回包新增可缺省字段 `timings`（total/receive/agent/gui_ready/press/confirm/write，毫秒），宿主 `host_bridge` 与发送子进程各写一行 `send timing ...`；旧客人端不带 timings 时宿主照常工作。行为、时序、超时数值（18.0/6.0/2.5/24/35/85）一个未改，发送失败路径暂不归因。
- 语音消息（行为变化，1.3.42 内追加）：未转写且无附带文字的语音此前会回一句「语音已收到，但这边的本地转写暂未完成…」，用户看到的是内部能力说明而不是聊天内容。现改为静默跳过——不调用模型、不发送，inbox 记 `skipped`（`语音未转写，本条不回复`）；有附带文字的语音仍正常回答文字。
- 视频消息（行为变化，1.3.42 内追加）：以前视频只能当「看不懂的文件」跳过，现在按帧抽样进同一条视觉通路——transport.download_media(msg_type='video') 先把视频落到本机，src/video_frames.py 用 PyAV 进程内解码（冻结版 _internal 已带 av/av.libs，不依赖外部 ffmpeg.exe）：读得到时长时第 i 帧取 (i+0.5)/N*duration（确定性，不随解码速度漂移），读不到时长才顺序解码后按等步长取。帧数取新设置 videoFrames（「表情包管理」里的「视频识别抽几帧」，1~3，默认 3），与图片帧共享同一条 3 画面上限和 25 秒预算，视频帧计入挤占名额。解码库缺失、文件损坏、超过 200MB 或 10 分钟一律只降级并写中文说明、绝不抛异常，其它图片照常识别；gifFrames 与动图抽帧行为一字未改。新增 tests/test_video_frames.py（8 用例）。
- 修改：确认改为墨迹对比——按下发送键前记录输入框墨迹像素与横向墨迹列数，点击后在 6 秒窗口内重新取帧，`empty` 或 `caret_like` 且（列数 ≤ 2 或像素降到原值 30% 以下）即判已清空；单个标点粘贴后墨迹不变，仍判不确定，不会误报成功。点击前另加 2.5 秒预留，剩余时间不足时不按键并明确报「未按发送键」。按键后不再重复标题 OCR（点击前已校验两次），避免把已成功的发送重新翻成不确定。绝不重发、绝不二次点击。另：本轮用真实客人端整窗帧离线重放（.tmp-sandbox-1339/guest-out/guest-full-2.png）量到预按键阶段一次发送要读 4 次标题（共 13 次 OCR 识别）与 9 次抓帧，18 秒预算在按键前就被吃光；超时数值一个未动，新增 tests/test_guia_prepress1343.py 锁定「预按键 9.0 秒时仍必须按下发送键并走完墨迹确认、只剩不到 2.5 秒时绝不按键」两条回归线。继续排查发现瓶颈是「每处校验都重跑整族 OCR」：一次发送 9 个校验点 × 3 个变体 = 27 次识别，预按键就要 13 次，18 秒预算被吃光后余量闸门没机会按键。现在改为「标题条像素签名」复用：同一次发送只在第一处做一次整族核验（三份一致才算读到），之后每处改比对标题条区域的 sha1 指纹（含该区域几何与窗口矩形，窗口一动指纹必变），指纹一致即复用已核验标题、零 OCR；指纹不同、取不到帧或任何异常一律退回整族 OCR，读不到或读到别的会话名照旧 fail-closed 拒绝；缓存只在单次发送内有效，send() 入口清空、绝不跨发送复用。因此按键之后那次标题核验（_confirmation_target）也变成签名比对、零 OCR，代码对齐「按键后不再重复标题 OCR」。同时修掉同一现场暴露的堵死缺陷：CONFIRM_RESERVE 余量检查前移到写剪贴板之前（余量不足时连剪贴板都不写、一个字都不粘），并且「已粘正文、发送键未按下」的失败路径退出时用 Ctrl+A + Delete 把输入框清回粘贴前状态（日志 reason=已清回输入框，永不按回车、永不写剪贴板、按过发送键后一个字都不碰）；清理前还必须自证仍在目标会话：只有本轮缓存过「已核验标题 + 同帧标题条指纹」且当前帧指纹与之一致才按 Ctrl+A + Delete，指纹不同、驱动没有签名能力或取帧失败一律一个字都不碰（日志 reason=target_changed / target_unverified），避免删掉用户在别的会话里写的草稿。新增 tests/test_guia_titlesig1343.py（8 用例）锁定：指纹不变⇒零 OCR 复用且发送成功、指纹变了且读到别的会话名⇒拒绝、取不到签名⇒处处退回整族 OCR、全族也读不到⇒拒绝、余量不足⇒clipboard.writes 为空且未按键、粘贴后失败⇒清回输入框且无回车、粘贴后切走会话⇒零清理按键且日志 target_changed、驱动无签名能力⇒零清理按键且日志 target_unverified。

- 视频抽帧（沙盒后端可用，本版内追加）：沙盒后端下不了媒体（host_bridge.download_media 是 fail-closed 空桩），收到的视频只会记「第 N 个视频没能落到本机，未抽帧」。本次给隔离通道补一条客体侧抽帧动作 video_frames：宿主 SandboxBridge.video_frames(chat, local_id, shard, want, max_side) 走既有 _rpc（新常量 VIDEO_FRAME_TIMEOUT = 20.0），客人端 GuestService.dispatch 用 bridge.download_media(chat, local_id, 'video', shard) 把视频落到本机后就地调 src/video_frames.frames_for_video，把 data URL 帧随回执送回宿主；参数严格校验（chat 1..256、local_id 为 str/int 且非空、shard ≤256、want 为 1..3 缺省 3、max_side 为 256..1024 缺省 768），非法回「隔离视频抽帧参数无效」，任何异常或超时都 fail-closed 成中文信封、绝不抛出；帧总量超出回执 4MB 上限时按预算裁帧，避免写回执失败打死代理循环。插件侧识图改为三级取画面（CDN 直链 → 客体侧 video_frames → 本机 download_media 再本地抽帧），任一层成功即停，失败文案与原来逐字一致；本地 wechatauto 通道仍走原有 download_media 路线。25 秒识图预算、maxImages=3、gifFrames 链路与既有发送/连接/轮询超时（24/35/85、18.0/6.0/2.5）一个未改。新增 tests/test_video_frames_guest.py 覆盖客体动作、宿主信封、通道分支与三级回退顺序。

- 一键接入 DSH（新能力，本版内追加）：以前要把桌面 DSH 接到控制台，得手工写 `whale_workspace_mcp_v2.py`、手工填 `mcpServers` 配置、再自己贴凭据。现在 Agent 页「生成并写入 DSH 配置」一条路做完：先按本机 `.venv-wechat` 定位 python（找不到就整件事失败，**不会**签发凭据留下悬空授权），撤销同名 peer 的旧凭据后签发一份 30 天全能力凭据，再把配置块幂等写进 `~/.dsh/profiles/desktop/cordis.patch.yml`——首次改动前自动备份为 `<文件名>.bak-YYYYmmddHHmmss`，块尾用 `# >>> whale-wechat-workbench` / `# <<<` 标记包裹，重复执行只替换块内字节、标记外一个字节不动（CRLF 保持，非 UTF-8 拒绝）；配置块内容是一个新预设 `- insert:`（`@deepseek-ai/dsh-agent-preset`，id `whale-workbench`、名「鲸鱼娘工作台」，其 plugins 里挂 `@deepseek-ai/dsh-mcp-client`，serverName `whale_wechat`、transport stdio、command 为上一步的 python、args 指连接器脚本、env 带 `WHALE_BOT_URL`/`WHALE_AGENT_TOKEN`/`PYTHONUTF8=1`、`toolCallTimeoutMs` 75000），所以工具只在这个预设的会话里出现，原有「鲸鱼娘微信聊天（无宿主工具）」预设的 `ctx.tools.restrict({allow: []})` 不受影响。入口 `POST /api/agent/dsh/provision`（body `{"write": true}`；`write: false` 只回配置块文本不落盘），面板按钮「生成并写入 DSH 配置」/「只预览配置块」。连接器同时补两个控制台工具 `console_catalog` / `console_execute`（GET/POST `/api/agent/control/*`，execute 每次带 request_id），仍然只依赖标准库，DSH 用系统 python 也能跑。既有 `/api/agent/dsh/provision` 在旧构建里不存在（返回 404），故本版必须重新冻结才能用面板按钮；直接调 `src/dsh_mcp_provision.provision()` 的老版本控制台同样可用。新增 tests/test_dsh_provision.py（12 用例，全部离线，用 env `WHALE_DSH_PATCH` 指临时文件，绝不触碰真实 `.dsh`）。

- 修「只预览」会把在用的 DSH 凭据顶成废票（本版内修复）：`POST /api/agent/dsh/provision {"write": false}` 原先在写盘判断之前就撤销同名旧凭据并签发新凭据，令牌只出现在返回体里、没落进配置文件，结果是预览一次就把 YAML 里那条凭据变成废票，MCP 调用随即 401「请重新绑定或凭据无权限」。现在 `write: false` 走独立分支：只校验管理员绑定、生成连接器与配置块（令牌位是占位符 `<WHALE_AGENT_TOKEN>`）、不签发不撤销、不碰配置文件；真正写入时才签发并同步撤销同名旧凭据。新增 tests/test_dsh_provision.py 两条用例（预览后 `agent_authority.json` 字节不变、已签发凭据的 digest 仍在、配置块文件字节不变）。

## 1.3.41 内部候选：隔离沙盒里中等字号会话标题读不出来导致发送被拒
- 现象：隔离沙盒内实际发送被拒，报「标题识别未通过，本次不继续输入或提交：未能核验完整标题」，配对停在 waiting_code=title_unverified，消息收得到但发不出去。
- 定位：会话标题墨迹高度 18 像素时走中等字号 OCR 变体表 ((2.5,150,32),(3,150,64),(4,150,0))，在沙盒 125% 缩放下三个变体全部读回 0 行（state=ocr_empty）。
- 修改：中等字号变体表改为真机实测可读的三组（NEAREST 2 倍 + 阈值 150、平滑 2 倍原图、平滑 3 倍原图），OCR 裁剪新增重采样参数；聚合规则改为「所有成功完整读到的文字必须一致且至少 3 个，任何一次读到不同文字仍然拒绝」，空结果不再立即中断。完整墨迹覆盖校验、逐字一致、不按期望会话名挑选结果、读不出就失败，全部保持不变。

## 1.3.40 内部候选：隔离后台不再「超时即掉线」，重启窗口消息不再被当历史跳过

两处接收侧修复，都不改变发送链路。一是隔离通道轮询：以前客体稍微慢一点（单次轮询超过 3 秒）就整条消息监听被拆掉，宿主日志每 60~100 秒重复「隔离监听已停止：隔离请求超时」→「5 秒后重连」，接收在这些窗口里完全停摆。现在区分「变慢」与「客体不可信」：超时按 0.5 秒起、上限 2 秒退避重试，连续 60 秒吃满预算才按原样拆线并置离线；协议、身份、摘要、心跳不符仍立即失败。轮询单次预算 3 秒→10 秒、轮询间隔 0.25 秒→1 秒，降低对共享目录与消息库解密的压力；同一场超时风暴只记一条「隔离通道响应变慢，正在重试」，恢复时记一条「隔离通道已恢复」。二是监听水位基线：库只在成功投递后才落盘水位，冷启动一条都没投递时水位文件根本不存在，于是每次新建监听都把每个会话重新播种成「当前最新序号」，而增量查询是严格大于——停机窗口里到达的那条消息序号恰好等于播种值，被永久跳过。现在播种完成后立刻强制落盘一次基线，重启后从基线继续，窗口里的消息照常投递；安装前的历史消息仍然跳过（首次启动的上下文不变）。超过 wechat.max_message_age（默认 25 秒）的补投消息仍按原规则判过期，不会产生迟到回复。

应用版本 1.3.40，仍为内部测试候选；功能验收前不制作新的正式安装包。

测试侧修复（不改变产品行为、不新增版本）：`tests/test_prepare_skips_unreadable1339.py` 的 `setUp` 原先只保存了 `patch.start()` 返回的替身，`tearDown` 再对这些替身调用 `stop()` 是空操作，于是 `sandbox_manager.find_wsb`、`subprocess.run`、`sandbox_manager.packaged_version` 三条全局补丁在整个测试进程里永久生效；全量回归时排在它后面的 `tests/test_sandbox_cli_timeout1319.py` 因此恒定出现 9 例失败（拿到的是替身写的 `{"ExitCode": 0}`，而不是被测的 stdout/退出码语义）。现在保存 `_patcher` 本身并在 `setUp` 里逐个 `start()`，模块结束后补丁全部还原，该模块单独跑与全量跑结果一致。


## 1.3.39 内部候选：控制台直接打开 Windows 沙盒

沙盒面板新增「打开 Windows 沙盒」按钮：以前必须自己从开始菜单点开 Windows 沙盒，现在由控制台自己生成 .wsb 配置并启动一台，等实例出现后把实例编号填进面板的实例下拉框并刷新状态。同一排还新增「在沙盒内安装微信」按钮：选一个本机的微信安装包（.exe），控制台把安装包所在目录只读映射进沙盒的 `C:\WhaleInstall`、把宿主日志目录映射成可写的 `C:\WhaleOut`，再执行 `C:\WhaleOut\install.ps1` 静默安装，随后每 5 秒查一次沙盒里的 `C:\Program Files\Tencent\Weixin\Weixin.exe`，最多等 300 秒；沙盒内已装好就直接复用，不重复运行安装包。

启动逻辑在 `src/sandbox_manager.py`，配置文件写在 `data/sandbox_launch/` 下并且只清理过期的旧配置；启动前先查 `wsb list`，已有实例就直接复用、绝不启动第二台，等待新实例的上限是 120 秒、每 2 秒轮询一次。失败一律给出稳定原因码与中文说明：`sandbox_exe_missing`（找不到 WindowsSandbox.exe）、`config_write_failed`（写 .wsb 失败）、`launch_failed`（启动进程失败）、`launch_timeout`（120 秒内没等到实例）。

安装同样是控制台能力：入口是 `POST /api/sandbox/install-client`，干活的是 `src/sandbox_manager.py` 的 `install_client()`，脚本与日志落 `data/sandbox_install/`；安装包必须是已存在的绝对路径 .exe，符号链接和相对路径一律拒绝，同一时间只允许一次安装（并发请求返回 409）。失败同样只给稳定原因码与中文说明：`installer_missing`（安装包不存在）、`sandbox_not_running`（沙盒未打开）、`share_failed`（共享目录失败）、`exec_failed`（客体拒绝执行脚本）、`install_timeout`（300 秒内没出现微信主程序）；已装好时返回 `already_installed`。

修复一条读不到的旧通道目录让任何沙盒（包括全新沙盒）都无法准备的问题：`src\sandbox_manager.py` 的 `_existing_pairs_for_prepare` 现在区分两类失败——目录本身读不到（权限拒绝等读盘错误）只跳过这一个目录，并把目录名放进准备结果的 `skipped_channels` 里报出来；目录能读到但配对结构不符（JSON 无法解析、`version` 不是 1、通道编号与目录名不符）仍然整体失败，返回「现有隔离通道元数据无法核验，请先检查配对记录；未覆盖任何映射」。1.3.18 遗留的 ACL 损坏目录 `5d78451fff6f4f3b987e6b84525b983e` 因此不再需要管理员权限才能绕过，恢复与映射流程的严格校验原样保留。

版本号升到 1.3.39，仍是内部测试候选：功能验收前不制作正式安装包。本次新增「打开 Windows 沙盒」与「在沙盒内安装微信」两条能力，发送链路未改动，隔离通道的准备只放宽「目录读不到」这一类。

修复本机配置保存与通道启用全部 400 的问题：`src\persistence.py` 的 `atomic_text()` 仍按原顺序在同一目录写临时文件、fsync、备份 `.bak`，但 `os.replace()` 被 PermissionError（WinError 32，文件被其它进程占用）挡住时不再直接失败，而是记一条 warning 后就地重写目标文件并 fsync；只有 PermissionError 才降级，其它 `OSError` 仍原样上抛，「写失败向上抛出」的契约不变，临时文件在任何分支都会被清理。新增离线回归 `tests\test_persistence_replace_fallback.py`。

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

## 1.3.31：缩短后台定位与共享请求的耗时（实发待验收）

1.3.30 的一次正式后台调用返回结果不确定，独立检查未取得新增自身消息回执，现有草稿与消费记录保留。实测发送进程多次检查像素边缘耗时明显，共享请求镜像也出现发布后缺名延迟。本版只做两处有界修复：三点颜色完全相同的边缘采样直接返回无边缘（原差值为零），完整输入框、标题、草稿和提交前核验不变；已预建的 control/request.json 以独占句柄原位发布同一签名请求，不重命名文件。不可变 canonical 请求仍先独占落盘，镜像保留同一 UUID、epoch、摘要与原截止时间。仅取得写句柄前 WinError32 可在剩余原预算和50ms中较短期限内等待，缺槽、权限、短写、写入/截断/刷新/关闭失败均不重建、不重写、不回退。其他业务文档发布规则不变，不延长识别或发送时限、不重新投递请求。离线提速与对照查询不是实发成功；正式后台发送仍须独立新增消息验收。

## 1.3.30：修复隔离 OCR 发布时的共享文件缺名（实发待验收）

实测同一沙盒共享目录中，ReplaceFileW 发布 OCR 心跳和结果会让客体反复遇到文件不存在；增加等待至原识别截止时间也未稳定恢复。仅对 OCR 既有心跳和结果槽，改为取得 share-mode 0 独占句柄后一次写入、截断、刷新、关闭，不重命名文件。取得句柄前的共享冲突可在同次有限预算内等待读取结束；写入开始后的异常不重写、不换通道。签名、路径和大小限制保留，结果发布受原 OCR 请求截止时间约束，不延长识别或发送时限、不重新投递请求。停止中继保留心跳文件，旧心跳按原八秒有效期自然失效，实际识别仍须收到本次签名回执。其他业务请求和消息发送协议不变。只读连续54次OCR通过是定位与原型证据，正式后台发送仍须独立新增消息验收。

## 1.3.29：排除同名的微信功能说明窗（实发待验收）

微信 4.1.8 的新功能说明与聊天主窗同名、同进程、同为 Qt 外层窗口，且没有 owner 或工具窗标记，曾被误计为两个账号主窗。Qt 外层候选现在必须至少有一个最小化或最大化窗口样式位；两者均无的说明窗不列为主窗。初次枚举与发送前身份复核使用同一条件，样式读取失败仍拒绝；mmui 主窗保持原规则。真正的两个账号主窗仍按歧义拒绝，不按大小、前台状态或顺序任选。标题、空草稿、当前会话、单次提交与超时不重发保持原约束。此修复及离线检查不代表真实后台发送验收通过。

## 1.3.28：修复展开图标被误判为草稿（实发待验收）

沙盒实测中，空输入框右上角展开控件的12个灰色像素进入正文区域，旧偏移模板未识别，导致始终报告已有草稿。增加完整25点形状的精确偏移：相对独立核验的输入框框体，右边减25、上边加3。保护区必须完整逐点相等、中性灰且唯一命中，只扣实际控件像素；任意其他标点、图片、颜色或不完整形状仍拒绝。原两套模板保留，光标仍须真实闪灭后读到空白帧，不直接忽略静态细线。保留标题完整核验、当前会话范围、单次提交、超时不重发及主机前台保护。离线与窗口像素验证不替代真实消息记录验收。

## 1.3.27：标题字号适配与准确诊断（实发待验收）

修复实测18px标题在6/8倍放大时错字而误判“聊天未打开”的问题：按全部标题墨迹的高度选固定处理表，16～24px使用2.5/3/4倍和32/64/0px白边，其他高度保留旧表。每次仍需三份完整识别逐字一致，不按目标挑结果、不模糊匹配。新增仅含有限状态与计数的标题诊断；识别失败显示“标题未核验”，只有完整标题确实不同才提示目标会话不符。不会改变当前会话范围、草稿保护、单次提交、超时不重发或主机前台保护。原灰度标题回归通过不等于真实发送通过；继续按完整回归、冻结检查、带数据覆盖升级与单次授权实发分别验收。

## 1.3.26：最小化沙盒的请求通信修复（实发待验收）

- 只读实测确认新 UUID 请求在最小化沙盒中延迟约 8.7 秒才可读，既有签名槽约 28 毫秒。预建固定请求槽传递同一完整签名请求，原 UUID、正文、摘要和 deadline 不变，各发布一次，失败不重投。
- 所有动作执行前去重；消费记录与回执绑定请求摘要，冲突保留证据。墙钟回拨不能复活过期请求，旧镜像不能重复恢复发送、无限续租或重建已取走的回执。
- 修复文件发布后重新起算等待期限的问题。排队、发布及回执核验共用入口预算；超时保持未知不重发。当前会话限制、主机前台保护、配置和历史保留不变，真实发送仍需独立验收。

## 1.3.25：共享目录访问与失败配对修复

修复管理员启动时新隔离目录只授予目录所有者、实际用户无法读取的问题。新目录在创建时仅授权实际用户、SYSTEM 与 Administrators，不修改系统安全策略或旧目录权限。只有首次准备和恢复都明确在共享程序步骤被拒绝、尚未共享通道或启动代理且无业务记录时，才允许“修复目录并恢复配对”：原目录完整归档，保留原通道身份、密钥及错误记录，使用新的已核验程序构建。共享/启动结果未知或修复中断继续保留证据并拦截重做；无需关闭沙盒或重新登录。配对不会启用通道、启动机器人或发送消息；当前构建的后台实发仍待独立验收。

## 1.3.24：失败配对续接与沙盒桌面预检（后台实发仍待验收）

- 修复“recovery.json 存在即永久拒绝”的状态机缺口：仅对身份、原始 failed session、完整程序指纹及已成功的通道/程序映射均可核验，且尚未进入结果未知启动阶段的配对，允许再次显式续接。保留原恢复证据及独占续接日志，不重复共享已确认通道；未知 share/launch/heartbeat、证据冲突或业务遗留继续拦截。
- prepare 创建配对前、恢复/升级执行共享或启动前，先用 ExistingLogin 执行固定只读身份预检。0x80070520 表示指定登录会话不存在，界面明确提示先连接并打开沙盒桌面；不能用 System 替代客体交互会话，也不要求修改安全策略。
- 沙盒窗口显示首次准备失败和恢复失败各自的有限阶段、HRESULT 及返回码；已确认映射可点击“继续恢复配对”，未知映射禁用重复准备/恢复。状态读取失败清空旧候选，操作结果与失败诊断分别保留，不输出原始异常、路径、命令或配对密钥。
- 本轮解决配对阻断与诊断，未自动启用通道、启动机器人或发送消息。主机前台兼容实发不能替代当前构建的沙盒后台实发验收。覆盖升级保留原配置、对话和记忆。

## 1.3.23：继续修复沙盒后台链路（当前构建实发仍待验收）

- 重新核对时，实际控制台与源码均为 1.3.22，配置为本机前台兼容发送，旧 Windows Sandbox 已关闭。1.3.22 历史实发记录证明的是前台坐标通道，不能替代严格后台或沙盒后台验收。排查已正常停止机器人，不补发旧队列。
- 修复沙盒 OCR 可变文件的瞬时读取失败：仅 ocr/relay.json 和 ocr/<32位小写hex>.result.json 在 Windows 错误 2/32 时作同次最多 50ms 的只读恢复。结果读取仍受原六秒单调截止时间约束，路径、大小、解析与签名每轮核验；权限/签名/解析错误以及不可变请求、事件、消费记录不重查。不会重投 OCR、RPC 或消息。
- PrintWindow 抓帧后必须先将目标位图从私有 DC 中移出，再调用 GetDIBits；无效 SelectObject 返回值拒绝，异常路径释放 DC 与位图，避免驱动/沙盒无法取图而误读遮挡屏幕。
- 修复输入框伸缩后沿用旧正文区域的问题：接住刷新后的区域，发送后重新测量当前输入框，空白检查前后继续核对精确会话；切到其他空白会话不再误报成功。输入框聚焦与发送均使用受保护的单次 SendInput 点击，不再调用会临时改渲染子窗样式的 wx_click。
- 点击按虚拟桌面计算绝对坐标并带 MOVE/ABSOLUTE/VIRTUALDESK，支持负坐标副屏；移动失败、越界或桌面几何变化时拒绝。持久提交阶段及悬停之后，实际按下之前再核对账号窗口、会话、完整正文、前台、落点与截止时间；发送键最多按下一次，未知结果不重发。
- 发布脚本不再无条件宣称覆盖升级/全量测试已通过，须以本版本验收报告为准。本轮未发送真实消息；当前构建的后台实发仍需沙盒恢复、人工登录和单次测试授权。

## 1.3.22：发送键改结构闸门、点击改 SendInput（真机实发验收通过）

- 根因一：`src/wechat_ocr_labels.py:locate_send_button` 的原前提「同一块内缩区做两份并排 OCR 必须读到『发送』」在本机不成立——同一帧标题区 4 倍 NEAREST 能读出「文件传输助手」，而发送键区 RGB 4/8 倍放大、单独按钮 8 倍 LANCZOS/NEAREST、反相、掩膜全部读不出「发送」（白字绿底 + 125% 缩放）。函数因此恒返回 `[]`，`_attempt` 每次回读正文后都以「未唯一定位到发送按钮」失败。
- `src/wechat_ocr_labels.py` 整文件重写：OCR 从必要条件降级为加分项（`explain_send_button` 的 `ocr` 字段取 `send`/`other`/`error`，异常一律不阻塞），改为多条像素结构证据同时成立才允许点击：
  - 扫描窗 = `send_region` 上探 `SCAN_TOP_MARGIN = 6` 行。为什么上探而不改 `inspect_composer`：真机绿色块顶边在 `send_region` 之外 1 行（实测块 y=549、region 顶边 550），而 `tests/test_pixel_sender1315.py:33` 的 `draft_box[3] < send_region[1] + 1` 锁死了「`send_region` 不得上探进正文区」；块被扫描窗边缘裁到即 `green_block_clipped` 失败。
  - 绿色块（`r<100 and g>140 and b<180`，4-连通）在窗内必须**恰好 1 个**，尺寸 30..120 × 15..55、填充率 ≥0.55、长宽比 1.2..4.5。
  - 块内缩区（`inset_x = max(4, round(w*.16))`、`inset_y = max(2, round(h*.12))`）取白像素（`min(pixel) > 150`，与标题 OCR 同一实测阈值）4-连通簇：主簇门槛 `floor = max(12, round(ink*.15))`，必须**恰好 2 个**；两簇墨量 ≥0.55·ink、跨距 0.35..0.95·内缩宽、左簇右边界 ≤ 右簇左边界+1、簇间 gap ≤ `max(2, round(hmax*.5))`、两簇宽度都 ≥ `round(hmax*.35)`、高度差 ≤ `max(2, round(hmax*.3))`、顶边差 ≤ `max(2, round(hmax*.35))`、垂直居中 `|ink_top - (inset_h - ink_bottom)| ≤ max(3, round(inset_h*.35))`、主簇密度 0.06..0.45。
  - 为什么能认「恰好 2 簇」：把真机启用态按钮内缩区按 4×4 块均值还原成 1× 后逐簇统计，阈值 150 时恰好 2 个主簇（11×10 / 12×10），阈值 120 时 2 簇 + 4 块碎片，阈值 80 时「发」「送」被抗锯齿像素连成单块——所以阈值固定 150 且簇数必须配合上面的尺寸/占比过滤；列投影不可用（两字之间没有零列谷）。
  - 失败一律 fail-closed，reason 码共 11 种：`no_evidence`（默认）、`region_out_of_frame`、`no_green_block`、`multiple_green_blocks`、`green_block_clipped`、`glyph_inset_too_small`、`no_glyph_ink`、`glyphs_not_two`、`glyph_geometry_invalid`、`glyph_noise_dominated`、`glyph_centering_invalid`；`explain_send_button` 的 `ocr` 字段另有 `skipped`/`send`/`other`/`error` 四态，只当加分项，不参与放行。
- 点击点改成**绿色块自身中心**：`sx, sy = geometry[0] + x + w // 2, geometry[1] + y + h // 2`；区域中心在真机上偏左，会压到圆角外的空白。
- `src/wechat_pixel_gui.py` 新增 `refresh_composer()`：清掉 composer/geometry/指纹缓存后重做一次像素核验并返回新的 `draft_box`；`src/wechat_guia_send.py:_attempt` 在回读正文之后、定位发送键之前调用它并重取 `send_region`（正文变多 → 输入框变高 → 工具栏下移时不会沿用粘贴前的区域）。
- 根因二（闸门修好后才暴露）：`gui.wx_click` 在本机对微信**完全无效**——`wechatauto/guia.py:1139` 先临时清掉渲染子窗 `MMUIRenderSubWindow` 的 `WS_EX_TRANSPARENT`，点击于是投进不处理输入的渲染层；实测点消息区后输入框选区像素哈希完全没变（连焦点都没换）。`src/wechat_guia_send.py` 新增 `_send_input_click(user32, x, y, sleep)`（`SetCursorPos` + 两次 `MOUSEEVENTF_ABSOLUTE` 左键按下/抬起，按 `GetSystemMetrics` 归一化），发送键那一击改由它投递；不动窗口样式，`_point`/`_require_foreground`/`_send_gate`/`submitted` 等既有闸门全部保留，只按一次，系统拒收时只松开左键并报不确定、绝不重发。`.venv-wechat` 里的 `wechatauto` 未改。
- 真机验收（隔离数据目录 `data/accept-send-1322`，目标「文件传输助手」）：`SendMsg('【1.3.22 实发验收】143405', '文件传输助手')` → `ok=True`、`elapsed=10.6s`、`diagnostic.submission_state='confirmed'`；聊天记录 14:34 真的出现该条消息（截图取证），发送后 `draft_state='empty'`、`_input_box_has_text=False`。
- 回归：全量 `.\.venv-wechat\Scripts\python.exe -m unittest discover -s tests` = Ran 2718 tests OK (skipped=5)、退出码 0；`tools/selfcheck_backend.py`、`tools/selfcheck_http.py` 退出码 0。未跑打包（安装包由用户构建）。
- 测试：`tests/test_pixel_sender1315.py` 的 `ShortButtonOCRTests` 按新语义重写（OCR 读不出/读出错字/抛异常都仍命中，多绿块失败且不再调 OCR），新增 `SendButtonGateFailClosedTests`（灰底无块、单簇/三簇、小点、贴边、块被窗边裁、区域越界都点不了）；`tests/test_wechat_guia_send.py` 新增 `SendButtonGateTest`（块中心而非区域中心、顶边被裁仍命中、越界不点、0/2 候选不点、刷新后的 region 生效、SendInput 只按下抬起各一次、系统拒收只报不确定不重发、驱动异常仍报不确定），`tests/test_send_phase_logging.py` 的假 user32 也按新点击路径补齐。定向 `tests.test_wechat_guia_send tests.test_pixel_sender1315 tests.test_send_phase_logging tests.test_pixel_title_passes1321` = Ran 140 tests OK。

## 1.3.21：遮挡下改为窗口自绘抓帧、失败原因带出（真机实发仍待验收）

- 坐标+OCR 通道的抓帧改走窗口自绘：新增 `src/wechat_capture.py`，用 `PrintWindow(hwnd, hdc, PW_RENDERFULLCONTENT)` 取整窗位图，再按渲染区原点与尺寸裁出渲染区，微信主窗被其他窗口完全遮挡时也能读到微信自身像素；`_grab_screen(box)` 的屏幕坐标 box 落在渲染区内就从位图裁子矩形，越界、`PrintWindow` 返回 0、尺寸异常或抛异常时才回退 `ImageGrab.grab(bbox=box, all_screens=True)`，双屏正确性不变。抓帧前后各取一次几何并比对帧尺寸的校验、以及「截图期间窗口位置或尺寸改变」的失败保持原样。
- 删除 `src/wechat_guia_send.py` 的 `_build` 里用实例属性把 `PixelGUI._grab_screen` 换成 `ImageGrab` 的覆盖（原意只为双屏 all_screens）：它会连遮挡无关抓帧一起盖掉，遮挡时抓到的是遮挡者像素。
- 只读布局检查失败不再丢掉消息：`guia_unavailable` 的 detail 现在带异常类型与原文，`_inspect_composer` 的 RuntimeError 带上 reason 码（如 `pane_divider_unconfirmed`），界面建议文案同步改为「微信被遮挡已不再影响识别，仍失败请把原因码反馈」。
- 真机只读验收（现网微信窗口 1264x748、`GetForegroundWindow()` 为 0、无交互桌面）：`readonly_ready()` 为 True、`available()` ok=True；同一进程内强制换回屏幕抓帧则报 `no_closed_frame_in_bottom_right_chat_pane`，证实此前读到的是遮挡者像素。
- 修复会话标题识别读不出现网会话名（此前 `current_chat()` 恒为空、发送前的标题校验永远不通过）：改为「同一块实测墨迹 + 固定三变体 + 必须完全一致」。先按标题区内实测墨迹收紧裁剪，再对同一块墨迹分别做 4 倍、6 倍、8 倍 NEAREST 放大 + 硬二值化（阈值 150）共三次 OCR；三次结果必须一致，且每次都要通过「识别框左右两侧的标题区空白里没有残留墨迹」的完整性检查，否则返回空标题。三档倍率不是拍脑袋定的：真机两种会话名（5 字「鲸鱼娘二号」、6 字「文件传输助手」）各扫一遍 scale{2,3,4,6,8,10} × threshold{None,120,150,180}，两张帧都全对只剩 4/6/8 三档 ×150。真机 17-18px 汉字在 BICUBIC/LANCZOS 放大和灰度增强下会把相邻笔画糊在一起并漏掉末字（实测读到「鲸 良二号」「鲸鱼娘二」），所以不再插值；不做二值化、以及过狠的 180，会把「助」读成「曲」这类同长度错字——完整性检查只抓得住漏字、抓不住错字，所以既要求三个倍率互不相同，也要求三份结果逐字一致（这正是旧版「3 倍不变、4/8 倍二值化」在「文件传输助手」上直接失效的原因）。也不再按上一轮词框回推第二块裁剪——上一轮漏字时，回推出的裁剪会连带裁掉漏字的证据，这正是旧实现「首轮少识别末字、复核轮读到另一个字符串」的成因。
- 标题完整性检查只查到实测标题区右边界为止：旧实现查到 `frame.width - 80`，把同一行右侧的工具栏图标和窗口按钮当成没识别的标题字，整行被无条件拒绝（这是遮挡修好之后仍读不到名字的第二个原因）。反过来，识别框一旦触碰标题区右边界即无法确认整行到此结束，仍判不完整并明确失败，不退回前缀。
- 真机只读验收（同一现网窗口、被完全遮挡、未前置）：`readonly_ready()` 为 True、`available()` ok=True；两种会话名各自实测——「鲸鱼娘二号」三个变体都读到该名、识别框 394..484 覆盖全部实测墨迹 395..482；「文件传输助手」三个变体都读到该名、识别框 393..501 覆盖实测墨迹 394..501，且 `current_chat()` 连读 5 次全部一致。真机实发仍待验收。

## 1.3.20：发送前窗口识别与复核（实发仍待验收）

- 主窗候选区分聊天主窗、附属窗和工具窗；窗口查询异常有单独诊断，账号仍须正向匹配，多个主窗有歧义时继续拒绝。
- 严格后台读取控件后、写入前重新核验账号窗口、桌面及非最小化状态；输入前重新读取精确会话标题、可写控件和空草稿，避免沿用已经切换的窗口或旧输入框。
- 坐标通道的前台与落点须归属于已绑定主窗或其渲染子窗；仅进程相同的另一顶层弹窗不再作为输入目标。未知窗口归属继续拒绝。
- 本轮只加强识别与发送前检查，不自动切会话、不清草稿、不增加发送次数；后台真实收发仍待验收，后续 Fish Tank 功能继续在其后。

## 1.3.19：只读就绪诊断、命令超时与历史日志（后台实发仍待验收）

- 修复 Windows 上只读协议文档的句柄阻止心跳文件原子替换、令代理自行停止的问题。读句柄允许共享删除，非独占发布使用经当前沙盒共享目录验证的 ReplaceFileW；固定心跳/控制槽的暂态缺名或共享冲突在同次读取内有界重查，权限、解析和签名错误仍立即失败；继续读取打开时的完整签名文档，后续读取可见新代文件；不忽略心跳失败，不重投请求，不改变发送时限。

- 修复后台重连探测异常被当成成功，以及 GET 请求失败返回空对象使界面保留旧在线显示的问题；失败明确显示本次错误，设置未加载成功时仍禁止保存。
- 增加独立的“修复未完成配对”入口：先停止机器人，复用原配对身份，派生新的客体通道目录，核验当前代理静止及新版新鲜签名心跳后登记；不关闭沙盒或微信，不自动启用或发送。普通准备仍不覆盖失败或不明映射。新沙盒不自带微信，界面明确安装和登录步骤。

- 输入控件树读取异常不再直接归为客户端不开放可访问性。无读取错误、已穷尽的浅层外壳最多延迟 150ms 再读一次，同次输入树查询共用两秒预算，继续复核账号、窗口、桌面、标题及草稿；不激活窗口、不改读屏标志、不写进程。此项只减少探测假阴性，不能解决或掩盖此前 Invoke 返回成功但未提交的问题。
- Windows Sandbox CLI 使用有界临时文件输出与直接进程等待，避免窗口后代保留输出管道使超时后的收尾等待失效；不结束沙盒、微信或用户窗口。保留 JSON、错误和超量输出的明确结果。
- 摘要中文件错误按首行有效日期区分历史日期、今日、待核对或日期未确认，警告同理；保留原文、顺序、记录 ID 和清空水位，去掉所有错误均有完整堆栈的错误承诺。历史日期不表示问题已解决。
- 新独立沙盒在最初仅验证代理的阶段，原 1.3.18 字节与原客体路径的代理已取得签名心跳并确认停止，未修改应用控制策略、未安装或登录微信、未发送。旧实例的 0x800711C7 未在新实例复现，不能再据此认定所有环境都不能启动；之后已仅在该沙盒安装已核验签名的腾讯微信 4.1.8.28，并由用户手动登录。本版仍需验证正式隔离通道与实际收发。

## 1.3.18：后台控件刷新与实时日志状态（实发仍待验收）

- UIA 后台正文写入后重新读取同账号、同窗口的控件树，核对精确标题、完整正文、当前唯一发送按钮和新调用模式，不再跨写入复用旧按钮。控件变化或桌面失联时保留草稿、不提交。
- 记录后台调用的原始标量返回和异常类型，不记录异常正文；调用后未清空仍为结果未知，不重试、不改走前台。“已尝试调用”不再表述为已经发送；只读体检明确尚未验证提交有效。
- 微信体检提示随当前失败状态更新，避免卡片失败而旧成功提示仍为绿色；导出等独立通知不被覆盖。首次打开日志在布局完成后跟随最新记录，同时尊重暂停、选区和手动上滚。模型状态明确为主动连接测试状态。
- 现场为本机 wechatauto 后台 Invoke 未确认清空；已离线复现并修复控件生命周期缺口，但不能据此认定现场原因已完全解决。保留现有草稿，本轮不发送真实消息；后台实发验收通过后再开展 Fish Tank 后续改造。

## 1.3.17：停止恢复、入站与日志审查修复（正式实发仍待验收）

- 停止使发送任务代次失效；等待间隔、等待线程队列和解析目标中的旧任务不能随恢复继续发送，旧超时或取消也不能中断新一代任务。桥层在查询联系人、等待发送锁及沙盒 RPC 锁后继续核验，重连换客户端也不能放行旧任务。已关闭传输不再重建执行器；原生发送子进程在慢准备完成后复核代次。
- 原生入站回调绑定实际桥和监听代次，停止、换桥及重启后的旧回调不入库；阻塞联系人查询后重新核验，经典轮询独立保留停止事件，避免旧线程复活。沙盒停止后的迟到事件不被误确认。
- 修复沙盒更新监控或重连后，监听恢复但发送仍暂停的问题。仅恢复同一运行原本允许的发送状态，等待期间发生停止、安全暂停或换通道时不恢复，迟到完成不能取消新运行。
- 关闭传输继续兼容只读媒体桥：宿主发送任务照常失效；提供取消钩子的真实发送桥照常取消，缺少发送钩子不会中断媒体任务清理。
- 日志等长重写且首尾相同时，正常轮转日志仍能检测中间的新错误；10MB 内使用分块内容指纹，追加只补新增字节，仅修改文件时间不会重复展示。更大文件保留前后锚点检测。ZIP/TXT 导出拒绝覆盖原日志及其文件别名，清空显示不删除日志。
- 沙盒准备期间阻止并发切换；运行中的同一沙盒须先停止机器人再准备，停止态准备防止新启动抢入。GUI 仅在明确就绪阶段放开启用按钮，损坏配对元数据不再因旧心跳显示为可用。
- 本轮仅做离线审查与回归，不运行参考项目 DLL，不新增真实发送授权；当前会话范围保持。Fish Tank 节流、记忆学习、插件兼容、自主笔记及真人感改造仍在后台实发验收之后。

## 1.3.16：受控协议候选与发送证据保护（尚未正式实发验收）

- 新增默认停用的「协议后台」候选设置页，保存、只读核验和启用分开；切换先停止旧队列，切换后机器人保持停止。模型、人设、记忆、原有插件与日志沿用鲸鱼娘。
- 仅接受精确账号与人工指定单个稳定目标的文字事件；核验接口所属进程、创建时间、版本、模块指纹、回环监听和唯一实际打开账号目录。参考包原版 DLL 的无鉴权全网监听不能通过核验，本版没有随包提供可直接使用的合格原生模块。
- 入站保留原始编号、账号与发送者，持久去重、入库后确认；回调有随机凭据、来源及大小限制，凭据从访问日志移除。发送前独占登记，接口返回成功仍须核对同账号、同目标、相同正文的唯一新增自身记录及服务器编号；超时、断连和结果未知不重发。
- 未执行参考包安装器、DLL、更新锁定或图片解密组件；未降级主机微信。候选接入和离线回归不能视为后台回复问题已解决。正式通道真实收发验收及合格原生提供方仍待完成，之后再推进 Fish Tank 节流、学习、自主笔记和拟人改造。
- 日志界面继续提供关键词/级别筛选、暂停滚动、复制和导出；清空显示不删除文件。插件仍按鲸鱼娘内部契约运行，未宣称支持通用 Agent 插件或直接加载 AstrBot 插件。
- 补充审查修复：停止/重启使旧回调和旧发送任务失效；恢复入站时先按固定目标过滤，避免旧会话积压堵塞新目标；回执补齐稳定基线，避免将同序列旧记录误认为新增。原版模块即使被改为回环监听也明确拒绝，目前可用模块允许集为空。
- 像素草稿检查支持 125% 缩放下的三像素插入符，仍须观察到实际闪灭才判空；仅精确识别已核验位置及完整图形的展开控件，其他贴边标点、额外点和未知装饰继续拦截。输入框形状变化拒绝旧区域；正文外全部像素一致时才复用定位。
- 修复 OCR 引擎在中文字间自动插空格的标题误判，按真实词框重组并保留实际空格；轻微词框外扩有严格边界，完整标题仍须两次独立识别一致。标题横线与左侧搜索栏同高时，需独立的窗口栏起点和上下竖线连续证据才能解歧，不能任取一条长线。
- 补齐 WinRT `TextAngle` 的词框坐标回转，避免将校正坐标直接用于原图二次裁剪；首轮标题按安全区域内全部真实字形收紧空白，不挑选目标片段。缩放变化后的真实标题及空草稿只读检查已通过，正式后台实发仍待验收。
- 只读发送体检提前检查草稿占用，返回 `draft_not_empty`，不再等生成回复后才发现已有草稿；检查不点击、不清理、不发送。
- 沙盒入站确认改为等待主机数据库写入成功；未确认消息保留，旧监听代次不确认新任务。迟到的真实发送回执仍保留确认记录，但不会恢复已经停止或超时的发送状态。
- 修复沙盒冷连接首次读取数据库超过 23 秒时，客体已连上而主机提前报超时的问题：只读连接单次等待最多 85 秒，不重投；请求仍须在原截止时间内开始，发送仍保持原有 25 秒总上限。
- 只读窗口体检单次等待 35 秒，为客体 24 秒检查及回执留出余量；事件循环继续响应，停止意图先关闭发送闸门，迟到检查不能恢复监听。
- 修复客体冷连接结束后仍沿用请求开始时刻、立即误判宿主失联的问题；只读完成后刷新空闲租约，超时判断与新请求切换串行，旧超时判定不能取消新一轮检查。请求和发送本身的截止时间不延长，不自动解除停止。
- 同一存活沙盒可安全更新代理构建：核对旧代理签名停止记录，再添加独立只读程序映射，新代理版本、构建号与新代次全部核对成功后才登记；失败保留旧配对信息和缓存，不关闭微信或自动启动机器人。
- 修复清空日志显示后，日志文件截断重写使新错误被旧偏移编号隐藏的问题。诊断 ZIP/TXT 仅在导出副本隐藏常见凭据和随机回调路径，保留异常帧，原日志不改；这不等于自动移除所有聊天或本地路径。

## 1.3.15：当前会话隔离后台通道（正式实发待验收）

- 新增「隔离后台」管理现有 Windows Sandbox，准备版本核验的受限代理并切换通道；准备不启动微信或机器人，切换先停止旧任务，需人工核对账号再启动。关闭沙盒会丢失其登录和安装状态，需重新登录、配对。
- 模型、记忆、队列、配置与控制台日志继续留在主机。仅支持沙盒当前已打开会话的文字回复；客体微信保持可见、可操作且位于客体前台，宿主沙盒窗口可以最小化。其他会话跳过，不自动切换，不回落主机发送，不发送附加图片/文件。
- 受限目录使用签名 RPC、代理 epoch、请求截止时间与独占发送消费记录；`resolve_chat_name` 在客体账号核对名称和重名。独立心跳及取消控制不被长请求阻断，事件落盘失败停止收发，失败/未知不重试。
- 修复长时间发送或定位使宿主事件轮询误判掉线、路径上级目录越界、旧请求占满队列、请求写入报错时误判未提交等问题；保留客体连续失败计数和每次发送证据。
- 2026-10-27 候选 D 在宿主沙盒最小化时，仅点击一次发送现有授权草稿；核对新增匹配自身记录 1 条及非零 `server_id`，17 次采样保持宿主前台与鼠标位置不变。此为单次候选验收，正式传输全流程实发仍待完成，不代表收件人已读或主机原生最小化发送通过。旧版本条目保留为历史记录。

## 1.3.14：发送审查与日志界面

- OCR 会话标题改为精确核对，降级沿用账号绑定的唯一窗口；完整回读正文，已有草稿或输入不一致时放弃提交。开始输入后不切换通道，点击前持久记录提交阶段。
- 保留旧二元组接口并传递本次提交证据；明确未提交记失败，部分提交或结果未知记待核实，均不自动重发。只读窗口检查不再冒充发送失败。
- 日志浏览支持关键词、级别、暂停滚动和清空显示；保留选区和滚动，原文转义。DSH 独立启动不再依赖未选用的普通 API 密钥。
- 后台能力仍待真实验收：主机个人微信最小化发送尚未通过；沙盒内部前台、宿主沙盒窗口在后台的路径继续评估，不能把离线通过当作已送达。

## 1.3.13：发送前拦截遮挡和焦点丢失

「坐标+OCR」通道在点击输入框和发送按钮前，用 WindowFromPoint 核对落点属于微信进程；点击输入框后最多等 300ms，前台进程仍不属于微信就返回 foreground_lost，正文不写剪贴板、不投递 Ctrl+V。遮挡或未知落点归属返回 blocked_by_window。请手动打开目标会话、最小化遮挡窗口并让微信保持前台。

不再调用 guia.input_text / click_send 的内部重试链：正文只粘贴一次，发送按钮只点击一次；原剪贴板文本及原始字节先完整备份，在粘贴确认或失败后恢复；句柄/文件等无法无损备份的格式会在覆盖剪贴板前拒绝输入，并提示先复制纯文本。已有草稿、窗口移动、定位/OCR 超时都明确放弃；过期的只读定位即使晚返回也不能继续输入。粘贴后及发送前复核前台与标题，按过发送按钮的异常/超时一律结果不确定、不重发。截图使用 all_screens=True；失败日志增加 point_pid、foreground_pid，不写正文。

2026-10-26 真机验收：用户授权后仅尝试向文件传输助手发送一条约定短消息。标题校验通过后，输入框定位截图报 OSError: screen grab failed，流程在点击与粘贴之前停止，发送键未执行；数据库新增匹配消息为 0。没有重试，尚未确认真实送达。最终离线专项 86 项、完整回归 2092 项通过（skipped=3），后端及 HTTP 自检均 EXIT 0。禁止热激活、自动切会话及点击会话列表/表情面板的红线保持原文。

## 1.3.12：标题行拼接修 target_not_open 假失败 + ALT 轻敲抢前台

- 修 1.3.11 的真机缺陷一：标题行被 OCR 按字形切成多段（实测 'O' 与 '西莲寺紫' 的 y 只差 1px），旧实现只取最上面一段，于是 current_chat() 返回单字符 'O'，**每次发送都被误判成 target_not_open**。现在以候选里最小的 y 为基线，收齐基线 +12px（新常量 TITLE_LINE_TOLERANCE）以内的片段按 x 升序拼成整行；同时按 x >= right_pane_left 过滤，把左侧会话列表（实测 '社招新群' 在 x=218）彻底排除，避免"侧栏里有同名会话"造成的假阳性。空片段先滤掉，拼不出内容仍返回空串（保持"读不到就当对不上"的 fail-closed 语义），title_matches / _name_matches 的容错逻辑未改。
- 修 1.3.11 的真机缺陷二：别的窗口占着前台时（实测 QQ / Chrome_WidgetWin_1），gui.bring_to_front 连试 5 次都返回 False，发送死在第一步、永远报 guia_unavailable。现在改为先做一次 ALT 轻敲（只按 VK_MENU 单击，不发 Alt+Tab、不打字符、不改任何系统设置）再 SetForegroundWindow，成功就跳过 gui.bring_to_front，不成功才回退老路径，两条都失败才报 guia_unavailable。抢前台逻辑抽成可注入函数 _alt_tap_activate，便于离线单测；结尾仍调用 gui.restore_zorder()。成功判据不只看句柄相等：前台落在微信自己的另一个顶层窗口（句柄 != main_hwnd）也算微信在前台，否则会白等一整个超时；老路径 bring_to_front 也失败时再补敲一次 ALT（仍只按 VK_MENU），两条路径都失败才报 guia_unavailable 并提示用户手动点一下微信窗口。前台**已经是微信**时（前台句柄等于主窗，或前台窗口属于微信进程——实测前台常是微信自己的 Qt51514QWindowIcon 顶层窗口）先只读看一眼就直接返回，不再多余地敲 ALT、也不再调 bring_to_front。
- 抢不到前台时失败能自解释：红字改为「微信无法切到前台（当前前台是 <窗口类名>），请手动点一下微信窗口后再重试」（类名取不到时退回原文案），失败日志新增 `foreground=<窗口类名>:<pid>`（只记类名与 pid，不记窗口标题），便于判断是不是被别的置顶窗口挡着；原因码仍是 guia_unavailable，仍然 fail-closed，绝不在键盘焦点不在微信时粘贴。
- 仍未真机发送任何消息：本轮全部验证都是只读探测（标题栏 OCR / 可用性探测）与 mock 单测；未调用 open_chat、未点击会话列表、未做 hot_activate、未新增任何红线 API。
- 测试：tests/test_wechat_guia_send.py 增至 48 个用例（新增 GuiaTitleLineTest 标题行拼装 7 例、GuiaForegroundTest 抢前台 16 例），tests/test_send_phase_logging.py 15 例一条未弱化。

## 1.3.11：发送通道恢复（坐标+OCR）+ 日志可复制导出 + 发送阶段可追溯

- 发送通道恢复：微信 4.1.15.13 恢复窗口后 UIA 树只剩 3 个节点（Qt51514QWindowIcon×2、MMUIRenderSubWindowHW×1），没有任何 mmui:: 控件，原有"UIA 取 mmui::ChatInputField 发送"在 4.1.x 上必然失败、控制台报 send_failed；Win32 PostMessage 直投文本实测无效（窗口差异 0.0205 小于空转噪声 0.0272），已排除。按用户 2026-10-26 在会话中的明确授权，接入 wechatauto/guia.py 的"坐标+OCR"通道：截图 → WinRT/guia OCR 定位输入框与发送按钮 → 真实鼠标点击 → 剪贴板粘贴；UIA 不可用时自动降级，失败原因码为 guia_unavailable / target_not_open / minimized / send_failed / invalid_target。
- 只发当前已打开的会话：发送前用标题栏 OCR 校验会话名，不符即明确失败（target_not_open）并提示用户手动点开目标会话；程序绝不自动切换会话（点会话行/搜索切换/open_chat），绝不 hot_activate（写微信进程内存），不点会话列表、不点表情面板。按下发送键后不再换通道，避免重复发送；单线程串行闸门、发送间隔、超时且不重发、连续失败重连全部保留。
- 日志可复制/导出：报错条可鼠标选中复制，旁边新增「复制报错」（复制内容带时间戳前缀，复制后提示"已复制报错"，报错条本身不被覆盖）；运行记录卡新增「复制全部」「导出 TXT」「导出诊断包」「打开所在目录」，导出 TXT 用 UTF-8 带 BOM 写盘（默认名 运行记录-YYYYMMDD-HHMM.txt），写失败红字说明原因而不是静默。
- 导出诊断包用标准库 zipfile 打 zip（默认名 诊断包-YYYYMMDD-HHMM.zip）：内含 report.txt（报错条原文 + 运行记录全文 + APP_VERSION + 系统/Python 版本/是否管理员 + 实际打包清单）、logs/ 下全部 .log 及轮换文件、send-worker-stacks.log、startup-diagnostics.json / startup.log / crash.log，以及 wechatauto_logs/ 下的日志；文件缺失或超出 32MB 只跳过不报错，导出后提示保存路径并可一键打开所在目录。
- 统一日志内核 src/logging_setup.py：root logger 一个 RotatingFileHandler 写 logs/backend.log（沿用 5MB×5 与 "%(asctime)s [%(threadName)s] %(levelname)s %(message)s"，级别由 WECHAT_LOG_LEVEL 决定，默认 INFO），重复初始化幂等（同一目标文件只留一个 handler，换目录摘掉旧 handler）；httpx/httpcore/urllib3/requests/asyncio 压到 WARNING，避免把真报错冲掉。安装 sys.excepthook 与 threading.excepthook 把未捕获异常连完整 traceback 落盘（链式调用启动器原有钩子，钩子自身 try/except BaseException）；src/runtime_logs.py 改为统一日志门面，不再自成一套 handler。运行记录每 5 秒从 backend.log 尾部（首次最多 200KB）增量读 ERROR/CRITICAL 块（带 traceback），记住已读偏移不重复追加，界面刷新后自动重放。
- 发送阶段可追溯：单行机器可读格式 `send phase=<码> who=<会话名前8字+…> len=<正文字符数> reason=<原因码> channel=uia|guia pid=<进程号>`，十一个码（locate_window / check_chat / input_text / press_send / verify_watermark / guia_probe / guia_check_title / guia_input / guia_click_send / guia_verify / fallback_switch）；**正文一个字都不落盘**，只留长度；会话标识与观察到的窗口标题都只取前 8 个字符加省略号，不写 wxid 全量、不写 token/URL/key。失败一律 logger.error 一行可读摘要（`send failed channel=… reason=… who=… len=… title=… hint=… pid=…`，带处理建议，例如 reason=target_not_open hint=请先手动点开目标会话），能被控制台运行记录的 extract_error_blocks 提取成红字；异常路径改走 record_exception() 留完整 traceback。UIA 阶段（window/conversation/input_control/focus/input/readback/send_key/confirmation）折叠为五个码且连续重复只写一行，一次成功发送正好 5 行。
- 实现落点：src/wechat_guia_send.py 让「坐标+OCR」通道按 guia_probe → guia_check_title → guia_input → guia_click_send → guia_verify 落盘，失败由 send() 统一补一条 ERROR；src/wechat_send_worker.py 子进程按 job['log_dir'] 调 setup_logging()，父子进程写同一份 logs/backend.log，不换道时的最终失败写 ERROR；src/wechat_sender.py 的 _prepare_job 把父进程实际在用的日志目录写进 job['log_dir']，发送子进程超时（subprocess_timeout）与异常退出（subprocess_exit）由父进程补写失败记录；src/diagnostics_bundle.py 负责 zip 打包、BOM 写文件与系统信息；src/logging_setup.py 另提供 mask_chat_name()、send_phase_line()、send_failure_line()、log_send_phase()、log_send_failure()、backend_log_path()/backend_log_dir()。
- 顺带修掉一个会吞掉所有发送日志的真缺陷：wechatauto/logger.py:56 导入驱动时直接 root_logger.handlers.clear() 并把 root 降到 DEBUG，统一日志内核在发送子进程里被整个摘掉（阶段行只落到控制台，backend.log 里什么也没有）；logging_setup 新增 restore_backend_handler()，log_send_phase()/log_send_failure() 写前自查，handler 不在就按原目标与级别重挂回去。写日志抛异常一律吞掉，绝不影响发送结果。
- 新增 tests/test_send_phase_logging.py（阶段序列、连续重复码只写一行、正文不入日志、观察到的窗口标题被掩码、三种原因码各产出一条可提取 ERROR、UIA 不换道失败、日志 logger 抛异常不影响结果、log_dir 传递链与老任务回退）、tests/test_logging_setup.py、tests/test_diagnostics_bundle.py、tests/test_gui_log_tools.py；APP_VERSION 1.3.8 → 1.3.11，仍只有 src/runtime_identity.py 一处硬编码。
- 红线不变：SendToLabel / ForwardToLabel / AddLabelMembers / ForwardMessage / ForwardVoiceMessage / SwitchToMoments / Moment* / Poke / VoiceCall / RecallLastMessage / RecallGuard / AtAll 一律不调用，不内置主动外发；例外只放宽"不截图 OCR"半句，之外需要的任何放宽仍要先停下来问用户。本轮未真机发送任何微信消息（全部 mock / FakeGUI 驱动），未运行 build_installer.ps1 或 tools/make_release.ps1。

## 1.3.8：工作台文件语音、DSH与旧功能恢复

1.3.8验收补充：控制台“测试AI”跟随当前启用引擎，DSH启用时实际测试独立DSH会话；接口连通不代表模型回答成功。已知AUTH/QUOTA/限流错误分别提示，不透传上游敏感原文。2026-10-26真实本机诊断发现miku/deepseek-v4.1-flash套餐额度耗尽；官方默认提供商另报认证失败。因此本轮DSH协议/专用人设已通过，只读连接与失败诊断可复现，不能将真实问答标成通过。未改变正式凭据或套餐。合成中文语音的本地base模型转写通过（约2秒）；新MCP v2实际独立进程完成档案查询、学习重建/审阅权限检查和撤销后拒绝。

Agent新增“文件 / 语音”：先在本机上传/预览，再选最多3份随本轮提问。支持txt/md/csv/json、文字PDF、docx与常见音频。单文件8MB，提取最多16000字；本轮附件引用总预算8000字，每主人最多50份/64MB，满后明确提示手动删除。解析不执行宏、脚本或外链；扫描/加密PDF明确报错。原件与索引按主人隔离，附带资料不作为操作指令、不会自动学为主人偏好。语音复用本地插件，工作台只用已安装的所选模型，不隐式下载模型或上传原音频；转写文字在用户点击发送后交给已选API/DSH。没有实现麦克风录制、语音合成或微信语音外发。

修复语音模型误选、依赖补丁导入、auto语言与转换缓存目录；DSH在锁等待、探测、建会话和提交前复核权限，权限不同不复用同一服务端历史。工作台收据包含权限上下文，低权限不能重放高权限的读文件结果。修复历史/卡片/设置方案迟到响应覆盖新会话，DSH表单编辑不被旧探测响应覆盖。

本地Agent MCP v2增加聊天原文搜索、学习库概览/查询和审阅工具，DeepSeek Harness可通过可撤销凭据整理本地学习内容。只读与写记忆权限独立；始终经宿主API，不开放SQL或原始数据库任意改写。导出新版本脚本和示例，不覆盖已有人工修改的连接器，不擅自修改DSH原配置。

安装包恢复全部32个受支持插件，沿用启用/禁用与已移除设置；退役风险插件仍不加载。修复设置页从旧后台状态刷新前台兼容设置时信号互相覆盖，覆盖验收新增“保存的旧关键词规则实际执行”检查。微信最小化/缺控件在严格后台仍等待，兼容前台与严格后台不能同时执行；本轮没有声称已解决微信后台实发或最小化自动发送。

## 1.3.7：实例隔离与微信失败诊断

按用户提供的 Cockpit Tools 1.3.74 源码参考实例目录归属与请求生命周期，采用本项目原生实现：新 DSH 会话按端点、主人绑定、账号、会话及人设的 scope 使用独立工作目录；旧共享目录和历史保留。接入探测、等待锁及停止旧任务后复核管理员绑定版本，换绑期间的旧设置不落盘。

修复发送失败仍沿用“窗口已确认/ready=true”的假就绪状态。失败统一标记 ready=false，保留具体等待原因与不确定提交状态；回读失败、会话变化保留草稿且不重发。严格后台仍只用现有控件能力，不切前台、不点击会话列表；没有控件时等待，不能靠兼容前台开关强制获得后台能力。

后台参考网站所链原仓库当前404；可读公开分支的发送路径包含 OCR 认行、坐标点击消息、SetForegroundWindow、keybd_event 和失败二次发送，不能作为本项目“不抢前台”的实现移植。参考文件仅静态阅读，未运行第三方安装、账号切换或凭据注入代码。离线通过不等于真实微信送达通过。

## 1.3.6

- DSH桌面自动发现只读进程树和现有TCP监听端口，再核验RPC协议。实际本机找到DeepSeek Harness PID17132 / 127.0.0.1:5947，排除一个无关子进程端口；未调用模型或微信发送。多端点需选择，核验专用人设后可保存并启用，管理员凭据页新增真实DSH入口。
- 大肥鱼以当前人设、第一人称根据本地当天记录写日记，最多30条/8000字，最多600输出token。SQLite与可读md保存，证据单独记录，旧统计摘要保留但不冒充日记。每日期/人设只提交一次，失败或未知不自动重试，自动写已结束日期可关闭。
- 真实本机DSH用合成对话完成两轮日记检查；第一轮发现补造细节，收紧为少记录写短日记、不增加人物动作或对话，第二轮5～10秒完成合乎来源的第一人称正文。敏感轮不纳入日记，群成员与主人来源分开，安静两分钟再自动开始。
- 再次封禁工作区中出现的热激活进程入口；旧配置、旧任务和直接调用都不能放行。前台兼容/严格后台说明区分焦点策略，不承诺生成微信控件。发布排除备份源码，避免混入历史实现。
- 完整回归发现未点名图片虽然视觉跳过，OCR/收藏仍可能下载；统一拦截_picture_text_only，沿用文字响应策略时本地确认、不调用模型。新增等待完整流水线结束的回归，避免旧50ms断言受调度快慢影响。
- 包含尚未交付的1.3.5原文召回、预算、DSH新引用和重启配置保留优化。本次微信发送仍未真实验收，DSH已连接不等于发送端可用。
- 2026-10-26最终源码验收1946项零失败、2项环境跳过；后端/HTTP、图片旁路30项、DSH与日记专项通过。真实本机DSH自动发现和合成日记回合通过，原用户配置与微信收发未被测试修改。冻结、覆盖升级与交付哈希见发布/1.3.6/验收报告.md。

## 1.3.5

- Agent在最近10轮之外，从同主人、同会话、当前人设的本地原文检索少量相关旧问答，带时间与证据引用；排除近期重复、命令回执和已明确忘记的陈述。不再无条件注入最新无关记忆。
- 最近轮数、历史与引用总字符预算、旧记录条数可在主人工作区配置；原文保留，模型不额外调用检索模型。记忆列表支持搜索与总条数提示，聊天完成显示实际使用量。
- 修复DSH复用会话时本轮新检索记忆被遗漏：新引用独立传递，旧历史仍只在首轮播种，不重复上传整库。
- 修复运行时配置段过滤遗漏agent/dsh，导致上下文设置、主人通道和DSH配置在重启后失效；真实Qt→HTTP保存并重启后台验证保留，构造运行时不自动连接。
- 笔记异步读取/保存增加请求代次，旧回调不能覆盖新文件或草稿，读取失败不复用旧正文和版本。移除退役自动打开会话的误导提示。
- 参考QQ版unified_memory/console_agent的相关性与最小注入思路，按微信权限契约适配。实际微信收发不参与本轮测试；发布验收见发布/1.3.5/验收报告.md。
- 2026-10-26源码验收：完整回归1925项零失败、2项环境条件跳过；后端/HTTP自检通过。实际Qt→本机HTTP保存预算/笔记并重启后台核对通过，独立MCP进程本机往返通过；冻结及覆盖升级证据随发布交付。

## 1.3.4

- 修正微信输入框Ctrl+Win辅助提示被当群名；生产发送不再恢复最小化窗口，等待状态贯穿模型队列。
- Agent单栏人设聊天、独立主人工作区笔记、跨端受限MCP连接器、显式插件工具接口与故障隔离。
- 原文SQLite持久化、带证据的明确偏好学习、去重/忘记/过期引用/日记；参考fish-tank分层设计，未执行第三方安装代码。
- 最近完整问答保留、短确认承接、可配置动态温度、白名单稳定ID防同名冒用。原配置与历史覆盖保留，账号安全暂停不解除。
- 凭据撤销/过期在模型等待、插件读写与返回结果时重新检查，未知操作不重试；主人指令优先于普通回复插件。人工记忆不被学习改为过期，已过期学习不占有效记忆名额。
- 修复工作台请求失败反复创建会话、对话插入历史顶部、笔记读取失败按钮锁住、主人通道保存失败显示错误勾选。升级夹具校验Agent原文、执行收据、学习证据及工作区笔记。
- 2026-10-26源码验收：后端/HTTP自检通过，完整unittest 1908项零失败、2项环境条件跳过；独立stdio MCP进程经真实本机HTTP完成笔记读写、记忆共享、只读拒写和撤销验证。冻结/安装升级结果与实测边界见发布/1.3.4/验收报告.md。

# 修复日志与残存问题（给后续 agent / 开发者）

## 1.3.3：账号安全事件收紧（2026-10-25）

- 用户报告三次强制下线、社交功能临时限制；先确认并停止本机1.3.2机器人，关闭旧兼容唤醒/自动打开会话/主动发送，写入持久暂停，不继续登录或真实发送测试。未将限制原因断言为单一动作。
- 审计发现旧compat路径保存并放行第三方库的进程无障碍字节写入；删除保存/恢复/调用入口，旧函数恒返回false。检查不再构造兼容唤醒驱动。修复此前“禁止进程修改”约束被重新放宽的问题。
- 微信团队、系统通知和gh_公众号不再自动回复；在入站和最终发送两端拦截，防止旧队列/插件路径漏出。收到微信团队安全提醒立即停止、取消在途生成和待发送队列，暂停持久化，阻止重新启动/连接/监听/窗口检查/媒体读取与自动重连。
- 新增账号安全页与仅本机人工确认的解除入口；解除不自动登录或启动、不补发积压，不代表微信限制已解除。自动搜索/切会话停用，旧配置不能继续执行。
- 旧测试保留，原本期待“兼容唤醒成功”的一项改为验证绝不调用回调，并增加伪造旧回调/官方通知/安全卡片/暂停跨重启/在途取消/凭据越权回归。这是修正危险行为预期，不以测试替代实际安全保证。

## 1.3.2：原生图片对话中心与发送模式分离（2026-10-24）

- 2026-10-25按qq-bridge a49877f4e58d04e47532d44b803360d0f001160d的协议分层，新增默认关闭的DSH原生对话桥：会话映射、斜杠RPC、浏览器会话鉴权、remote.mux完整回合收集、失败清队列/取消且不重问。专用零工具人设、按账号/群私聊/人设隔离及20轮活动上下文轮换；原聊天归档保留。接入设置与安装入口放在Agent页，绑定管理员后配置。
- 本机DSH协议检测和专用零工具人设的真实问答通过；合成两轮记忆与另一群隔离通过，不重启既有DSH/QQ，不上传私聊历史。提供商错误会出现AUTH，新增provider/model/思考强度及只读导入qq-bridge接入参数，避免默认官方提供商误用。真实微信后台未验收。ctw07参考仓库当前提交0482b258966ea0449e2fdc4cd3d9bbb0ccbfe685仅公开README/LICENSE，没有可核验的发送实现，不伪称已移植。

- 核查QingchenWait/astrbot_plugin_smart_imagechat_hub v2.8.6及青尘插件源，采用微信原生适配，不运行AstrBot插件或订阅源代码。新image_chat_hub将入库、标注、检索和发送解耦；标签包括主体/情绪/动作/语境/文字，人工标签独立保存。
- 新图自动额外AI标注默认关闭，可在表情包管理启用；旧库补全每批最多20张、活动队列100张。SQLite持久任务、逐图故障跳过、取消/重启未知请求不自动重做；独立识图客户端共享用量，避免图库阻塞聊天模型锁。旧说明本地生成标签，聊天识图直接回填，不重复调用模型。
- 修复识图早于收集入库时说明丢失；有界缓存只在同内容实际入库后回填，不绕过收集名单。识图期间人工编辑、删除、关闭插件或修改模型设置时不保存旧结果；人工标签不会被AI覆盖。
- 模型选图先做本地相关检索，最多12候选、候选JSON不超过1200字符；未知含义的自动哈希图片不参与推荐。手动图库、显式标签命令、每轮一张和严格后台跳过图片的行为保留。
- 修复隐藏昵称单独启用/聊天控制禁用时，其他昵称和零宽字符前缀仍漏出@；在空格整理前清理，保留昵称与正文边界，避免误删正文首词。固定关键词字面原文和正文邮件不改。
- 用户确认“草稿未发”发生在兼容开启、严格后台关闭。明确前台模式不再先尝试后台Invoke；旧后台尝试失败仍不能前台补发。切模式先停止旧任务，不以此宣称后台已解决。
- 经用户明确授权新增background_submit=posted_enter实验选项，默认invoke，需本机明确选择。只向正向核对账号的当前窗口投递一次回车按下/抬起，不移动真实鼠标、不调用置前/全局键态/坐标或自动切聊天；不先Invoke、不补发。缺少控件或账号不能核实时等待。仅离线验证，真实微信后台发送尚未验收。
- OCR明显乱码不进入模型；Agent的问候/身份/只读状态直接显示回答，不再生成必须应用的状态占位卡。真实语音未提供音频样本，转写能力不冒充已实测通过。
- 图片中心提供自动开关、补全/失败重试/取消/进度、人工标签与AI标签。来源与已适配范围见docs/智能图片对话原生适配-20261024.md。

## 1.2.99：不确定后台提交禁止转前台，点名与对话框修复（2026-10-24）

- 兼容模式中后台写入失败或异常后直接结束本次任务，禁止转入前台键盘发送；防止抢焦点及重复提交。严格后台仍在驱动加载前分流，不唤醒控件、不恢复窗口、不自动切聊天。
- 后台Invoke只调用一次，确认窗口从0.5秒延长到最多2秒，随时确认即返回；校验同一聊天，草稿变化、控件异常与未清空明确结果待核实、不重发。诊断记录submission_state，控件可读不等于微信送达。
- 切换发送模式前收掉旧发送任务再应用设置，避免旧子进程继续使用前台模式；仅改模型/API仍保持原连接。修正严格后台提示误导用户开启互斥兼容开关的问题，设置页说明前台模式可能抢焦点。
- 修复传输层在“不要昵称/@”开启后仍默认补@提问者；宿主最终检查正文/每段与mentions元数据，固定关键词原文保留。开关关闭时保持旧行为。
- 定时任务及管理员绑定对话框显式绘制不透明浅色背景，修复主界面透明样式下黑底。
- 核查Eifptaur参考项目当前实现：后台窗口消息仍涉及坐标点击及前台兜底，不直接移植。独立电脑/虚拟机可隔离使用者桌面，但不是微信后台能力修复，也不能保证锁屏/多会话自动发送；完整方案见docs/微信不抢焦点方案-20261024.md。自动表情说明补全、共享记忆与DSH后续工作仍待继续，不能当作本版已完成。

## 1.2.98：管理员绑定与Agent控制能力底座（2026-10-24）

- 对照QQ 5.2.16控制台Agent、统一记忆和DSH接口，并核对K0nd1us/QQ-agent 0.4的扩展类型/能力依赖/生命周期设计。保留原微信基础页面与被动回复链，新增显式登记的结构化控制能力，复用原控制台校验与锁，不接收任意HTTP路径。
- Agent修改前必须绑定单一管理员，按平台/账号/用户稳定标识归属；旧方案卡的应用/撤销、技能与任务写操作同样校验绑定。提供GUI绑定/解绑、已有微信管理员预填和损坏记录显式恢复，不凭昵称猜测跨端身份。
- bridge握手与执行接口支持有范围、30天默认有效期的Bearer凭据；只保存摘要，换绑/解绑/撤销令旧凭据失效。带bridge凭据不能直接调用其他管理接口。接口仍仅本机访问，不是操作系统级的本地进程隔离。
- 写操作先持久化request_id与执行中状态，再调用原接口；重复请求返回已有结果，不重复执行。取消或断电后结果未知的操作拒绝自动重做。审计不保存请求正文或密钥，状态查询剔除聊天原文。
- 不提供直接发消息/主动群发/管理员自授权/键鼠面板能力，不允许Agent放宽严格后台模式。修复旧Agent人设切换未持久化的问题。
- 本阶段是可调用的控制底座；外部DSH、QQ服务未运行时不能标成实连，共享记忆、日记、扩展兼容和自然语言规划将分阶段继续。

## 1.2.97：宿主插件故障隔离与修复重试（2026-10-24）

- 检查QQ侧钩子容错实现后按微信契约扩展：由宿主保护导入、元数据、构造、post_load、配置归并和插件列表；坏插件与重复ID不能拖垮启动或覆盖其他插件。未安装的可选市场插件不误报故障。
- 运行钩子独立捕获普通异常与插件SystemExit，故障插件自动隔离；失败的消息、对话和回复元数据改动不提交，健康插件继续执行。准备/后处理/tick/on_ready增加异步等待上限，正常停止取消不记故障；保持上一版视觉与OCR并行读取。
- 模型请求抛错向运行时传播，不把转发模型调用的聊天控制插件误判为损坏；保留重生成失败后使用原回复的行为。对话事务提交保留当前消息对象，确保后续识图注释仍进入模型输入。
- 故障按ID、阶段、异常类别、时间和版本/文件指纹保存在plugin_faults.json，避免重启反复执行同一坏版本，不记录异常正文。保留插件源码、原配置和聊天记录；修复文件、更新程序或停止后手动重试可重新加载。
- 插件中心显示“已隔离”及故障阶段，提供“重试故障插件”。运行中拒绝重载；权限/点名/群名单等保护组件故障时暂停外发，不以跳过方式放宽权限。
- 源码与冻结自检新增真正加载SystemExit损坏插件、跳过和恢复验证；新增故障注入、消息回滚、HTTP恢复和Qt故障卡片测试。它是普通插件故障隔离，不是恶意代码/原生崩溃的进程沙箱。

## 1.2.96：接管交接包，修复分气泡与识图链路（2026-10-24）

- 先核对 DeepSeek 1.2.82～1.2.95 与 DSH 微信交接包，保留12MB统一限制、未点名图片不下载、原因码及插件恢复等改动。
- 修复 chat_controls 生成分段后又被极速模式及最终发送合并的问题。仅本地后处理授权分段，模型 PACE 不执行；纯正文归档，每段重查运行/策略/人设，媒体仅首段，部分失败记 uncertain、不重发。
- 独立识图地址补全、按目标协议构造图片/鉴权/响应解析，计入用量且不改文字模型配置。视觉成功后慢 OCR 最多额外等1秒；下载/抽帧不占文字模型锁。缓存绑定账号/会话/模型/清晰度/抽帧参数，切号及关闭后的旧下载不回填。
- 未转写纯语音使用事实性本地说明，不调用文字模型责怪用户或编造内容。有文字问题/成功转写仍正常处理。
- 修复发布脚本名为白名单实为整仓黑名单遍历的问题：明确源码目录/根文件白名单，配置用默认模板，不打包真实配置或临时市场目录。同步六份DIY说明。
- 真实接口复验：当前保存 TM ·Auto 两张生成测试图均识别成功（10/10核对点），约16.4s/5.0s；原图与储藏区副本一致。文字极短问答3次仅1次成功（7.2s），两次超时，不能承诺4秒。未向真实微信联系人发送测试消息。
- 源码、冻结、升级与交付验证结果以本轮验收记录为准，不把模拟通过记成真实微信实发通过。

## 1.2.95：原因码上界面 + 文档（交接指南第 5 节收尾）（2026-10-23）

- 控制台「微信接入」卡片标题在未就绪时直接带码：`读消息在线 · 发送未就绪（minimized）`，截图/反馈里一眼能看出是"窗口最小化"还是"没开放输入控件"。
- 「测试微信」输出新增一行 `发送原因码：<code>（说明）`。
- `微信版使用说明.md` 增加**原因码对照表**（minimized / accessibility_unavailable / chat_not_open / input_unavailable / draft / 空），每条都写了"你要做什么"。
- 新增 `tests/test_send_code_surface.py`（3 条）：界面取 `send_code`、接口输出含"发送原因码："、文档覆盖全部码值。
- 未做：账号目录与数据库绑定的取证、准备预算切分 —— 都需要 02 模板的信息或真机时间线。

## 1.2.94：发送未就绪给出原因码（交接指南第 5 节）（2026-10-23）

- `bridge.status()` 原来只有 `send_state`（ready/waiting/unchecked），**没有原因码**：报告和界面都说不清"为什么没就绪"，只能去翻长文本。
- 新增 `send_code`（`accessibility_unavailable` / `minimized` / `chat_not_open` / `input_unavailable` / `draft` / 空）与 `send_reason`（原始说明），与 1.2.66 的 `send_channel_hint()` 文案互补：一个给机器读、一个给人读。
- 新增 `tests/test_send_state_code.py`（3 条）：waiting 带 code、ready 为空、unchecked 为空。
- 踩坑：手搓假桥对象会不断缺属性（connected_at / last_message_at / last_send_at…），改用**真实 `WeChatBridge(tempdir)` 只覆盖字段**，测试才稳定。
- 未做：控制台把 code 显示出来（下一批）、账号目录/数据库绑定的取证（需 02 模板信息）。

## 1.2.93：识图分段计时（交接指南 3.2 第一步）（2026-10-23）

- 指南要求"下载、持锁、慢 OCR、视觉、聊天分别测量，不能笼统写识图慢"。此前只有一句"图片识别成功：耗时 X 秒"，无法区分是取图慢还是模型慢。
- 新增：`describe()` 里给取图/落盘/抽帧单独计时，完成后写 `取图/抽帧阶段耗时 X.XX 秒（N 个画面待识别）`；视觉耗时沿用原有的"图片识别成功：…耗时 X 秒"。
- 未点名群图被跳过时不产生任何计时噪音（测试断言）。
- 新增 `tests/test_vision_stage_timing.py`（2 条）。
- 仍未做：`_work` 的准备总预算(90s)分摊到各阶段的具体改造 —— 需要真机时间线数据来决定预算怎么切，避免凭空调大超时（指南明确禁止）。

## 1.2.92：未点名群图改为"下载前跳过"（交接指南 3.3）（2026-10-23）

- 复核证实指南的判断：`_picture_text_only` 的检查原本位于**取图、保存、抽帧之后**，也就是"先把图下下来，再决定不识别"——白耗流量与磁盘，还占用 AI 锁。
- 修法：把检查移到 `describe()` 的第一行，**任何 `_acquire_source` / `download_media` / 落盘之前**就返回，并写入 `vision_status='略过：群聊未点名的图片不做识图'`。
- 没有放大的东西：未点名的群消息仍然**不会**被加进"必回"的触发条件，只是把"已放行但只回文字"这条路径的代价降到 0。
- 新增 `tests/test_text_only_skip_early.py`（2 条）：未点名群图既不取图也不调 `download_media`、不产生 `data/received_images`；被点名的图照常进入取图流程。
- 下一步：3.2（准备预算与分段计时，需要真机时间线）、3.5（用色块图验证当前接口视觉能力，需要你的密钥/授权）。

## 1.2.91：图片上限统一到 12MB（交接指南 3.1 复现并修复）（2026-10-23）

- 交接包《01-微信端10小时修复指南》3.1 的复现项已验证：`image_understanding.MAX_IMAGE_BYTES` 是 12MB，但储藏区依赖的 `sticker_library.MAX_BYTES` 还是 **6MB** —— 6~12MB 的图"取图阶段接受、保存阶段拒绝"，用户看到的是"识别失败"。
- 修法：`sticker_library.MAX_BYTES` 提到 12MB 并在注释里写明为什么必须与识图一致；`image_understanding` 直接 `from ..sticker_library import MAX_BYTES as MAX_IMAGE_BYTES`，**只保留一个来源**，以后不会再漂移。
- 保留的边界：字节上限 12MB、像素上限 16MP 都还在（不是把校验删掉换性能）。
- 新增 `tests/test_image_size_limits.py`（4 条）：三处常量一致、7MB 合法 PNG 能被 `inspect_image` 接受（旧代码会抛"图片超过 6MB"）、超字节仍拒绝、小字节超大像素仍拒绝。
- 下一步（本指南）：3.2 分段计时与准备预算、3.3 未点名图片在下载前跳过、3.5 用色块图验证当前模型视觉能力；引用图反查仍标为未支持。

## 1.2.90：定时任务面板（P1-2 UI）（2026-10-23）

- Agent 页右侧新增「定时任务」按钮，打开 `ScheduleDialog`：列表显示 名称/动作/间隔/启用状态 + 上次结果；支持新建（连接检查、每日简报）、立即运行、启用停用、删除。
- 对话框标题与首行说明都写明「只写本地报告，不发微信」；`tests/test_schedule_dialog.py` 静态检查它只调 `agent/schedules*` 接口、没有 `queue_outgoing/send_text/wechatauto/proactive`。
- 新增 5 条测试。
- 未做：技能面板 UI、Agent 学习闭环。

## 1.2.89：学习页新增「整理记忆」按钮（2026-10-23）

- 「对话与记忆 → 本地学习库」工具行新增 **整理记忆**：弹确认框写明会做哪三件事（合并近重复、清理 7 天前被替换、清理 30 天前单证据候选），确认后调 `POST /api/dialogue/memory/maintain`，结果写回提示条并自动刷新列表。
- 为什么加确认框：这是**破坏性**操作（会删候选与旧记忆），一句话说明白比默默执行安全。
- 新增 1 条测试：接口路径与实现模块存在于 `dialogue_management`（防止按钮指向一个 404）。
- 未做（下一批）：技能面板 UI、定时任务面板 UI、Agent 的"这次对了/不对"学习闭环。

## 1.2.88：群里回复仍带 @昵称——发送前加最后一道闸（2026-10-23）

- 用户截图：机器人回复开头仍是「@戏络 哟，这都怪到本鲸头上…」。但直接调 `chat_controls.on_post_reply` 传同一句话，`@` 是能被去掉的（实测 7 种分隔符都正常）。
- 结论：**插件层的整形只覆盖模型那条路**，从关键词/本地插件/立即回复等路径出来的文字没有经过 `run_on_post_reply`，于是带着 @ 直接发出去。
- 修法：在真正发送前（`_send_with_pacing` 调用点）再兜一次 `strip_at_mentions`，条件是 `chat_controls.noAtInReply` 为真。这样**任何来源**的回复都不会带开头 @昵称。
- 新增 2 条测试：去 @ 幂等、发送调用点上方 700 字符内确实存在 `strip_at_mentions`（防止以后重构把这道闸删掉）。
- 说明：`noAtInReply` 默认 True；用户配置里只有 `replyProbability:30`，其余走默认值。

## 1.2.87：引用回复（appmsg type=57）正文不再丢成 [文件/链接]（2026-10-23）

- 用户截图证据：一条"引用图片再提问"的消息，原始 XML 是 `<title>我再考考你 这个是谁</title>` + `<refermsg>`（被引用的是图片），旧解析把它归成"文件/链接/卡片"，结果 `text="[文件/链接]"` —— **正文和被引用的图全丢了**，模型只能瞎猜。
- 新增 `_refer_reply(content)`：抽 `title`（正文）、`refermsg` 里的被引用内容与类型（image/video/emotion/text）、`displayname`。被引用内容先 `html.unescape` 再判断类型，避免把 `<type>3` 当成正文。
- `message_from_raw`：有 `<refermsg>` 时正文改用 `title`、不产出 `[文件/链接]` 占位，并把解析结果挂到 `message['refer']`（供后续识图/展示使用，不混进正文）。
- 新增 `tests/test_refer_reply.py`（5 条）：引用图片保留正文并标记 image、引用文字抽出 quoted、普通消息无 refer、消息级 `text` 不再是占位符。
- 未做（下一步）：用 `refer` 里被引用图的 `svrid/local_id` 去把原图找回来并送识图（需要按会话+时间反查本地库）；目前只是"正文不再丢失"，被引用的图还不会自动识别。

## 1.2.86：修回归——机器人不发表情了（配图链路被分层划走）（2026-10-23）

- 现象：用户升级后"他也不会发表情了"。
- 根因：**是我上一批分层造成的回归**。1.2.75 把 `sticker_admin` / `sticker_memory` / `sticker_cadence` 划进市场包，冻结版只带基础插件 → 这三个根本不进安装包，配图功能凭空消失（用户之前一直开着）。
- 修法：三件套全部升回基础插件（配图是一个完整链路：管理界面 → 记住含义 → 按节奏发）。现在 **24 基础 / 8 市场**。
- 加了防回归测试 `test_sticker_chain_is_built_in`：`sticker_admin/sticker_memory/sticker_cadence/image_library` 任何一个被划出基础清单，测试立刻红。
- 文档 `docs/插件分层清单-1.3.0.md` 同步更新，并写明这次调整的原因。
- 教训：**用户正在用的功能不能因为"看起来可裁剪"就划走**；分层前应该先看用户配置里哪些是开着的。

## 1.2.85：群名解析不出来时不再白算一遍回复（2026-10-23）

- 用户截图现象：某群消息显示 `待核实`，错误是 `ValueError: 未解析出当前账号的目标会话名称，本次未操作微信`。即：模型**已经生成**了回复，发送阶段才发现群名查不到，于是整条作废。
- 新增 `bridge.resolve_target_name(target)`：入口处先判断"这个名字能不能搜得到"。
- `wechat_app` 在群聊触发判定前加一道预检：解析不出显示名 → 直接记为未回复并写明原因（"在微信里给该群设置可见名称后再试"），**不再调用模型**，省一次 token，也不会出现"待核实"。
- 豁免：`bridge` 未连接 / 读不到数据库 / 后端是 mock 时返回 `"unknown"`，视为"不检查"——否则模拟模式与单元测试里所有群都会被误拦（这一点先写进了代码注释与测试）。
- 验证：`tests.test_wechat_app_flow` 25 条通过（含群聊链路）。
- 未解决：`5640372287@chatroom` 这种**微信侧本来就没有群名**的群，除了让用户在微信里设置名称，没有别的手段——UI 自动化只能按名字搜索会话。

## 1.2.84：语音转不出来（av 版本冲突）+ 只按逗号说话时不拆条（2026-10-23）

- **语音识别全挂**：日志 `语音转写失败（open() got an unexpected keyword argument 'metadata_errors'）` —— faster-whisper 内部调用 `av.open(..., metadata_errors="ignore")`，而当前 `av` 已移除该参数。新增 `_patch_av_open()` 包一层 `av.open` 丢掉该关键字（不改第三方包文件），在 `ASRService` 载入模型前调用。实测 `decode_audio` 从"抛 TypeError"变成正常解出 16000 个样本。
- **回复不拆条**：`split_reply` 原来只在 `。！？…` 处切，模型常整段用逗号连着说（"还是语音？…，字打几个会咋，非逼我骂你？"），于是永远一个大气泡。现在若只切出 1 段、且文本超过 `max(20, 上限×0.6)`，再按 `，,、；` 补切一次。实测该句被拆成「还是语音？」+ 后半段；短句不会被硬拆。
- 新增 2 条测试（逗号补切、短句不拆）。
- 说明：识图超时仍未解决——`workbuddy.tokenmao.cn` 的模型不接受图片输入（HTTP 400 拒收），需要在插件里把「识图专用接口」指向支持看图的接口；这属于外部服务限制，不是代码能修的。

## 1.2.83：Agent 技能包（P1-1 后端）（2026-10-23）

- 新增 `src/agent_skills.py`：`format="whale-agent-skill"` / `version=1` 的**纯数据**技能包（`id/name/description/triggers/permissions/instructions`），**不执行任何代码**。
- 权限白名单 8 项；`send_message` **即使声明也永不授予**，只在 `denied` 里回报（微信端硬约束）；未知权限直接 400。
- `match()` 只返回**启用中**且触发词命中的技能 —— 停用后不进规划上下文（计划要求）。
- `SkillStore` 落盘 `data/agent_skills.json`（最多 100 条），`preview()` 不落盘、`import_skill()` 需 `overwrite` 才能覆盖。
- 接口：`GET /api/agent/skills`、`/preview`、`/import`、`/toggle`、`DELETE /{id}`。
- 新增 `tests/test_agent_skills.py`（11 条）：格式/ID/触发词/未知权限校验、`send_message` 只声明不授予、停用不匹配、预览不落盘、导入/开关/删除、重复导入需覆盖。
- 未做（下一批）：技能面板 UI、`match()` 接进 Agent 规划、定时任务面板、「整理记忆」按钮。

## 1.2.82：定时任务（P1-2）（2026-10-23）

- 新增 `src/agent_scheduler.py`：任务字段 `id/name/action/every_minutes/enabled/last_run/last_result`，落盘 `data/agent_schedules.json`，间隔限制 **5~1440 分钟**。
- 动作白名单只有两个：`connection_check`（读一次 `status()` 记结论，**不自动重连**）与 `daily_digest`（写 `data/agent_reports/daily-YYYYMMDD.txt`：近 24h 已回复/失败/过期/排队、调用次数与 token、记忆与学习规模，末尾写明"不会发送到微信"）。
- `wechat_app` 启动时起一个 30 秒的 `_scheduler_tick`，`_stop_locked` 里一并取消（与补学任务同样处理，避免关停时拖住后台）。
- 新增接口：`GET/POST /api/agent/schedules`、`/run`、`/toggle`、`DELETE /api/agent/schedules/{id}`。
- **硬约束有测试兜底**：`tests/test_agent_scheduler.py::SafetyTests` 静态扫描模块，确认没有 `queue_outgoing`/`send_text`/`wechatauto`/`proactive`；另有"不认识的动作用 `send_daily_to_group` 直接拒绝"。
- 踩坑：`update()` 最初只认 `every_minutes/enabled/name`，`tick()` 写回的 `last_run` 被忽略 → 任务每 30 秒连刷一次（测试里 0 != 时间戳直接暴露）。
- 新增 `tests/test_agent_scheduler.py`（8 条）+ `tests/test_agent_api.py` 增补 2 条接口用例。
- 未做（下一批）：定时任务的界面面板（接口已就绪）、技能面板（P1-1）、「整理记忆」按钮、Agent 学习闭环。

## 1.2.81：记忆预算与「整理记忆」（P1-3 完成）（2026-10-23）

- 新增 `src/memory_maintenance.py`：`budget()`（手动记忆最多 **8 条 / 1800 字**，超出按调用方给的顺序截断）与 `maintain()`（合并近重复 ≥0.92 保留较新的一条、清理 7 天前 `superseded`、清理 30 天前单证据候选）。
- `conversation_memory.recalled()` 注入前过一遍预算：记忆再多也只带 8 条进提示词，避免"又贵又乱"。
- 新增 `POST /api/dialogue/memory/maintain`：整理记忆（只动本地库，返回合并/清理计数）。
- 安全边界：**已确认且唯一的记忆永不删除**；`superseded` 与单证据候选才清理；不同 `subject` 之间不合并（测试钉住）。
- 新增 `tests/test_memory_maintenance.py`（10 条）。
- 踩坑记录：给 `dialogue_management.py` 补 `import asyncio` 时按 `from fastapi import` 定位，误伤到 `_persona()` 内部那行，直接把文件搞成 IndentationError —— 改文件要先看清匹配在哪个作用域。
- 未做（下一批）：「整理记忆」的界面按钮、学习经验 60 天新鲜度、Agent「这次对了/不对」闭环、技能面板、定时任务。

## 1.2.80：记忆召回排序 + 冲突覆盖 + 敏感信息（P1-3 第一步）（2026-10-23）

- 新增 `src/memory_rank.py`（纯函数）：`bigrams`、`similarity`（二元组 + 概念归一 + 包含兜底）、`freshness`（默认 14 天半衰期）、`evidence_score`、`score` = **0.62 相似度 + 0.20 新鲜度 + 0.18 复证**；另有 `is_sensitive`、`supersedes`、`near_duplicate`。
- `chat_state.recall` 改用统一打分：查询为空时不打分按原顺序（仍按更新时间），有查询时**低于 0.10 的直接丢掉**，避免把无关旧事塞进提示词。
- `chat_state.remember` 写入新事实时，若命中"搬家/改名/换工作"这类变化词、且旧事实属于同一概念组（住处/称呼/工作），把旧条目标 `superseded`（数据留着，可追溯）；"喜欢猫"与"喜欢狗"不会被误判。
- `safe_fact` 增加证件/银行卡/验证码拦截（身份证 18 位、12 位以上卡号、4~8 位验证码、16~19 位长数字）。
- 新增 `tests/test_memory_rank.py`（12 条）：分词、相关度、概念归一、衰减、复证上限、综合打分、敏感词、`safe_fact` 拒绝、搬家顶掉旧记忆、近重复、召回排序实测、superseded 落库。
- 未做（下一批）：手动记忆按相关度限 8 条/1800 字、"整理记忆"UI（合并近重复 + 清理 7 天前 superseded / 30 天前单证据候选）、学习经验 60 天新鲜度、Agent 的"这次对了/不对"闭环。

## 1.2.79：Agent 页 UI（P0-4 页面完成）（2026-10-23）

- 控制台新增第 9 个标签「Agent」（放在**最后**：前面标签的索引被验收/自检按序号引用，插在中间会串页）。
- 页面结构：会话下拉（新建/删除）+ 左侧对话记录与输入框（"回复太长了，改成 40 字"）+ 右侧方案卡列表（显示动作与状态：待应用/已应用/已撤销）+ 「应用」「撤销」按钮。
- 只调 `agent/*` 接口，不直接改设置、不碰发送通道；`tests/test_agent_tab.py` 静态检查这一点（端点前缀、无 `queue_outgoing/send_text/wechatauto/mock/recv`、有应用与撤销按钮）。
- 空状态自动建会话；`showEvent` 首次进页才请求列表，不占用启动时间。
- 未做（下一批）：Agent 的"这次对了/不对"反馈与学习闭环、技能面板、定时任务、记忆升级。

## 1.2.78：P0-4 Agent 后端（白名单动作 + 多会话 + 预览/应用/撤销）（2026-10-23）

- 新增 `src/wechat_agent.py`：
  - 动作白名单只 5 个可写 + 1 个只读：`set_reply_length`、`set_picture_reply`、`toggle_plugin`（**只允许基础插件**）、`activate_persona`、`answer`；`control_service` 保留但明确拒绝启停（启动要微信连接、停止可能掐断正在发送的回复），只回报当前状态。
  - `plan()` 是确定性规则（"太长/改成 N 字" → 长度；"表情包不要每条都回" → 配图；命中插件 id → 开关；"换成 X 人设" → 切换），不调用模型，零消耗、可测。
  - `apply_card()` 逐条执行，任一条失败**回滚已执行的**；`undo_card()` 先核对"当前值还是不是当初应用的值"，被别人改过就拒绝撤销（避免覆盖别人的改动）。
- 新增 `src/agent_sessions.py`：`agent_sessions.json`，最多 50 段会话 / 每段 200 张卡片，列表只回摘要。
- 新增 `src/agent_api.py`：`/api/agent/sessions`（增删查）、`/api/agent/plan`、`/apply`、`/undo`、`/answer`；路由只做校验。
- **硬约束有测试兜底**：`tests/test_wechat_agent.py::SafetyTests` 用 AST 去注释后静态扫描 `wechat_agent.py`，确认没有 `queue_outgoing`/`send`/`wechatauto`/`ai.chat`/`bot.start()|stop()`。
- 新增 `tests/test_wechat_agent.py`(19) + `tests/test_agent_api.py`(4)。踩坑：`app.state.bot` 在 lifespan 里赋值，`TestClient` 必须 `with` 进入才会触发。
- 未做（下一批）：Agent 页 UI（对话气泡/会话列表/方案卡）、学习闭环、技能面板、定时任务、记忆升级。

## 1.2.77：控制台批量导入 UI（P0-2 完成）（2026-10-23）

- 「插件」页新增「批量导入…」：`QFileDialog` 多选 `.py/.zip`；直接点取消会转为"选文件夹"，把目录里的插件一次带进来。
- 流程：先调 `plugins/batch-inspect` **预检并展示清单**（✓/✗、同名冲突、危险用法逐条列出），再让用户选「覆盖同名并安装」/「只装不冲突的」/「取消」，最后调 `plugins/batch-install`。
- 为什么先预检：插件是要被 import 执行的代码，旧流程一个个装，用户看不到这批里有没有冲突或危险用法，装到一半失败还会留半成品；现在一次看全、失败自动回滚。
- 未做（下一批）：把这里再包一层拖拽（拖文件到插件页）、装后立即启用的勾选（后端已支持 `enable` 参数，UI 目前固定 False，避免用户误开高风险插件）、**Agent 页**（P0-4）。

## 1.2.76：插件批量导入（P0-2 收口）（2026-10-23）

- 新增 `src/plugin_batch.py`：`expand_paths`（文件/文件夹展开、去重）、`preview`（逐个预检、不写盘、标出同名冲突）、`install`（批量写入 → **只重载一次** → 加载失败按"覆盖/新装"分别回滚 → 可选装后立即启用）。
- 新增 HTTP：`POST /api/plugins/batch-inspect`、`POST /api/plugins/batch-install`；`wechat_app` 只做参数校验与 JSON 返回，业务流程全在 `plugin_batch`，测试与脚本可直接调用。
- 两个关键坑（都踩过）：
  1. `reload()` 只加载 `PLUGIN_MODULES` + **已登记的自定义插件**，光把文件写进目录不会生效 —— 必须 `register_custom(id)`，回滚时 `unregister_custom(id)`。
  2. 插件标识以 `PluginMeta(id=...)` 为准，不是文件名；文件名随手写成 `a.py` 时 `inspect_local` 会先按文件名报错（标识太短），`_inspect()` 会用代码里的 id 再试一次，不把好插件误杀。
- 启用逻辑：插件 schema 里没有 `enabled` 字段时返回 `enabled=None`，不再谎报"已启用"。
- 新增 `tests/test_plugin_batch.py`（10 条）：文件夹展开、去重、预检不写盘、同名冲突、危险代码拦截、安装成功、装后启用、坏插件回滚并删除文件、覆盖安装失败恢复旧文件、空输入报错。
- 未做：控制台的多选/拖拽 UI 接线（后端已就绪）、Agent 页、技能/定时/记忆升级。

## 1.2.75：冻结版只带基础插件（P0-2 收口）（2026-10-23）

- `wechat_bot.spec` 现在从 `src/plugin_catalog.py` 读 `MARKET_PLUGINS`，同时加进 `excludes` 与 `hiddenimports` 的过滤条件 —— 安装包只带 **21 个基础插件**；源码环境仍然是全量 32 个（测试覆盖不变）。设 `WHALE_KEEP_ALL_PLUGINS=1` 可打全量包（排查用）。
- `src/gui/smoke.py` 的插件数量断言改成按清单判定：冻结版必须**恰好** 21 个、源码环境必须恰好 32 个，多一个少一个都报错（旧断言是 `>=25`，两条路必有一条永远红）。
- 分层微调：`image_library`、`group_summary` 升为基础插件（配图与本地日报是常用功能，且被 GUI 自检直接引用）。现在是 **21 基础 / 11 市场**。
- `keyword_reply` 留在市场包；`smoke` 里它的 UI 检查改成"装了才检查"，冻结版记录 `skipped(市场插件未安装)`，不再让可选插件的缺席把安装包自检拖红。
- 新增 `tests/test_plugin_layering_freeze.py`（5 条）：spec 确实读 catalog、市场模块确实进 excludes、基础模块没被误排除、逃生开关存在。
- 未做：插件导入体验（多选/拖拽/预检展示/失败回滚/批量重载）、Agent 页、技能/定时/记忆升级。

## 1.2.74：P0-2 插件分层（清单 + 市场包）（2026-10-23）

- 新增 `src/plugin_catalog.py`：`BASIC_PLUGINS`(19) / `MARKET_PLUGINS`(13) / `MARKET_DESCRIPTIONS`，并提供 `validate()` 自检"清单与实际插件是否一一对应"。源码环境仍加载全部 32 个，`manager.PLUGIN_MODULES` 不动——**冻结版排除是下一步**。
- 新增 `tools/build_plugin_market.py`：把 13 个市场插件打成 `<中文名>-<id>.zip`（`plugin.json` + `<id>.py` + `使用说明.md`），同时输出 `README.md` 索引与 `使用说明/<id>.md`；每个包都过 `plugin_installer.check_source` 预检。默认输出 `D:\聊天机器人\插件市场\微信端`（实测 13 个 zip 生成成功）。
- `plugin_installer.check_source` 改为 **AST 去注释/文档字符串后**再扫禁用用法：`sticker_steal.py` 的说明里写着 `wechatauto.media.MediaDownloader`，旧实现把**注释**当成真调用，导致自家插件过不了预检。真 `import wechatauto`、`os.system(...)`、`runtime.queue_outgoing(...)` 仍然拦得住（新增 5 条回归测试）。
- 市场包对 `group_ambient` 单独放行"插件直接外发"这一条：它调用的是 `runtime.queue_outgoing`，受 `wechat.proactive.enabled` + 群白名单双重限制，属合规通道；该放行按插件 id 白名单生效，第三方插件仍会被拦。
- 新增 `tests/test_plugin_catalog.py`(8) + `tests/test_plugin_market_build.py`(4) + `tests/test_plugin_installer_scan.py`(5)。
- 未做（下一批）：`build_installer.ps1` 用 `--exclude-module` 排除市场模块 + 冻结自检断言 `plugins == 19`；插件导入体验（多选/拖拽/回滚/批量重载）；Agent 页。

## 1.2.73：新图标 + 控制台第四张指标卡（2026-10-23）

- **P0-3 图标**：`icon.ico` 换成鲸鱼娘角色图（256×256，取 QQ 5.0.4 的 `whale_girl.ico`），旧图标留档为 `icon-old.ico`。因为 `installer.iss` 的 `SetupIconFile=icon.ico`、`wechat_gui.app_icon()` 也读同一文件，所以安装包图标、exe 图标、窗口/任务栏图标一次全换。
- **P0-3 指标卡**：控制台从三张卡变四张，新增「记忆与省 token」。标题是"记忆 N 条 · 学习 M 条"，副标题是"N 位群友 · C 条待确认 · 已用 X token / Y 次调用"。
- 数据源：`status()` 新增 `memory` 段（`memory_overview()`），只做 4 条 COUNT（`persona_notes`、`chat_memories` 按状态、`learning_items` approved、去重 subject），走已有索引，不新增接口也不增加轮询次数。
- 降级：后台还是老版本（覆盖升级后未重启）时，卡片显示"记忆为空 / 还没有调用记录"，不会报错。
- 新增 `tests/test_memory_card.py`（4 条，卡片文案与降级）+ `tests/test_status_memory.py`（2 条，status 的 memory 计数）。
- 踩坑：`WeChatRuntime.mode` 是只读属性，测试里不能赋值（写成 `bot.mode='mock'` 会 AttributeError）。
- 未做（下一批）：插件分层代码落地（`plugin_catalog` + 市场包 + 冻结版排除）、Agent 页、技能/定时/记忆升级。

## 1.2.72：P0 起步——插件分层清单 + Mio 浅色默认背景（2026-10-23）

- 按《微信端 1.3.0 更新计划》开工。本版完成 **P0-1 基线审计** 与 **P0-3 的第一项**。
- 新增 `docs/插件分层清单-1.3.0.md`：实机加载 32 个插件逐条记录 id / 名称 / 默认启用 / 归属，划分为 **19 个基础内置 + 13 个市场包**。计划原文提到的 `mention_nudge_guard`、`quote_reply` 在本仓库属 `RETIRED_PLUGINS`，已用现有能力替代并在文档里写明。
- 新增资产：`assets/backgrounds/mio_light.png`、`assets/whale_girl.ico`、`assets/whale_girl.png`（取自 QQ 5.0.4，为后续换图标备用，图标替换本身还没做）。
- `src/gui/config.py` 新增 `default_background`（默认 True）与 `apply_default_background()`：首次启动把包内浅色背景**复制**到 `%LOCALAPPDATA%\WhaleGirlWeChat\data\backgrounds` 再写入 `background_image`。为什么不直接指向包内路径：安装目录是随版本变化的只读资源，覆盖升级后会指向不存在的文件（用户看到黑屏）。
- `wechat_gui.py` 在 `MainWindow` 初始化时调用一次；用户自己设过背景、或主动清除并写了 `default_background=false` 的，一律不动。
- 踩坑记录：`sanitize()` 是从 `DEFAULTS` 深拷贝出来的，新增字段必须从**输入** `cfg` 读，不能从 `clean` 读（否则永远是默认值）——`tests/test_default_background.py` 钉住了这条。
- 新增 `tests/test_default_background.py`（6 条）。基线：后端自检、HTTP 自检、全量测试均通过。
- 未做（下一批）：图标替换、四张指标卡、插件分层代码落地（`plugin_catalog` / 市场包 / 冻结版排除）、Agent 页。

## 1.2.71：图片取原图（不再用缩略图）+ 识图可接独立接口（2026-10-23）

- 用户反馈："图片压缩的太小了"——存下来的图只有 180×149 / 6 KB，调 `visionMaxSide` 也没用。
- 根因：`wechat_transports.message_from_raw` 组装媒体时只认 `cdnurl` / `thumburl`。聊天图片的 XML 用的是 `cdnbigimgurl` / `cdnmidimgurl` / `cdnthumburl`，于是取不到原图链接，退回 `download_media(tier='best')` 的本地缩略图。**源头就是缩略图，后面再怎么放大都没意义。**
- 修法：按 `cdnbigimgurl → cdnmidimgurl → cdnurl → cdnthumburl → thumburl` 顺序取第一个可用链接；大小按 `hdlength → length → len` 取（原来只读 `len`，会低估原图体积）。
- 顺带：`image_understanding` 新增 `visionApiUrl` / `visionApiKey` —— 可以把识图单独指向另一个 OpenAI 兼容接口（例如本地 Ollama 的 `http://127.0.0.1:11434/v1/chat/completions`），绕开"中转只支持文字"的限制；`ai_engine.describe_images(..., api_url=...)` 走独立的 `_post_vision`，只发一次、失败抛 `HTTPStatusError` 交给现有错误映射。
- 实测背景：用户的中转 `workbuddy.tokenmao.cn` 下 11 个模型**全部拒绝图片输入**（HTTP 400 upstream_error），所以不接视觉接口时该功能不可能可用；现在至少能把识图指到别处。
- 新增 `tests/test_image_cdn_fields.py`（4 条，CDN 字段优先级与体积）+ `tests/test_vision_endpoint.py`（3 条，独立接口路由与错误透出）。

## 1.2.70 发布形态：安装包 + DIY 指令包（2026-10-23）

- 用户定下新交付模式：项目已开源，每次发布在 `发布/<版本>/` 里出两件套 —— ① 安装包 ② DIY 指令包（喂给 DeepSeek 的提示词 + 完整源码）。以后都按这个来。
- 新增 `tools/make_release.py` 与 `tools/make_release.ps1`：读 `APP_VERSION`，把安装包复制进 `发布/<版本>/`，同时生成 `WhaleGirlWeChat-源码-<版本>.zip` 与 `WhaleGirlWeChat-DIY指令包-<版本>.zip`，并写 `发布说明.md`。
- 新增 `diy-src/`（说明书源头，随源码入库）：`00-先读我`、`01-喂给AI的指令`（项目事实 + 七条硬性红线 + 插件钩子契约 + 交付流程）、`02-项目地图`（功能→文件对照）、`03-常见改造任务`（十个真实需求改法 + 六个踩过的坑）、`04-已知限制`（发送通道/识图/记忆/平台，避免 AI 乱承诺）、`05-自测与打包`。
- `AGENTS.md` 增补第〇节：交付模式与目录约定、DIY 说明书必须随功能更新、打包白名单会自动排除个人数据与安装包本体。
- 已验证：`发布/1.2.70/` 下三个文件齐全（安装包 162.5MB、DIY 包 7.17MB/301 项、源码包 7.16MB/295 项），压缩包完整性检查通过，且不含 `.exe/.dll/data/logs/.venv` 条目。

## 1.2.70：修复回复整形从未生效（同步钩子被 await）（2026-10-23）

- 现象：1.2.69 装好后真实群聊里仍然带「@西莲寺紫」、仍然长。用户截图两条证据。
- 根因：`chat_controls.on_post_reply` 我写成了**同步函数**，而 `PluginManager.run_on_post_reply` 是 `await plugin.on_post_reply(...)` → `TypeError: object str can't be used in 'await' expression`，被 `except` 吞掉后打印一行 `[plugins] ... error` 并返回原文。**1.2.68/1.2.69 的"去@、限长、拆条"全部没生效**。
- 修法：改为 `async def on_post_reply`；测试改为 `await` 调用。
- 守卫升级：`tests/test_plugin_hook_signatures.py` 新增 `test_awaited_hooks_are_really_async` —— 用 `inspect.iscoroutinefunction` 检查所有插件的 `on_prepare/on_post_reply/tick` 必须真的是协程。这是同一类问题第二次发生（1.2.67 是签名参数错位），现在两类都能被测试挡住。
- 识图超时默认 15 → **25 秒**（`image_understanding` 的 `visionTimeout`，现场配置也一并改成 25）。原因：用户中转站一次视觉请求约 13 秒以上，2048 边长时更容易超过 15 秒，表现为"图片识别超时了"。
- 未改：人设语气（用户截图里有"别拿这破号乐子"这类冲话，属人设/风格设定，不是本次 bug）。
- 带图/表情消息不受"未点名回复概率"限制：`has_picture()` 识别 media 与 `[图片]/[表情]/[动画表情]/[视频]`，命中即视为被点名（仍受冷却与每分钟上限）。实测用户发表情包七成概率被静默。
- 识图失败文案区分原因："接口拒绝图片输入"现在直接说"当前接口的模型不支持看图片，换成支持识图的模型/接口再发"。
- **实测结论（重要）**：用户的中转 `workbuddy.tokenmao.cn` 下 11 个模型（TM ·Auto/Fast/Deep/GLM5.3/HY4/Kimi-K3/MiniMax-M3/Space-Bunny/DeepSeek-V4.1 等）**全部拒绝图片输入**（HTTP 400 upstream_error「请求被模型拒收」）。所以"纯图片无法识别"不是代码问题，是该服务商不支持视觉。要真正识图需换支持 vision 的接口，或接本地视觉模型（如 Ollama + qwen2-vl/llava）。
- 新增 `tests/test_picture_reply_gate.py`（6 条）。

## 1.2.69：长回复拆成几条短消息发（2026-10-23）

- 用户要求："回复的长度可以分成多个句子发送"。原来的 `send_pacing` 插件**已退役**（`plugin_policy.RETIRED_PLUGINS`，禁止重新加载），所以没有复活它，而是在核心插件 `chat_controls` 里用运行时已有的 `\x01PACE:N\x01` 标记实现：一条回复按句末切成多段，段间停顿 `segmentDelayMs`（默认 800ms），每段不超过 `replyMaxChars`。
- 新设置：`replySegments`（长回复拆成几条，1=不拆，默认 2）、`segmentDelayMs`（200~5000ms）。与 1.2.68 的 `replyMaxChars`/`noAtInReply` 配合：先去掉 @昵称，再按句切、超长句截断、尾部合并超条数。
- `split_reply` 是纯函数：一句一条；超过条数上限时把剩下的并成最后一条；合并后仍超长则截断——保证每条都不超上限、也不会丢掉整段内容。
- 新增 `tests/test_reply_split.py`（10 条）+ 更新 `tests/test_reply_shape.py`。
- 未做：拆条间隔的可视化预览；若觉得连发太密，把 `segmentDelayMs` 提到 1500~2000。

## 1.2.68：回复别太长、别 @ 人（2026-10-23）

- 用户反馈（截图）：机器人一段回三四行，且开头带「@西莲寺紫」。
- 「关键词与群聊控制」新增两项：`replyMaxChars`（默认 80，0=不限）与 `noAtInReply`（默认开）。前者在提示词里要求"一到两句、不超过 N 字"，后者要求"不要写 @昵称/@所有人"。
- 提示词不一定管得住，所以加 `on_post_reply` 兜底：`strip_at_mentions` 去掉开头连续的 @昵称（支持全角＠、冒号、多个连续；不会误伤邮箱），`shorten_reply` 在**句末**收住超长回复（找不到合适句末才补 …）。
- 新增 `tests/test_reply_shape.py`（12 条）：去 @ 的多种写法、邮箱不误伤、句末截断/硬截断、0 不限、插件默认值、提示词内容、`on_post_reply` 端到端。
- 未做：表情包/图片与文字的比例仍按「定期配图」的设置；如需"每条最多一句"的更狠档位，可把 `replyMaxChars` 调到 30~40。

## 1.2.67：修复「联网现查」从未生效（钩子签名错位）（2026-10-23）

- 现象：真实日志里每轮都出现 `联网现查（搜一下/最新…） 读取附加消息失败`。
- 根因：插件 `on_prepare` 写成了 `(message, conversation, runtime)`，而 `PluginManager.run_on_prepare` 只传 `(message, runtime)` → `TypeError` 被 `except Exception` 吞掉，只留一行提示。**1.2.58 加的联网现查实际上一次都没跑过**；单元测试因为直接调插件传 3 个参数，所以全绿。
- 修法：签名改为 `on_prepare(self, message, runtime)`；测试改为走 `manager.run_on_prepare`。
- 新增 `tests/test_plugin_hook_signatures.py`：用 `inspect.signature` 校验**所有插件**的 `on_prepare/on_pre_ai/on_post_reply/on_ingest/on_sent/tick` 与 manager 调用方式一致，并有一条走 manager 路径的实测（断言不出现"失败"提示、`_web_lookup` 有结果）。这类"被 except 吞掉"的错位以后会在测试里直接现形。
- 生产日志顺带证实：机器人启动后立即开始处理离线积压消息，图片储藏、识图、联网现查均在跑。

## 1.2.66：把"发送未就绪"翻译成用户能做的动作（2026-10-22）

- 现象：用户长期以为"连不上微信、无法回复"。真实状态是"读消息已连接，发送未就绪"，而界面给的是 `严格后台等待：微信只提供外层窗口，没有开放聊天输入控件` —— 普通用户不知道要点哪里。
- 修法：新增 `send_channel_hint(environment, compat)`，按 `waiting_code` 补一句可执行处理：`accessibility_unavailable` 且未开兼容 → "设置 → 发送通道勾选「兼容模式」后重新启动机器人"；`minimized` → "恢复窗口、不要最小化"；`chat_not_open`/`input_unavailable` → "先手动点开目标会话"；`draft` → "清空输入框草稿"。启动日志与等待中的状态文案都改用它。
- 现场同时处理：把用户 `config.wechat.yaml` 的 `send_compat` 从 false 改为 true（备份 `config.wechat.yaml.bak-before-compat2`）。这是**唯一**能让微信 4.1.15 严格后台通道拿到输入框的办法；另一条路是键鼠切窗口，属于封号红线，不做。
- 新增 `tests/test_send_channel_hint.py`（6 条）。
- 「自我记忆」空表时给出原因说明：只有对方明确说「我喜欢…／我不喜欢…／我叫…」这类自述才会自动记录；用户真实库里这类句子是 **0 条**（扩展前缀也只命中 1 条噪声），所以空白是正常行为，不是坏掉。同时指向「本地学习库」说明两套记忆的区别。
- 补学分块：每会话最多 200 条、会话之间让出 50ms。原因：改成同步执行后，一次处理上千条会把事件循环卡住，安装版验收（关闭后台）会因此超时——实测 `test_gui_live_acceptance` 复现并修复。
- 待用户确认：开兼容后是否仍出现"回几次话就要求重新登录微信"；若出现，下一步做"发完还原门控字节"。

## 1.2.65：补学按当前人设归属（记忆页在用户人设下可见）（2026-10-22）

- 现象：用户升到带学习库的版本后，记忆页仍是空的。
- 根因：`conversation_log.persona` 有大量历史行为空（人设功能上线前写的），补学时一律落到 `DEFAULT_PERSONA`（tsundere_whale_girl），而用户当前人设是 `123` → 严格隔离下一条都看不见。
- 修法：`rebuild` 补学时对**没有 persona 标记**的旧行改用 `runtime.style.active_name`（有标记的仍按原人设）。人设隔离本身没有放宽。
- 实测：在用户真实库上补学 → 96 条学习结果（264/12/47 条原文，分别产出 90/4/2 条），全部归属人设 `123`。
- 另外：用户 `plugin_settings.json` 的 `chat_controls.replyProbability` 原为 0（群聊未点名不回复，即截图里"不加关键词就不回复"），现场调成 30 并备份为 `.bak-before-replyprob`。这是**配置**改动，不改默认值，红线不变。

## 1.2.64：修复补学任务拖住关闭（安装包验证曾因此失败）（2026-10-22）

- 现象：1.2.63 的 `verify_installer.ps1` 报"验收后台没有正常退出"，退出码 1，安装包未通过发布门槛。
- 根因：1.2.58 加的启动补学用 `asyncio.to_thread(self.learning.backfill_all, ...)` 整段丢进线程；`stop()` 也没取消这个任务。`asyncio.run` 收尾要等默认执行器，线程还在跑 → 后台线程一直 alive → 验收断言失败。
- 修法：拆成 `backfill_candidates()`（筛出需要补的会话）+ `backfill_one()`（补一个，异常吞掉），`_backfill_learning()` 改成在事件循环里**逐会话**处理、中间 `await asyncio.sleep(0)` 让出；`_stop_locked()` 把该任务一并 cancel 并置 None。顺带避免了"后台线程和事件循环同时写同一个 sqlite 连接"的隐患。
- 验收口径：`test_close_is_not_blocked_by_pending_learning_backfill`（关闭 < 5 秒且任务已清空）、`test_backfill_uses_candidates_and_feeds_learning`（真实运行时里群聊前缀正文能变成学习结果）。
- 已验证：全量测试通过、冻结自检、安装包往返验证恢复通过。

## 1.2.63：主动开口加「亲密度」门槛（2026-10-22）

- 「群聊主动行为」（`group_ambient`，一直默认关闭）新增三项设置：只对聊得热的群开口（`intimacyEnabled`，默认关）、统计天数（`intimacyDays`，默认 7）、门槛（`intimacyMin`，默认 30）。
- 亲密度定义：最近 N 天该群里**机器人真正回过**的消息条数，20 条为满分 100（`intimacy_score` 纯函数，便于测试）。只数 `reply` 非空的行 —— 只被人问过一句还谈不上"熟"。
- 频率控制沿用原有：每群最小间隔、`maxPerHour` 全局每小时上限、免打扰时段、只在最近有人说话时开口；发送仍必须过 `runtime.queue_outgoing` 的三道关（`wechat.proactive.enabled` + 群白名单 + 去重）。
- **红线未动**：插件仍默认关闭；没有新增任何主动外发入口，也没有绕过白名单。
- 新增 `tests/test_intimacy_proactive.py`（9 条）：分值边界、只数有回复的消息、门槛生效时冷群不开口、默认关闭。
- 未做：按人设区分图库偏好、音频转文本复验、发送门控"发完还原字节"（等用户确认重登提示的形态）。

## 1.2.62：新插件「回复节奏（别秒回）」（2026-10-22）

- 插件 `reply_pace`：收到消息后随机等 `minDelay~maxDelay` 秒再开口（默认 2~6 秒、默认**关闭**），对方消息超过 30 字再额外等 0~`longTextExtra` 秒。走的是既有 `delay` 提示通道，只在"正常档"生效（极速档本来就不等待），不改内容、不改发送上限、不碰红线里的任何接口。
- 与"回复速度三档"的区别：三档管上下文与输出预算（生成耗时），本插件管**开口前的等待**，两者独立。
- 为什么加：秒回是最明显的机器人特征，同群连续零延迟也更容易被按行为模式识别。
- 配图节奏沿用「定期配图」已有的"每 N 条回复附图"（`sendEveryN`），未新增设置项。
- 新增 `tests/test_reply_pace.py`（5 条）：默认关闭不等待、范围可设、长提问多等、0 秒不返回提示、上下限写反不抛错。
- 未做：按人设区分图库偏好、主动回复插件（频率+亲密度）、音频转文本复验 —— 留待下一批。

## 1.2.61：挑图按名字+含义加权（2026-10-22）

- 「定期配图」（sticker_cadence）原来纯轮转：聊到下雨也可能配张笑脸。现在先按当前消息内容给候选打分 —— 标签命中权重 3、含义说明命中权重 1（中文取二字切分、英文取整词），同分保持轮转顺序，全部不相关时与以前完全一致。
- 含义说明取自机器人图库的 `description`（识图回填的说明也在此列），所以"图的名字"和"图是什么意思"都会影响选择。
- 新增 `tests/test_sticker_pick_weight.py`（6 条）：名字优先于含义、含义命中、同分保轮转、空文本纯轮转。
- 未做（需要产品确认）：按人设区分图库偏好、降低回复速度的按钮与表情包节奏 —— 这两项要动设置面板与发送节奏，留待下一批。
- 待用户确认的风险项：微信 4.x 的 `send_compat` 会保持"无障碍开关字节=1"直到微信重启（库本身在字节已为 1 时不会重复写）。若"回几次话提示重新登录"由此引起，需要改成"发完还原字节"，那会让 UIA 树重新塌陷、只能按发送瞬间开合，属于改动风险区，先征得同意再动。

## 1.2.60：群聊学习失效的真因（发送者前缀）与素材清洗（2026-10-22）

- **真因**：群聊归档正文形如 `wxid_xxx:\n我手忙的发软了`，而学习提取遇到换行直接跳过 —— 所以群聊**一条都学不到**，记忆页长期空白。新增 `strip_sender_prefix` 在观察/补学前去掉发送者前缀。用用户真实库副本实测：同一群从 **0 条 → 90 条**学习结果，私聊 2 条。
- 素材清洗：`[图片]`、`[表情]`、`[文件/链接]`、`[拍一拍] …` 等占位符、`YCNY20250101083` 这类数字过半的单号、以及单字符符号，不再当成"表达例句"；真实库里这批噪声原本占了 38 条里的 36 条。
- 展示口径：切换人设后，默认人设下学到的条目仍会显示/统计（否则换个人设记忆页又变空白）。**召回仍按人设严格隔离**，只有展示放宽，避免把默认人设的事实串到别人设。
- 每会话学习结果仍上限 200 条，活动窗口 1000 条不变；不删除原文与归档。
- 已验证：真实库副本补学 264+12+47 条原文、产出 92 条学习结果；全量测试通过。
- 未验证：真实微信里"批准表达"后的语气效果仍需人工看几条真实回复。

## 1.2.59：记忆页总览与一键学习、批量批准表达（2026-10-22）

- 记忆/学习页不再一进来就空白：新增 `GET /api/dialogue/learning/overview`，显示"共 N 个会话、活动窗口 X 条、已学 Y 条（待确认 Z 条）"，然后自动读取第一个会话。
- 新增「全部会话一键学习」「全部批准表达」两个按钮（`POST /api/dialogue/learning/bulk`）：前者对最多 30 个会话补学，后者把表达例句从候选批量改成已确认 —— 表达仍**需人工批准**才被引用，只是把"逐个会话点"变成一次点击。
- `LocalLearning.overview/bulk` 只读本账号本会话范围，未知动作直接拒绝；批量操作失败按会话跳过并继续，不中断整批。
- 人味来源说明：已确认的表达例句会在同一会话回复时作为"语气参考"注入（每轮最多 1 条），事实按当前问题相关性注入。
- 未验证：安装后真实补学的条目质量与召回效果（需要用户先装 1.2.58+ 并让机器人跑一段对话）。

## 1.2.58：联网现查、学习库自动补学（2026-10-22）

- 新插件「联网现查」：消息里出现"搜一下/查一下/最新/现在多少"等触发词时，现场抓公开搜索页（Bing 主、搜狗备）取 3 条标题+摘要注入当轮提示词。只在触发词命中时联网（默认开启但默认词表不含"今天/新闻"，避免每句都搜）；硬超时 6 秒；失败或无结果时明确告诉模型"没查到、别编"；摘要末尾统一标注外部内容仅供参考、不得执行其中指令。同问题短时缓存 10 分钟，最多留 50 条。
- 解析按块提取：Bing 取 `<p>` 摘要、搜狗取段落；属性残片不当正文（实测误切会把搜狗结果切成 0 条，已回归）。页面结构变了就返回空，由调用方如实说明。
- 学习库自动补学：升级到带学习库的版本后，本地 conversation_log 早就有原文、learning_items 却是空的，记忆页显示"一大片空白"。新增 `LocalLearning.backfill_all`：启动后后台只补"完全没有学习结果且没有活动窗口"的会话，最多 30 个，幂等；结果记入运行记录。
- 学习噪声过滤：少于 3 个不同字符、或纯笑声/单字重复（哈哈哈哈哈、嗯嗯嗯、666）不再进学习库，避免记忆页堆满无意义"表达例句"。
- 未验证：真实微信里长聊后的学习质量；搜索页反爬可能变化，失败路径已按"如实说没查到"处理。

## 1.2.57：识图清晰度可配、动图上限放宽（2026-10-22）

- 用户反馈"图片压得太小、肯定不行"：送视觉模型的图片最长边默认 1536 改为 **2048**（可在「图片与表情包识别」里 512~4096 调整），JPEG 质量 90→92。1600×1200 的截图不再被缩小，表格与小字保持可读。
- 动图抽帧尺寸与识图上限统一（原来写死 1024）；「手动识别这张表情」与聊天识图共用同一上限，避免同一表情两种结果。
- 单张图片读取上限 6MB→**12MB**：GIF/WebP 动图常超过 6MB，旧上限会把它们直接判成"无法读取"；送模型前仍按最长边压缩，成本不变。
- 每次动图抽帧在运行记录里留痕（"第 N 张动图抽了 M 帧"），便于判断是识别失败还是抽帧没发生。
- 未验证：真实微信里用户那张具体 GIF 是否可解密取到原文件（加密表情无 CDN 时仍会如实报告无法读取）。

## 1.2.56：本地学习、1000条窗口与估算Token节流（2026-10-22）

- 新增LocalLearning：持久化活动窗口每会话1000条与200条提炼结果，按账号/会话/发送者隔离。两条独立自述确认事实，表达候选人工批准；不学习自己的回复，不自动改人设，删除原文撤回证据，重复读取不重复计数。原始归档及升级数据不自动删除。
- 对话页默认显示并读取本地原文，手动表达偏好与聊天记忆区分，新增学习库查看、提取、批准、拒绝和删除入口。群聊旧记录召回限定在1000条活动窗口内。
- Token节流新增对话文字估算预算，相关记忆优先于旧闲聊；当前问题与受保护内容保留，预算无法满足明确记录。估算不冒充模型账单，也不额外调用学习模型。
- WeFlow只读HTTP增加ChatLab历史导入，核对ownerId/当前账号/会话，最多1000条且整批校验后事务写入，重复去重、不触发历史回复。已检查用户提供x64 wcdb_api.dll依赖WCDB.dll，未加载未知DLL或自动启用数据库写入能力；本机默认API未连通，真实连接仍待环境具备。

## 1.2.55：图片先储藏再识别（2026-10-22）

- 仅对通过回复规则的图片取图：微信本地导出、CDN 下载、已登记表情原文件都先保存至 data/received_images，再读取校验后的副本交给识图模型。GIF/WebP 保留原始文件后才抽帧；缓存命中也确认副本落盘。
- 储藏区按 SHA256 去重，超过 50 张删除最旧 floor(N/2) 张（51→26）；不递归清理，不动聊天记录、记忆、表情库和微信原图。保护本次原图，记录无法删除的数量。并发保存与回读串行，防止读取前被清理。
- 插件设置新增「打开图片储藏区」入口；运行记录显示保存及清理数量。写入失败、损坏、像素超限不调用视觉模型；没有原文件或下载地址的加密表情仍报告失败，不虚构下载或画面。
- 对照 QQ 版的先取字节再识别流程，微信继续经 transport.download_media 接口取图，不直接照搬 QQ URL 通道。

## 1.2.54：严格后台能力说明与识图失败追问（2026-10-22）

- 真实只读检查：已绑定的账号窗口未最小化，但微信只提供 3 个外层节点，没有输入框/发送按钮。新增 input_search/waiting_code，区分控件未开放、未打开目标聊天、最小化、草稿占用；不将此状态伪报为发送就绪，不使用键鼠/置前/切聊天回退。
- 空队列下健康检查每 15 秒只读刷新后台能力，避免手动恢复窗口后仍长期停留旧的最小化状态。
- 控制台长状态放在按钮下换行，卡片可收缩且等宽，保留完整提示文本，防止长路径把按钮和模型卡片撑出窗口。
- 历史识图失败的机器人回复不再反复进入模型输入，原始归档保留；明确短追问在同一会话/发送者五分钟内按已知状态解释取图失败，不责怪用户、不编造黑图破图、不自动附图。成功识图、新图片与其他问题保留原模型链路。

## 1.2.53：连接测试超时、本地聊天记忆与 Token 节流（2026-10-21）

- 模型诊断采用表单超时（旧客户端采用已保存超时），修复固定 15 秒/4 秒与界面不一致；GUI 等待同步延长，列表失败与对话失败分开显示。根域名自动补全路径与带空格模型名保留。
- 回答时从当前会话本地归档检索相关旧记录，并与近期上下文一起提供；原始消息 ID 去重，避免发送日志和归档重复进入同一轮提示。检索不额外调用模型、不跨账号/群聊，已删除记录不恢复，人设切换不带入旧机器人回复。
- 新记录单条原文及已发送回复归档上限提升至 65536 字符，提示词只取片段；已有被旧版截短的原文无法凭空恢复。不因预算或升级删除历史。
- 修复 Token 节流在消息条数少时忽略字符预算的问题；保护当前问题、所有 system/tool 和多模态内容，历史摘要不再提升为系统指令，去重使用完整内容且仅压相邻重复。新配置建议单条 1200、总预算 6000，已有用户显式配置不覆盖。
- 运行日志显示直接对话/近期背景/相关旧记录条数；节流详情可显示字符节省量。修复 temperature=0 在实际生成时被替换，以及准备超时日志误报为输出 token 预算的问题。
- 群聊未回复诊断分开说明 @/关键词未命中、概率、每分钟上限、冷却剩余时间；修复超过 60 秒的冷却被一分钟查询窗口截断，以及 processing 状态漏计的问题，未放宽触发与发送约束。
- 加密表情无 CDN 时以 MD5 定位用户本地图原文件并验证 SHA256；旧图库不必重导，校验成功后才送视觉模型。未知表情仍如实提示；纯不可读表情不再调用文字模型，也不自动附图。识图技术错误仅保留本机诊断，支持单引号 XML 媒体属性。

## 1.2.52：人设导入兼容与启用提示（2026-10-21）

- 修复 GUI 直接使用中文文件名作为内部标识而被拒绝的问题：自动生成合法标识，中文显示名保留，同名在后台分配副本，不覆盖旧文件。
- 支持 QQ/微信 YAML/JSON、prompt 别名、TXT 人设正文和 UTF-8/UTF-16/GB18030 文本；无正文、重复字段、未知版本、无效参数与示例会明确拒绝，不生成空白人设。
- 导入只保存并选中；界面持续提示点击「设为当前」启用，不擅自切换人设。旧详情回调不覆盖当前选择，导入中阻止重复点击。
- 导入后读取详情保留有效的 temperature=0，不再误当作缺省值改成 0.7。
- 内嵌图片验证和人设写入失败时清理本次新增孤立图片，保留已有图片和人设。文件中的路径或命令不执行。

## 1.2.51：账号绑定和 WeFlow 只读检查（2026-10-21）

- 修复账号路径规范化到公共根目录后丢失账号选择的问题，支持库内目录与具体 .db 文件。
- 显式账号不存在、路径与下拉框冲突或底层静默换号时连接失败；不记录错误账号记忆，不创建发送器。保留自动发现原有顺序。
- 可选 WeFlow 本机只读诊断、会话与消息预览、JSON 导出；Token 加密，查询限流量和时间，禁止重定向、系统代理、非回环地址、消息写入或发送端点。
- GUI 地址/凭据/会话改变使旧预览失效；不将历史查询写入 inbox，不触发机器人回复。
- 新状态字段分离读取与发送，客户端登录状态未知时不伪造在线。AstrBot 实时接入未在本版启用。

## 1.2.49：识图失败与后台弹窗（2026-10-20）

- 1.2.48 的 UI 在勾选严格后台时因兼容模式已开启而反向取消严格后台，后端也会按兼容优先保存。新界面显式提交 send_mode，严格后台取消兼容项；旧客户端继续使用 legacy 组合口径。新增保存、重新加载、重启及 worker 禁止前台回退回归。
- 日志中视觉请求超过 6 秒被极速档取消，随后 TimeoutError 被包装成 RuntimeError，错误显示又变成“模型未返回内容”。识图现在遵守插件 visionTimeout，保留真实超时类型并优先于上一请求残留错误分类。
- 新增真实图片字节测试：透明底铺白、EXIF 方向、按比例压缩、模型收到实际 image_url 图片块；未落盘图片在完成第一轮取图后有限重查，保持多图编号。
- 实际配置的 TM ·Fast 对生成红蓝色块识别成功（1.80 秒，见 .build-tools/v149-live-vision.json）。这不能证明故障现场每张图片都可解密，也不代表真实微信后台发送已验收。403/404 属服务商权限或模型配置，程序不会擅自更换模型或密钥。

## 批次二十九：语音/识图提速 + 「不抢鼠标」2026-10-20，1.2.47

### 一、语音识别（实测修复）

`asr_service._ensure_model` 只认 `model_dir/<name>/model.bin`，而 faster-whisper 的缓存
布局是 `model_dir/models--Systran--faster-whisper-<name>/snapshots/<hash>/model.bin`。
结果：**缓存里的模型被当成没下过** → 每次启动联网重新解析，实测卡 **368 秒**；
网络不好就整体失败 —— 这就是"语音经常失效"。

改成直接定位 `model.bin` 所在快照并 `local_files_only`：**加载 0.42 秒**（原 368 秒）。

### 二、识图（能力正常，慢）

实测同一份配置：视觉调用**能**返回（"图1为纯红色背景…"），但单张耗时 **12.9 秒**，
加后续文本调用就撞上 21 秒请求超时 → 用户看到"请求超时/看不清"。提速方向：
先把图缩到 640px 再送视觉（下一批次）。

### 三、「不抢鼠标」

1. 兼容模式物化控件树后，**先试完全非激活的发送**（UIA 写值 + Invoke 发送按钮，
   不 SetFocus、不 SendKeys）。只在"目标会话已经打开"时成立。
2. 需要切换会话时激活不可避免，因此发送结束后把**前台窗口还原**给发送前的窗口
   （含 AttachThreadInput，否则子进程 SetForegroundWindow 会被系统忽略）。


## 批次二十八：透明度拉满无法自救 / 人设导入卡顿或不成功 2026-10-17，1.2.39

### 一、透明度拉满，重装也没用

外观设置存在 `%LOCALAPPDATA%\WhaleGirlWeChat\data\gui_config.json`，**覆盖安装会保留**，
所以"重装"当然救不回来；而 `transparency` / `panel_transparency` 原本允许到 1.0，
拉满后窗口几乎看不见、卡片也点不准。

| 文件 | 改动 |
| --- | --- |
| `src/gui/config.py` | 新增 `MAX_TRANSPARENCY=0.85`、`MAX_PANEL_TRANSPARENCY=0.90`；`sanitize()` 在**加载时**把超限值收回来 —— 已经写坏的配置会在下一次启动自愈 |
| `wechat_gui.py` | 外观页新增 **「恢复默认外观」** 按钮：透明度/面板透明度/模糊度回默认、清背景图、取消等比例缩放，并**立刻应用+写盘** |

实测：`{'transparency': 1.0, 'panel_transparency': 1.0}` → 载入后 `0.85 / 0.90`；
正常值（0.3 / 0.5）不动。

### 二、人设导入"卡顿或不成功"

原来流程要弹**第二个模态输入框**问"新标识"：无边框+半透明主窗口上的模态输入框容易卡，
而且留空/撞名时后端直接报"同名人设已存在"，导入就失败了。

改成：**不弹第二个框**，用文件名自动起名；遇到重名**自动加 `_2 / _3`… 重试**（最多 20 次）；
其它错误如实写进提示。导入完会告诉你是以什么名字导入的，要改名在人设页改即可。

### 验证

* `tests/test_appearance_and_persona_import.py`（7 条）：透明度超限被收回、正常值不动、
  垃圾值回默认；「恢复默认外观」把四个滑块/背景图重置并立刻写进配置；
  导入用文件名且只请求一次、撞名自动加后缀重试、其它错误原样提示。
* 全量 `unittest discover -s tests`：**1296 条全绿**；两项自检通过。


## 批次二十七：把回复提速 2 倍（「回复速度」三档）2026-10-17，1.2.38

用户：`还是太慢了，速度能提升2倍吗`。

### 先实测，别瞎调

用用户自己的中转（`TM ·Auto`）测了两次同样的请求：

```
正常输出：26 字 → 4.55 秒
压到 120 token：8 字 → 2.38 秒
```

拟合出 **≈1.4 秒固定开销 + 每字 0.12 秒**。他的回复常常两段共 100~150 字 →
单条 13 秒以上。所以提速只有两个有效杠杆：**少带历史（固定开销）、少让模型写字（每字成本）**，
调温度/换参没用。

### 改了什么

设置页「模型与 API」新增 **回复速度**（`wechat.reply_speed`）：

| 档位 | 历史轮数 | 输出上限 | 识图等待 | 自动重试 |
| --- | --- | --- | --- | --- |
| 极速 turbo | 2 | 140 token | 6 秒 | 关 |
| 快速 fast（**默认**） | 4 | 260 token | 10 秒 | 关 |
| 正常 normal | 6 | 不封顶 | 15 秒 | 开 |

落地三处（都有测试）：

* 取历史的 SQL `LIMIT` 用档位值（`_work` 里 `self.speed_plan()["turns"]`）；
* 输出 `max_tokens` = `min(人设值, 档位上限, 插件上限)`；
* `image_understanding` 的识图超时再被档位压一次（极速档 6 秒）。

按实测公式估算：100 字的回复在 `fast` 档下从 ≈13 秒降到 ≈6 秒左右（历史也少一半），
`turbo` 档更短。

### 验证

* `tests/test_reply_speed.py`（3 条）：档位参数严格递减、未知值回落 `fast`、
  **三档取历史时 SQL 的 LIMIT 分别是 2/4/6**（用包一层的 Spy 抓真实参数，因为
  `sqlite3.Connection.execute` 是只读属性，无法 monkeypatch）。
* 全量 `unittest discover -s tests`：**1290 条全绿**；两项自检通过。


## 批次二十六：按 QQ 版补齐「模型与 API」四项（超时/重试/计费/单价）2026-10-17，1.2.37

用户截图指出 QQ 版设置页有「回复超时 + 超时后自动重试一次」和「计费方式」，
要求微信版按同样的样子来（`微信的token优化按qq的来就这样`）。

原来微信版这几项**只存在于配置文件**（`ai.timeout: 20`、`ai.max_retries: 2`），
界面上看不到；计费方式/单价则完全没有。

| 层 | 改动 |
| --- | --- |
| `src/wechat_app.py` | `Settings` 新增 `request_timeout(3~300)`、`auto_retry`、`billing_mode(token/request)`、`unit_price`；保存时映射到 `ai.timeout` / `ai.max_retries`（开=2、关=1）/ `ai.billing_mode` / `ai.unit_price`；`public_settings()` 回读；新增 `billing_estimate()` 并把 `billing` 放进 `status()` |
| `wechat_gui.py` | 「模型与 API」新增：回复超时（秒）+「超时后自动重试一次」、计费方式（按 token / 按请求）+ 单价（元/百万 token 或 元/次）；「模型 / API」卡片副标题改成"测试结论 ｜ 已用 N token / M 次请求 ≈ ¥x.xxxx" |
| 样式 | 表单是 `QVBoxLayout`，用 `field()` 收行；组合控件包一层 `QWidget`（直接 `addRow` 会 AttributeError —— 本轮踩过） |

**估算口径**（与 QQ 版一致）：`token` 模式 `total_tokens/1e6*单价`；`request` 模式 `请求次数*单价`；
单价留 0 时只报用量、`cost=None`（卡片不显示金额）。

**为什么值得做**：用户看着"用了多少 token"没有成本感；而且「自动重试」正是上一版
"识图把消息拖过期"的一半原因——现在可以一键关掉。

### 验证

* `tests/test_api_billing.py`（11 条）：默认值、保存/回读、引擎 `max_retries/timeout` 跟随、
  超时钳制 3~300、两种计费口径、单价 0 不折算、`status()` 带 billing、界面回填/收集、
  卡片副标题含用量与金额（单价 0 时不显示 ¥）。
* 全量 `unittest discover -s tests`：**1287 条全绿**。


## 批次二十五：识图不再拖到消息过期 + 人设导入的"黑框" 2026-10-17，1.2.36

### 一、图片识别太慢，消息被拖"过期"

用户截图里连续出现「等待桌面恢复期间消息已过期，避免迟到回复」，回复内容也全是
"没看清那张图"。原因有两层：

1. `AIEngine.chat` 按 `max_retries`（默认 3，配置里 2）重试，中转站一次视觉请求就要 20 秒
   —— 识图挡在回复前面，一次重试就 40 秒，整条消息直接过期；
2. 识图没有任何硬超时，慢的时候只能干等。

改法：

* `AIEngine.describe_images()` 内部改成 `retries=1`（**识图只试一次**），`chat()` 新增
  `retries` 参数覆盖本次尝试次数；
* `image_understanding` 新增配置 `visionTimeout`（默认 15 秒，3~120），用
  `asyncio.wait_for` 卡死；超时写 `图片识别超时（超过 N 秒），本轮直接回复文字，不编造画面`，
  然后**照常回话**，不再让模型瞎猜"没看清"。

实测（假装视觉接口 10 秒不返回）：`visionTimeout=3` 时 3.05 秒收尾、不产生描述、
状态如实写失败。

### 二、导入人设弹窗是"黑框黑字"

`_import_persona()` 用的是 `QInputDialog.getText(...)` 静态调用——它**不套用应用样式**，
在深色/半透明主题下输入框渲染成黑底黑字。

改法：改成实例化 `QInputDialog`，显式 `setStyleSheet(DIALOG_STYLESHEET)`、设置图标与
输入模式，并**预填文件标识**（省得每次手打）。其它 `QFileDialog` 是系统原生对话框，不受影响。

### 验证

* `tests/test_persona_import_dialog.py`：断言对话框拿到的是 `DIALOG_STYLESHEET`、
  预填的是文件标识。
* 识图超时用一次性脚本验证（3.05s 收尾、状态诚实、无编造）。
* 全量 `unittest discover -s tests`：**1276 条全绿**。


## 批次二十四：从 QQ 版搬"省 token"功能 2026-10-17，1.2.35

用户要求把 QQ 版（1.6.31）那边的省 token 功能拉过来。

### 先补契约：插件可以限制本轮输出长度

微信原来 `run_on_pre_ai` 只收 `delay/block/extra_system/on_ready`，`max_tokens`
固定取人设值。现在：

* `manager.run_on_pre_ai` 收集 `max_tokens`，**多插件取最小**（省 token 场景里"更严"总安全）；
* `wechat_app._work` 用 `min(budget, hints['max_tokens'])` 调模型，**撞句重生成也用同一个预算**
  （第一版漏了这里，重生成会回到人设的 1200，实测序列里看出来的）。

### 新增插件

| 插件 | 默认 | 作用 |
| --- | --- | --- |
| `minimal_mode` 极简模式 | 关 | 档位 off/极简/极限/敢死：裁历史（保留最近 N 条）+ 封顶输出 + 追加"说短话"纪律；管理员**私聊**发 `极简开 / 极简关 / 极限 / 敢死 / 极简` 切换，口令走被动回复、不经模型 |
| `token_saver` Token 节流 | 开 | 把更早的历史压成一行摘要、截断超长单条、删掉完全重复的轮次；**只在超出预算时动** |

三条底线（测试钉住）：**最后一条永远保留**（它就是要回应的内容）；正常长度对话一个字不改；
摘要只截原文、不调模型。

### 没搬的三个（说明理由，避免以后重复评估）

* `early_finish`：它省的是"接话判定调用"和"AI 选表情调用"——微信这两处都不存在（判定随
  `context_chime` 退役，选表情是本地轮转不调模型），搬来只会多一个空跑的正则；
* `cache_stability`：微信的人设在前、插件说明在后，稳定前缀本来就成立；且 `ai_engine.TokenUsage`
  没有缓存命中字段，搬来只能记个假命中率；
* `group_cost_report`：那是**统计**不是节省，且微信没把每轮用量落库，属于另一个改动（见
  `QQ功能移植对照表.md` 第 20 条）。

### 验证

* `tests/test_token_saving.py`（15 条）：裁历史保留末条、短对话不动、档位持久化、
  口令仅私聊+管理员、封顶取最小、token_saver 的摘要/截断/去重/预算兜底、以及"多插件取更严"。
* 真机链路（mock 微信 + 假模型）实测模型收到的 `max_tokens` 序列为 `120`（人设 1200 被封顶）。
* 全量 `unittest discover -s tests`：**1275 条全绿**。


## 批次二十三：拍一拍回复 / 偷表情 / 关键词导入导出 2026-10-17，1.2.34

用户报三件事：关键词要能一键导入导出、戳一戳回复有问题、偷表情没实现。

### 一、「戳一戳」（拍一拍）——原来被当成链接卡片

实测微信 4.1.15.13：「拍一拍」是 `文件/链接/卡片` 类型，正文是

```
<msg><appmsg><title>"站高速看夕阳" 拍了拍 "怪怪." Ciallo～(∠・ω&lt;)⌒</title>…
```

以前它走"链接卡片"分支，`media[].text` 只留 200 字 XML 残片，模型看到的就是乱码 → 回复莫名其妙。

`src/wechat_transports.py` 新增 `_pat_title()`：

* 抽出干净文本 `[拍一拍] A 拍了拍 B`（去掉 XML、HTML 转义与尾巴上的自定义后缀）；
* **只有"被拍的正是机器人或它的别名"** 才置 `at=True`（别的人互拍不会让机器人插话）；
* 普通 appmsg 卡片不受影响。

### 二、偷表情——表情现在会真的进图库

`src/plugins/sticker_steal.py`：

* `on_observed()` 重新**登记群里别人的「动画表情」**（以前直接 return，只在"要回复这条"时才收图片）；
* 表情走**消息 XML 里的 CDN 直链**（`fetch_image`），不再走加密的本地文件；
* 结论：表情（emotion）**永远收**（几十 KB、无解密），普通图片仍**只在要回复时**收
  —— 保持 1.2.2x「不替群里所有照片做后台下载」的决定（对应既有用例
  `test_untriggered_images_do_not_start_background_downloads` 仍为绿）；
* 表情不再套用"小图才收"的尺寸阈值（它本身就是表情包）。

真机验证：用那条真实表情跑 `on_observed → drain`，图库新增 1 条
`收集_5f93d45560  png 300×300  94734 字节`。

### 三、关键词一键导入 / 导出

`src/gui/keyword_rules.py` 的规则编辑器新增 **导入… / 导出…**：

* 导入支持四种写法：`关键词=回复` 文本、`关键词|回复`、`{"format":..,"rules":[{keyword,reply}]}`、
  `[{"keyword":..,"reply":..}]` 或 `[["关键词","回复"]]`；容忍 BOM、注释行、空行；
* **先解析校验、再一次性替换**（坏文件不会把现有规则弄坏），替换前弹确认框；
* 导出可选 `.txt`（`关键词=回复`，回复里的换行写成 `\n`）或 `.json`（带 format 字段）。

### 验证

* 新增 `tests/test_pat_steal_keyword.py`（11 条）：拍一拍文本/是否点名/别名/普通卡片不受影响；
  表情经 CDN 收集、无直链跳过、回复路径图片仍走数据库且未点名不下载；四种导入格式、
  BOM/注释容错、四类坏输入拒绝、重复关键词拒绝。
* 全量 `unittest discover -s tests`：**1260 条全绿**。


## 批次二十二：回复里的"空行"去掉 2026-10-17，1.2.33

用户实测截图：机器人一条回复里两段之间**空一行**，看起来像多了很多空格。

原因：`tidy_reply()` 只做了"去行尾空白"，**保留了换行**；而模型习惯用"空一行"分段，
于是气泡中间就空了一截。

改法（`src/reply_text.py`）：

* 新增 `join_lines()`：把所有行合并成一段；**中文直接连**（"了。要是…"读得通），
  只有英文/数字边界之间补一个空格（否则 `hello\nworld` 会粘成 `helloworld`）；
* `tidy_reply()` 改为调用它，顺序：压中文间空格 → 去句内复读 → 合并换行 → 去首尾空白。

实测（用用户截图里那两条原文）：

```
'…眼皮也瞬间打架了。要是主人想睡了…'   （不再有空行）
'…有护盾的。不过……要是主人愿意…'      （不再有空行）
'hello\nworld' → 'hello world'          （英文不粘连）
```

验证：`tests/test_reply_text.py` 更新为"段落合并 + 英文边界留空格"（18 条全过）；
全量 **1249 条全绿**，两项自检通过。


## 批次二十一：没开会话也能发 / 拦截原因写清楚 2026-10-17，1.2.32

用户报了两台机器上的两件事（并明确分开：第一张是本机，后两张是另一台）。

### 一、本机：`账号窗口已匹配，但未找到聊天输入控件`

1.2.31 的控件树其实已经物化（`visited=85`，能看到 `mmui::ChatSessionList / ChatDetailView /
XValidatorTextEdit`），但**微信当时没有打开任何聊天** → 没有 `mmui::ChatInputField`。
而 `send_text()` 的顺序是"先 `ensure_window()`（要求输入框存在）→ 再判断/打开会话"，
于是这种情况**永远走不到自动打开那一步**，直接报"未找到聊天输入控件"。

| 文件 | 改动 |
| --- | --- |
| `src/wechat_send_worker.py` | `send_text()` / `send_picture()`：`ensure_window()` 失败且允许自动打开时，**先 `open_target_chat(who)` 再重试定位**（搜索框在没开会话时也在树里，所以能打开） |
| `src/wechat_window.py` | `inspect_window(..., auto_open_chat=False)`：识别"控件树正常但没有会话详情页"（`no_chat_open`）；开关打开时判 **ready=True** 并说明"发送时会自己打开并校验会话名"；关闭时提示去打开聊天或打开该设置 |

### 二、另一台机器：`收 7 / 发 0`，全部"回复管理策略未放行"

那句话没有任何可用信息。`reply_allowed()` 拆成 `reply_decision() -> (bool, reason)`，
原因写进运行记录；并补上**原本完全静默**的两个提前返回：

| 场景 | 现在的运行记录 |
| --- | --- |
| 群不在「回复群列表」 | `未回复：这个群不在「回复群列表」里（群：X；在设置 → 回复群列表添加它，或清空列表表示全部群）` |
| 私聊回复关闭 | `未回复：私聊回复已关闭（设置 → 微信接入 → 回复私聊消息）` |
| 暂停回复 | `未回复：机器人处于「暂停回复」状态（机器人管理页可恢复）` |
| 点名闸门 | `未回复：点名闸门未放行（群未在名单内 / 发送者在忽略名单 / 本群不是仅 @ 才回）` |
| 私聊名单 | `未回复：私聊白名单/黑名单未放行（插件：私聊管理）` |
| 群聊监控关闭 / 回复开关关闭 | 分别点名"群聊监控没有开启" / "群聊监控里的「回复」开关是关闭的" |

### 验证

* `tests/test_reply_decision.py`（7 条）：私聊默认放行；私聊关闭点名设置位置；群不在名单点名群名；
  群在名单放行；暂停；群聊监控回复开关；**原因确实写进 `runtime.events`**。
* 全量 `unittest discover -s tests`：**1247 条全绿**。


## 批次二十：能读「动画表情」了（CDN 直链）2026-10-17，1.2.31

### 用户报的现象

> 还是不会读表情和图片内容

截图里机器人连着回"读不懂的波浪纹""读不懂的密信""这个动画表情打不开"。查微信数据库，
用户发的那几张**全都是 `动画表情` 类型**（local_id 38/40/42/44），而识图链只处理 `type='image'`：

```python
images = [item for item in message["media"] ... if item.get("type") == "image"]
if not images:
    message['vision_status'] = '失败：微信加密动画表情无法从本地库导出…'
```

也就是说：表情**从来没进过视觉模型**，模型只看到占位符 `[表情]`，于是自己编"读不懂"。

### 破局点：表情消息 XML 里有 CDN 直链

微信 4.x 的本地表情文件是加密的（`MediaDownloader` 也没有表情下载接口），
但消息正文是 XML，里面带 `cdnurl` / `md5` / `len`。实测：

```
cdnurl="http://vweixinf.tc.qq.com/110/20401/stodownload?m=e4a5…&amp;filekey=…"
→ HTTP 200、41510 字节（与 XML 里的 len 一致）、文件头 FF D8 FF E0（JPEG）
```

**不用解密、不用截图、不用点面板**，直接下原图。

### 改了什么

| 文件 | 改动 |
| --- | --- |
| `src/wechat_transports.py` | `message_from_raw`：`emotion`/`image` 的 XML 段解析出 `url`（`cdnurl`/`thumburl`，**还原 `&amp;` 转义**）、`md5`、`size`。图片也带上 url —— 本地导出失败时可当兜底 |
| `src/plugins/image_understanding.py` | 新增 `_remote_source()`：用 `fetch_image`（公网校验 + 大小上限）取直链，再用 `frames_for_vision` 归一成 PNG data URL；`_local_source()` 先走直链，**表情不再去试加密的 `download_media`**（那条路只会白等 60 秒）；`describe()` 把"带直链的表情"当图片处理；状态文案改成"另有表情没有可下载的地址" |

### 真机验证

用那条真实表情（local_id 44）跑完整链路（真实 CDN + 用户自己的模型）：

```
vision_status = ok
vision_description = 以下按顺序对应原消息第 1 张图片：
  一个蓝发Q版（二头身）人鱼少女，穿着黑白相间的女仆装，头上有光环，身后有鱼尾。
  她双臂交叉抱胸，嘴巴微微撅起，眼神看起来有点嫌弃或无语，显得有些傲娇。
```

即"表情 → XML → CDN 下载 → 视觉模型 → 描述进上下文"整条通了。
（接口第一次 20 秒超时、重试成功，说明中转站偏慢，与识图无关。）

### 验证

* `tests/test_emotion_vision.py`（7 条）：XML 解析出 url/md5/size 且转义还原；
  图片也留兜底 url；纯文本不变；带直链的表情会调视觉模型且**不再调 download_media**；
  没有直链时如实说明且不调模型；本地导出失败会回落到直链；直链失败不编造画面。
* 全量 `unittest discover -s tests` 全绿；两项源码自检通过。

### 仍未解决

* **微信内置小黄脸**（`[表情:微笑]` 那种 `face` 类消息）在微信 4.x 里只有文本占位、
  没有图片、也没有 CDN 链接，仍然读不到 —— 它本来也不是图片。
* 直链是腾讯 CDN，链接带时效（`filekey`）；同一张表情**过很久再点开**可能 403，
  此时会如实写"没有可下载的地址/下载失败"，不会编造内容。


## 批次十九：自动打开目标会话（用户明确要求）2026-10-17，1.2.30

### 用户要求

> 还是没有输出啊？你参考最初的程序这样，自动打开微信，搜索框搜索发送微信这样

1.2.29 让发送**终于能执行**了，但暴露出下一道门：`send_text()` 要求
"当前打开的会话必须就是目标会话"，否则直接拒绝 —— 实测日志：

```
发送接口返回失败：当前不是目标会话，请手动打开对应聊天；不会自动搜索或切换会话，
本次未输入正文；阶段：核对会话；发送键：未执行
```

### 改了什么

`src/wechat_send_worker.py` 新增 `open_target_chat(uia, who, phase)`：调用库自带的
`WeChatUIA.open_chat()`（搜索框输入 → 收结果 → 优先精确匹配 → 点击 → **校验会话标题**），
并在打开后**再校验一次**，名字对不上就绝不输入正文。

| 文件 | 改动 |
| --- | --- |
| `src/wechat_send_worker.py` | `send_text(..., open_chat=False)` / `send_picture(..., open_chat=False)`：会话不对且开关打开时先 `open_target_chat()`，失败给出可操作原因（含"没能自动打开会话「X」"） |
| `src/wechat_sender.py` | `IsolatedWeChatSender(..., open_chat=<取值函数>)`，任务带 `auto_open_chat`；取不到按 False（fail-closed） |
| `src/wechat_bridge.py` | `auto_open_chat` 属性（**默认 True**），以函数传给发送器 |
| `src/wechat_app.py` | `Settings.auto_open_chat`（默认 True）、`public_settings()`、保存与启动时写入桥 |
| `wechat_gui.py` | 设置页「微信接入」新增 **自动打开目标会话（按名字搜索并打开后再发送）**，tooltip 说明"打开后仍校验会话名" |
| `src/wechat_window.py` | 检查口径修正：**最小化不再判成"发送未就绪"**（真正发送的 `prepare_window()` 会先恢复窗口），改为 ready + 提示"发送时会自动恢复窗口" |

### 这是对一条旧红线的有意放宽（用户明确要求）

`AGENTS.md` 第二节原先写着"发送前不切会话"（怕键鼠点面板误发）。用户现在明确要求
恢复"最初的程序"行为，故放宽为：**允许用搜索框搜索并打开目标会话，但打开后必须校验会话名，
对不上仍然不输入**。开关名 `wechat.auto_open_chat`（默认 true），关掉即回到旧行为。

### 真机验证（本次是真的发出去了）

提权后用源码调用生产发送路径（`IsolatedWeChatSender(..., compat=True, open_chat=True)`
→ `SendMsg(text, '文件传输助手')`）：

```
ok = True
message = 正文已写入，发送键执行后输入框已清空
```

再**只读**查微信数据库核对（`wechatauto.db.WeChatDB.get_messages('filehelper')`）：

```
{'create_time': 1792215305, 'type': '文本'} | 自检：发送通道测试（13:35:00），可忽略
```

即"搜索 → 打开 → 校验 → 输入 → 回车 → 落库"整条链路真实可用。

### 验证

* `tests/test_auto_open_chat.py`（8 条）：开关关→拒发且不打字；开关开→打开后发送；
  打开到别的会话→拒发；打开失败→可操作提示；打开抛异常→不外泄；已在目标会话→不重复打开；
  任务带开关；默认值与异常取值 fail-closed。
* `tests/test_send_compat.py`（8 条）+ 全量 `unittest discover -s tests` 通过。


## 批次十八：发送根本发不出去（微信 4.x 无障碍门）2026-10-17，1.2.29

> 批次十七末尾那句"真实微信发送阻塞未恢复"——**批次十八就是修这个的**。

### 用户报的现象

> 这个实际上并没有输出

截图（1.2.28）：控制台「微信接入」= **读消息在线 · 发送未就绪**，原因"账号窗口已匹配，但未找到聊天输入控件"；
「消息与回复」里 13:12 的几条回复全是 `等待` → `cancelled`；微信窗口里**只有对方的消息，一条回复都没有**。

### 根因：微信 4.x 的 UIA 控件树默认不生成，而我们把"唤醒"这一步禁掉了

后台 `/api/test-wechat` 的诊断 `send_environment`：

```
candidate_count: 1  matched_count: 1  minimized: false
uia_class: Qt51514QWindowIcon
input_lookup: { visited: 3, search_status: "exhausted",
                classes: ["MMUIRenderSubWindowHW", "Qt51514QWindowIcon"], errors: 0 }
input_available: false
```

整棵树只有 3 个节点。原因是微信自己的机制：4.x 主界面自绘，**只有 `Weixin.dll`
里一个"Qt 无障碍门"字节为 1 时才会物化 `mmui::*` 控件树**。只读确认：

```
微信 4.1.15.13 · Weixin.dll 基址 0x7ff8aae00000 · 门 RVA 0xb135c38 → 字节 = 0
```

而 `src/wechat_send_worker.py` 的 `restrict_driver()` **故意**把
`_wake_accessibility()` / `_hot_activate_accessibility()` 换成 `lambda: False`
（"不许库修改微信进程"）。两者叠加：`mmui::ChatInputField` 永远不存在 →
`prepare_window()` 失败 → 运行时把每条回复判成"发送未就绪"并 `cancelled`
→ **这台机器上真实发送从未成功过一次**（以往所有"已发送"都来自 mock 后端）。

也验证了"不碰内存"的替代：只开 Windows 读屏标志（`SPI_SETSCREENREADER=1`）后
重新检查**完全一样**；库文档也写明"冷启动时 UIA 树只暴露空壳"。只剩"打开那个字节"。

### 改了什么（做成显式开关，默认关）

| 文件 | 改动 |
| --- | --- |
| `src/wechat_send_worker.py` | `restrict_driver()` 改为**返回**被禁用的两个入口；新增 `enable_send_compat(uia, entrance)`：只在"唤醒"这一次调用里**临时放行** `_hot_activate_accessibility`，结束立刻恢复（异常也恢复）。`window-check` 在开关打开时也先唤醒——否则"检查说未就绪 → 发送要等检查通过"会死锁 |
| `src/wechat_sender.py` | `compat=<取值函数>`，每个任务带 `send_compat`；取不到按 False（fail-closed）；新增 `_prepare_job()` 便于测试 |
| `src/wechat_bridge.py` | `send_compat` 属性（默认 False），以函数形式传给发送器，改配置不必重连微信 |
| `src/wechat_window.py` | `inspect_window(..., send_compat=False)`；**修掉 `shell_only` 的正则 bug**（真实类名是 `MMUIRenderSubWindowHW`，原来 fullmatch 永不匹配）；原因文案改成可操作 |
| `src/wechat_app.py` | `Settings.send_compat`、`public_settings()['send_compat']`、保存/启动时写入桥 |
| `wechat_gui.py` | 设置页「微信接入」新增 **发送兼容模式** 勾选框 + 实时红/绿状态提示，tooltip 写明只改 1 字节、写后校验失败会回滚 |

**语义**：关 = 能收、能生成，但**不向微信发送任何内容**（继续"不修改微信进程"）；
开 = 发送前把那个字节置 1（读屏软件唤醒 Qt 无障碍树的标准机制；不注入 DLL、不改磁盘文件、
写完立即用"控件树是否出现"校验，失败回滚；微信重启后字节归零，每次发送会重新唤醒）。

### 验证

* 真机（微信 4.1.15.13）：把门字节**复原成 0** 后——开关关闭时检查报
  "微信 4.x 默认不生成控件树…请到设置 → 发送兼容模式"；开关打开时**子进程自己打开门**：
  `materialized: true`、`input_available: true`、`uia_class: mmui::MainWindow`、当前会话可读、输入框可定位。
* `tests/test_send_compat.py`（8 条）：只放行一次且立刻恢复、异常不外泄、缺方法时 fail-closed、
  任务带开关、检查原因随开关变化。
* 全量 `unittest discover -s tests`：**1224 条全绿**；两项源码自检通过。

### 注意

* `send_text()` 的硬规矩不变：**当前打开的会话必须是目标会话**（不自动搜索/切换会话）。
  所以真实发送验证要先在微信里手动打开目标会话。
* 微信 4.x 的 `emotion`（动画表情）内容加密，仍拿不到字节（见批次九）。


## 批次十七：关键词唤醒设置填写引导（2026-10-17，1.2.28，验收中）

- 唤醒词设置专用布局：去掉内部英文标识，词表置顶，添加页首完整填表示例、每项可见解释、推荐下拉选项、追加示例按钮和保存前摘要。明确关键词叫醒 AI，与固定答案不同；明确人设词表优先、空值不等于关闭、策略关闭恢复原规则。
- 示例仅修改草稿，保留原词且不重复；单字提醒和保存校验沿用后台要求。关闭相关开关时禁用无效输入，不丢草稿。固定底部操作，窄窗口可滚动。配置字段及值不变，不重置原词表、人设或历史。
- 新增 9 项界面回归，实际截图检查正常与 520×440 窄窗口；源码后台、HTTP、完整测试及 GUI 验收结果见《关键词设置界面验证记录-1.2.28.md》。真实微信发送阻塞未恢复，不把表单改进作为收发已修复的证据。

## 批次十六：真实 @ 元数据恢复与等待状态（2026-10-17，1.2.27，未发布）

- 真实数据库确认 source 为存于 TEXT 的 Zstd 二进制，依赖库既未把 source 交给监听，又通过 text_factory 损坏其字节。仅对带 @ 且缺少稳定名单的群消息，在原查询器、分片、local_id、sort_seq 下只读 CAST AS BLOB 后有限解压；坏数据不丢消息，普通私聊和闲聊不增加查询。五条历史样本通过实际适配入口验证：四条 @ 本账号正确，一条 @ 他人不触发。
- 限定窗口内的控件查找改为浅层优先，兼容深度 40；保留 2000 节点/时间预算。诊断区分外壳、丰富控件树未找到输入与搜索预算，不再一律误报空外壳。等待中的消息过期时清除等待阶段，避免空队列仍显示等待。
- 经用户授权临时启动系统讲述人只读复查，实际确认窗口存在，但机器人账号仍只有三项外壳节点；随后确认讲述人已关闭。未重启微信、未输入或发送，未放宽内存/读屏标记/会话切换红线。完整结果见《核心功能修复验证记录-1.2.27.md》；真实发送未验收前不发布安装包。

## 批次十五：核心回归修补与真实验证（2026-10-17，1.2.26，未发布）

- 复现并修复图片说明 @ 被丢失、明确 @ ID 未使用、同名昵称前缀误判、OpenAI 内容块被转为列表 repr、可选缓存/图库写入失败让识图成功变失败、插件 enabled 异常打断后续 post_reply/tick。
- 窗口不可用时仍让过期队首进入既有 expired 流程，避免永久占队。OCR 与视觉在同一模型锁内并行准备，对话构造前完成，描述只注入一次；原功能与用户配置保留。新增 12 项回归。
- 两轮全量各 1196 项通过，两轮真实本机 HTTP/Qt 界面验收通过；真实 API 四模型文字和形状识图成功，数字精读有误，不声称模型识别无误。真实模型＋模拟微信完整识图回复：Fast 3.047s、Deep 7.922s。
- 真实微信窗口已唯一绑定但无输入控件，未进行外发。遵守用户先完全跑通再打包的本轮条件，暂不发布 1.2.26。不可把 1.2.25 的冻结/升级验证冒充本版结果。证据见核心功能修复验证记录。

## 批次十四：账号窗口绑定与发送就绪诊断（2026-10-16，1.2.25）

- 真实只读检查发现两个不同账号的微信主窗口，原发送端未收到读库选定的账号。新增账号目录传递，以候选 PID 实际打开的 db_storage 文件唯一匹配；不能匹配或存在歧义时拒绝，不选择前台/首个窗口，不关闭其他微信。
- 同时确认机器人窗口只暴露三个外壳控件，无聊天输入框；标准 Windows 无障碍查询也没有暴露输入框，进程权限级别相同。用户确认开启讲述人后，本程序可见会话仍无讲述人进程及读屏状态，不能据此断定用户未操作，也不能声称已解决控件兼容性。
- 新增只读 window-check，测试微信和启动区分读库在线与发送就绪；已知无输入控件时暂停模型生成，每五秒复查。最小化保留既有发送恢复路径，锁屏仍等待。未执行发送键的窗口/控件失败不触发数据库重连；原生超时、崩溃和发送后不确定结果保留原约束。
- 新增双账号选择、错误账号/权限/路径边界拒绝、只读检查、进程协议、连接分类、队列暂停及恢复回归；冻结发送自检增加双账号替身校验。更新保留正式 AppId、数据目录和历史，覆盖安装验证以本轮最终报告为准。真实检查未输入或发送，实际发送兼容性尚未恢复。

## 批次十一：同题多答、停止卡顿与精简（2026-10-16，1.2.22）

- 确认两处可重现缺陷：`start()` 的 running 检查发生在 await connect 前，多次请求可创建多套任务；worker 没有领取状态，多个 worker 可同时生成同一条 pending 的不同回复。新增生命周期锁及 `pending -> processing` 条件领取。
- 同事件按 server_id/sort_seq 与稳定账号去重，兼容旧键。停止先关发送、杀本程序的发送子进程、丢弃未发送队列，重启不补发；发送中断仍 uncertain，不删除历史。旧监听回调按实例隔离，退出等待有上限。
- 产品移除九项附加插件（34→25），旧设置保留；旧源码继续用于历史离线测试，冻结不携带。保留模型去重与用户最后确认的自动表情包，每轮一张；打字标记不再拆条。图库仅收集已触发回复的图片，后台闲聊不下载。
- 发现依赖 UIA 唤醒会写微信进程内存，禁用热激活、全局读屏标记、自动登录，控件不可用明确失败。发图旧路径会截图探测输入框、重复粘贴，已替换为 UIA 单次粘贴/确认/单次发送；父进程拥有临时剪贴板，子进程被终止也会恢复，用户新复制优先保留。
- 回归覆盖并发启动、双 worker、分片/昵称变化、准备中停止与重启、发送进程取消、发送间隔中停止、旧回调丢弃、图片上限及剪贴板恢复。原测试保留，更新被用户明确移除行为的期望：旧分段不再拆条、停止后取消队列、收集测试改走回复钩子；未通过降低断言来规避真实缺陷。
- 仅模拟微信/模型、控件与剪贴板替身及隔离覆盖安装验证；未连接受保护真实账号。无法据截图证明账号保护根因，也不承诺恢复该账号。

## 批次十：沿用 1.2.20 修复识图、动图与相关异常（2026-10-16，1.2.21）

- 确认当前基线为昨天 DeepSeek 修改并打包的 1.2.20，保留本地图库、自动收集、回复整理、等比例缩放。
- 聊天识图接入图库已有 GIF/WebP 转 PNG 抽帧能力，均匀读取动图首尾，不让动图占掉后续图片位置；成功描述回填已收集条目，不额外收集私聊。
- 只读下载改用 `tier='best'`，优先本机已有原图。复用同数据库下载器并串行保护，按账号/会话/分片隔离导出目录；传输层共享下载任务及短时成功缓存，失败不缓存。
- 加密 emotion 明确写入失败状态，混合图文不谎称全部看过；仅缩略图不写完整识别缓存。视觉失败映射为不含密钥或 URL 的分类诊断，图库失败保留人工说明。
- 补齐 `PluginBase.on_observed` 空实现，修掉新版每条消息反复输出 AttributeError；图片像素上限在 load 前检查，防止先占内存再拒绝。
- 自动收集在媒体尚未落盘时有限重试；按账号区分待收集指纹，切换后不下载旧账号队列。发送串行、间隔、25 秒超时不重发等约束不变。
- 新增媒体回归测试；冻结自检使用真实 GIF 字节验证共享下载、PNG 抽帧、识别回填与加密表情诊断。完整测试、打包及旧版带数据覆盖验证结果以本轮交付为准；没有真实微信外发。

> 本文件只记录**改了什么 / 为什么 / 怎么验证 / 还剩什么**。新改代码前请先读这里，避免重复劳动或误删。
> 与 AGENTS.md 的铁律一致：每次行为改动都要重新打安装包。

## 批次九：表情库对齐 QQ 版 + 回复文本 + 等比例缩放（2026-10-15，版本 1.2.20）

### 用户要求

> 然后这个微信表情库和qq不同，他并没有这个保存别人发的图片的功能，也无法识别表情包，
> 也不触发自动恢复表情，还有聊天的时候文本也有问题，回答重复度很高，一次回答两次语句还有空格，
> 解决以上的问题然后再加一个可以等比例拖拉窗口大小的功能，表情库参考第一张qq界面的图。

前置澄清（用户后补）：第一张图（「表情包管理 · 机器人本地图库」）是 **QQ 版**的界面，
微信版当时**完全没有这个窗口**——`src/` 里搜不到 `stickers` 路由、没有图库表、
没有"使用次数/缩略图/AI识别这张图"任何一处实现。

### 一、机器人本地图库（新增，对照 QQ 版 `sticker_library/sticker_api/gui.stickers`）

| 层 | 新文件 / 改动 | 说明 |
| --- | --- | --- |
| 存储 | `src/sticker_library.py`（新） | `index.json` + `images/`，主键 = 图片字节 sha256；去重累加 `seen`、`uses` 只增不减、LRU 淘汰（数量+容量）、标签校验、路径穿越防护、SSRF 防护（解析到内网/本机一律拒）、**索引带 `.bak`** |
| 接口 | `src/sticker_api.py`（新） | `GET /api/stickers?q=&page=`（每页12）、`POST /api/stickers/import`、`POST /api/stickers/{id}`、`POST /api/stickers/{id}/recognize`、`DELETE /api/stickers/{id}` |
| 界面 | `wechat_gui.py` 新增 `StickerLibraryDialog` | 预览/标签/图片含义/使用次数四列，上传、刷新、翻页、搜索、保存、AI 识别、删除；入口在**插件页顶部按钮**与 `sticker_admin`/`sticker_steal`/`image_library` **三张插件卡**上 |
| 插件 | `src/plugins/sticker_admin.py`（新） | 开关 + 动图抽帧数；关掉后接口返回 403 |
| 插件 | `src/plugins/sticker_steal.py`（新） | 自动收集别人发来的图片；默认**开启**、默认**不收私聊**、黑名单、只收小图/动图、数量与容量上限 |
| 插件 | `src/plugins/image_library.py` | 图库 = 手动登记 + 自动收集合并（`all_entries()`，同名手动优先）；发送成功后按 id 计"使用次数" |
| 插件 | `src/plugins/sticker_cadence.py` | 从"只提示模型加 emoji"改成**每 N 次成功回复真的附一张图库图片**（轮转取图，已有图不重复追加，失败不计数） |
| 运行时 | `src/wechat_app.py` | 建库 `self.stickers`（读不出来时置 None + 可读原因）、挂载路由、新增"已观察"钩子调用 |

**为什么必须新增"已观察"钩子**：`on_ingest` 只在**机器人准备回复**的消息上触发
（在访问策略之后），而群里别人的表情大多没点名——收不到就永远攒不下图库。
所以 `PluginManager.run_on_observed()` 在 `ingest()` 里、访问策略**之前**调用，
插件只登记"看到了哪张图"（同步、不下载），真正的下载交给 `tick`/`on_prepare` 这些
async 钩子分批做，不阻塞入站线程。

**微信侧的三处硬限制（已在界面与文档写明，不假装支持）**：

1. **"动画表情"拿不到字节**：微信 4.x 的表情在本地库里是加密的
   （第三方库 `wechatauto.msgtype` 注释明说无法还原，`MediaDownloader` 也没有表情下载方法），
   所以 `emotion` 类型**明确跳过**，不去做必然失败的下载。好消息是好友发的大部分"表情包"
   其实以 `图片` 类型到达，那些照收不误。
2. **`stickerOnly` 只能下载后判断**：微信消息段里没有尺寸信息，先取图再决定存不存。
3. **缩略图用 Pillow 生成**，后端不 import PyQt6 —— 避免冻结包缺图片插件时把合法图判成损坏。

### 二、回复文本（用户报"重复度高、一次回答两次语句、还有空格"）

调查结论（`src/reply_text.py` + `wechat_app.py`）：
**代码不会往中文里插空格**（全仓库 `join(' ')` 零命中），多余空格与句内复读都是**模型自己吐出来的**——
实测模型输出 `"你好 呀，今天  天气不错  "` 被原样发进微信，因为发送链**故意不做 strip**
（`tests/test_pacing_markers.py` 钉住了"无标记分支原样发送"）。

| 改动 | 文件 | 为什么 |
| --- | --- | --- |
| 新增 `tidy_reply()`：压掉**汉字之间**的半角空格、去句内重复句、去首尾空白 | `src/reply_text.py`（新） | 只动中文之间的空格，英文单词/代码缩进/颜文字里的空格保持不变；重复句只在**≥6 个实义字符**时去重，"哈哈""嗯嗯"这类语气重复不受影响 |
| 发送前统一整理 | `src/wechat_app.py` | 放在"模型返回值 → 插件收尾"这一段，不动发送层（发送层的原样发送是有测试保护的） |
| 生成后查重 → **换一次说法** | `src/wechat_app.py` `_avoid_repeating()` | 原来只有"下一轮提示词提一句"的软约束，实测压不住 0.95 温度下的复读；现在撞句时带着"禁用句"重生成**一次**，再撞就认了（宁可重复也不让用户等第三条） |
| 提示词窗口放大 | `src/plugins/reply_dedupe.py` | 原来只放前 5 条、每条截 40 字，6 小时窗口里只剩 200 字；改成 8 条 × 80 字，并新增 `recent_replies()` 供查重 |
| 停用 `chat_controls` 不再静默丢配置 | `src/wechat_app.py` | `options = controls.settings if controls and controls.enabled else {}` 会让用户设的 `historyTurns` 无声变回 6 |

### 三、等比例缩放窗口

`src/gui/window_sizing.py` 新增 `keep_ratio()` 纯函数 + 四角比例约束：

* 外观页新增开关「等比例缩放窗口」（存在 `gui_config.json` 的 `proportional_resize`），
  **按住 Shift 可临时生效**，拖四边仍是自由缩放；
* 比例约束**不能交给系统**：原来 `startSystemResize()` 一旦接手，拖动过程就不归我们，没法逐帧约束，
  所以等比例模式下改为自己算几何；
* 写测试时抓到并修掉一个真实缺陷：锚点原本取"自由拖动后的矩形"，拖"上+右"角时
  下边会被一起拽走（实测 `699 != 899`），窗口整体偏移。锚点必须取**起始几何**。

### 四、核对过的"疑似问题"（不做改动，避免错改）

调查阶段有两条结论经复核**不成立**，这里记下来免得后人重踩：

1. "发送超时后 `consecutive_fail` 永不累加"——**不成立**。
   `bridge.send_text()` 无论成败都会走 `_after_send()`（`wechat_bridge.py:812`），
   超时路径另有 `mark_send_timeout()` 也会累加，守护重连是通的。
2. "`bridge.send_text` 会 `exact=True` 失败后 `exact=False` 再发一次"——**不成立**，
   当前实现每次只调一次；`wechatauto` 的 `SendMsg` 里 `exact` 参数本身没被使用。

仍然存在、但**本轮未动**的风险（需要真实微信才能判定）：
第三方 `wechatauto` 的 `ChatBox.send_text` 在输入框只残留 U+FFFC 时会**递归重发**同一段正文
（`chatbox.py:141-150`）。正文带 emoji 或 @对象时可能触发，属第三方行为，
本项目没有回读消息列表做去重；如用户再遇到"整条重复两次"，优先查这里。

### 五、验证

* `unittest discover -s tests`：**1092 条全绿**（新增 `tests/test_reply_text.py` 16 条、
  `tests/test_sticker_library.py` 32 条、`tests/test_sticker_dialog.py` 9 条）；
* 两项源码自检通过；
* 打包 + 冻结自检 + 安装包往返验证（含旧版带数据覆盖升级）见本轮交付说明。

测试里顺带修掉的两个**真实缺陷**：
① 导入接口遇到坏图会**部分导入**（好图已入库才报错）→ 改成先逐张解码校验、再统一入库；
② `/表情 标签` 发送成功却不计"使用次数"（`_reply_meta` 没带 `library_ids`）→ 补上。

## 批次八：模型名带空格被误拦（2026-10-15，版本 1.2.19）

### 用户报的现象

> 这个无法识别空格，但这个模型需要空格。

截图里的「测试连接」输出（1.2.18）：

```
模型列表: 10 个模型 · 594 ms
当前模型在列表中: 否
对话请求: 失败 · 391 ms
失败原因: HTTP 403: 接口权限不足；服务端说: 模型「TM-Auto」当前不可用……
模型名称里有空格，看起来是服务商界面上的显示名，不是 API 的 model id。……
```

最后那句是**我们自己加的**，而用户的服务商（`https://workbuddy.tokenmao.cn`）给出的
model id 本身就带空格。

### 根因：1.2.0 加的"防呆"判据是错的

1.2.0 为了修"模型名抄成网页显示名 → 404"加了一条启发式：
`AIEngine.model_id_problem()` 里 **只要模型名含空格就判为填错**。它被用在三处：

| 位置 | 后果 |
| --- | --- |
| `src/ai_probe.py` | 「测试连接」直接返回失败，**根本不发请求** —— 用户连试都试不了 |
| `src/wechat_app.py` `save()` | 「保存全部配置」抛 `模型名称里有空格…`，**存不进去** |
| `src/wechat_app.py` `status()` | 「模型 / API」卡片一直挂着这条提示 |

`chat()` 的 404 分支还会再附一句同样的猜测。

当时的假设是"主流服务商 model id 都不含空格"。这条假设对中转/聚合服务不成立，
而用户恰恰用的是这类服务。**另外**：服务端原话已经在 1.2.1 起被带出来了
（`服务端说：…`），比我们的猜测准得多，这条启发式已经没有必要。

### 改了什么

| 文件 | 改动 |
| --- | --- |
| `src/ai_engine.py` | `model_id_problem()` 不再检查空格，只拦空值与 >120 字符；注释写明"实测有中转服务 id 自带空格" |
| `src/ai_engine.py` | 404 报错去掉"模型名里有空格…"那句猜测，保留"模型 + 接口 + 服务端原话" |
| `src/wechat_app.py` | `save()` 与 `status()` 的注释同步更正（行为随之放宽） |
| `tests/test_wechat_recovery.py` | 原"空格必须被拒"的用例改为**"带空格的 id 必须放行"**（含 `TM DeepSeek V4` 等 4 个样例）；`SaveRejectsBadModelTests` 改为 `SaveModelNameTests`：断言带空格能存进 `ai.model`，**空值仍被拒**且拒绝时配置与连接原样；404 用例断言带出服务端原话、且不再出现"显示名"字样 |
| `instructions/WECHAT-CONTRACT.md` | 约定改为"只校验非空 + 长度，不许按形状猜；GUI 只做首尾 strip，不得改动内部空格" |
| `微信版使用说明.md` | 「模型名称怎么填」与 404 排错行改为"不同服务商的 id 由它自己决定，带空格也合法"，并新增 1.2.19 说明；1.2.0 那节标注更正 |

### 验证

- `unittest discover -s tests`：**1029 条全绿**。
- 打包 + 安装包往返验证（含**旧版带数据覆盖升级**、快捷方式图标、桌面启动、后台异常重连、
  20025 条历史保留）：见本轮 `verify_installer.ps1` 输出。

### 顺带说明

截图里的 `模型「TM-Auto」当前不可用` **不是本项目发出去的模型名**（全仓库搜不到 `TM-Auto`），
也不是本项目的文案——那是中转服务自己的返回。用户界面上的模型名输入与「读取可用模型」
下拉填充都只做首尾 `strip()`，不会吃掉内部空格。

## 批次七：新增「关于作者」页（2026-10-15，版本 1.2.16）

### 用户要求

> 然后在帮我在控制台这里新加一页：关于作者，作者：林夕，头像：是第二个图，qq:2019117802
> 特别感谢：参与制作的用户：你携秋水揽星河。声明：未经作者允许禁止贩卖、转卖。

（用户随后更正用词：是「**转卖**」，不是「专卖」。）

### 改了什么

- `wechat_gui.py` 顶部新增常量，作者信息集中一处：`AUTHOR_NAME`、`AUTHOR_QQ`、
  `AUTHOR_BLOG`（留空则不显示该行）、`THANKS_USERS`、`COPYRIGHT_NOTICE`；
- 新增 `AboutTab`（五张卡片：关于作者 / 声明 / 特别感谢 / 关于本程序 / 使用须知），
  在 `MainWindow` 里注册为最后一页「关于作者」（图标取图标库的 `app` 槽位）；
- 头像资源 `assets/author/linxi.jpg`：原图 1997×1122，按人脸位置裁 1:1 再缩到 320×320
  （约 25 KB）；运行时从 `BUNDLE/assets` 读取并裁成圆形（四角透明 + 描边）；
- 「复制 QQ 号」「复制版本信息」两个按钮（后者便于用户反馈时附上版本与数据目录）。

### 为什么这样放

- **做成独立一页而不是弹窗**：联系方式、版本信息、免责声明、"哪些事这个程序不会做"
  都是用户会回头翻的内容；
- **声明显眼**：放在作者卡片正下方（红字），这是用户最在意的一条；
- **头像走 `BUNDLE`**：打包后资源在 `_MEIPASS`，写死源码目录会在装完之后显示空白 ——
  这条专门有测试钉住（`tests/test_about_page.py::test_avatar_is_bundled_and_loads_as_a_circle`，
  并断言四角透明，防止样式表圆角"看起来圆、实际露方底"这个坑）；
- **版本号只有一个来源**：页面显示 `APP_VERSION`，不写死。

### 怎么验证

```powershell
.\.venv-wechat\Scripts\python.exe tools\selfcheck_backend.py
.\.venv-wechat\Scripts\python.exe tools\selfcheck_http.py
.\.venv-wechat\Scripts\python.exe -m unittest discover -s tests
powershell -NoProfile -ExecutionPolicy Bypass -File build_installer.ps1
powershell -NoProfile -ExecutionPolicy Bypass -File tools\verify_installer.ps1   # 安装往返（含冻结资源）
```

另有 `tests/test_about_page.py` 8 项：页面确实显示作者/QQ/致谢/声明、文案单一来源、
头像随包且裁成圆、版本取自 `runtime_identity`、两个复制按钮写进剪贴板、
页面不依赖在线内容、头像缺失时退化成文字不崩、标签栏最后一页就是它。

### 还剩什么

- 用户博客地址尚未提供（`AUTHOR_BLOG` 留空 → 页面不显示"博客"一行）；拿到地址填上即可。

## 批次六：快捷方式图标（2026-10-14，版本 1.2.3）

### 现象与根因

用户截图报"这个图标有问题"：桌面上「鲸鱼娘 微信控制台」显示的是 Windows 的**通用齿轮图标**。

查桌面快捷方式的实际属性：

```
鲸鱼娘 微信控制台.lnk
  目标 : %LOCALAPPDATA%\Programs\WhaleGirlWeChat\启动（管理员）.cmd
  图标 : ,0        ← 空的
```

根因是 `installer.iss` 的两条 `[Icons]` 指向的是 `.cmd` 启动器，而**快捷方式不会从 `.cmd`
继承图标**（`.cmd` 自己就没有图标），于是 Windows 画通用图标。而"直接指向 exe"又不行：
exe 是**刻意不提权**的（`wechat_bot.spec` 里 `uac_admin=False`，避免每次启动都过 UAC），
提权靠 `.cmd` 里那句「已提权就直接启，否则 `PowerShell -Verb RunAs`」。

### 改了什么

`installer.iss` 的两条 `.cmd` 快捷方式补上显式图标（exe 里已经嵌了 `icon.ico`）：

```
IconFilename: "{app}\WhaleGirlWeChat.exe"; IconIndex: 0
```

* 桌面「鲸鱼娘 微信控制台」
* 开始菜单「鲸鱼娘 微信控制台（管理员启动）」

开始菜单里"普通启动"那条本来就指向 exe，图标一直是好的，未改动。

### 验证

- 重新打包 + 静默覆盖安装后，读回快捷方式属性确认 `图标` 不再是空（见下面的实际输出）。
- 刷新 Windows 图标缓存（`ie4uinit.exe -show`），避免 Explorer 继续显示旧图标。

## 批次五：关控制台不再杀微信 + 账号可指定（2026-10-14，版本 1.2.2）

### 真凶：`disconnect()` 会把用户的微信强杀

在 1.2.0 的真实连接测试里，输出里混进了一行 `SUCCESS: The process with PID 2768 has been terminated.`
——那是 `taskkill` 的成功回显。顺着查：

| 位置 | 代码 | 说明 |
| --- | --- | --- |
| `.venv-wechat/.../wechatauto/wx.py:1634` | `def ShutDown(self): ... os.system(f'taskkill /f /pid {pid.value}')` | 名字像收尾清理，实际是**强杀微信主进程** |
| `src/wechat_bridge.py` `disconnect()`（旧） | `for closer in ("Close", "ShutDown"): getattr(self._wx, closer)()` | 挨个调一遍 → 每次都把微信杀掉 |

调用 `disconnect()` 的地方有两处，都很常见：

* `WeChatTransport.close()` / `WeChatRuntime.close()`：**关掉控制台**（uvicorn 收尾）就会走到；
* `bridge.reconnect()`：**连接守护每次自动重连**都会先 disconnect。

后果链（正好就是用户最初报的三条症状）：

```
关控制台 / 自动重连
  → taskkill 微信
    → 微信重启中，数据库还没被打开
      → active_database_account() 判不出来 → 退回"按 .db 修改时间挑"
        → 挑到已退出的旧账号（西莲寺紫）
          → 收不到消息、发送接口返回失败 = "连不上微信 / 连接不成功 / 无法回复信息"
```

而且"按 `.db` 修改时间挑"还有恶性循环：**我们自己打开某个旧账号的库会更新它的 `-wal`/`-shm`
时间**，于是旧账号看起来越来越"新"，一路错下去。

### 改了什么

| 文件 | 改动 | 原因 |
| --- | --- | --- |
| `src/wechat_bridge.py` | `disconnect()` 只做"停监听 + 放引用"，**不再调用任何客户端方法**；注释写明永远不要碰 `ShutDown`/`Kill`/`Exit`/`Quit` 这类名字 | 上面那条真凶 |
| `src/wechat_bridge.py` | 新增 `newest_message_account()`：按"哪个账号还有会话在动"兜底（会话时间是我们读库不会改变的量） | `.db` 文件时间会被我们自己读库污染 |
| `src/wechat_bridge.py` | 新增 `choose_account()` + `read_account_hint_record()`/`write_account_hint(source)`：账号按"**设置里指定 → 进程打开的库 → 可靠记忆 → 会话时间 → 弱记忆 → 库启发式**"排序，并把**依据**写进运行记录；多账号目录时明确提示"不确定请用管理员启动" | 选错账号的表现是"连上了却收不到消息"，必须让用户看得见依据、也能自己指定 |
| `src/wechat_bridge.py` | 新增 `resolve_account_dir()` / `describe_accounts()` | 支持用户填账号目录名或 wxid；列出本机账号（含昵称、"正在登录"标记） |
| `src/config_manager.py` | `wechat` / `wechat_only` 增加 `account: ""` | 新增设置项，留空=自动 |
| `src/wechat_app.py` | `Settings.account`、`save()` 写入并纳入 `WECHAT_SETTING_KEYS`、`GET /api/wechat/accounts` | 设置页要能选账号，改账号要重连 |
| `wechat_gui.py` | 设置页新增「机器人使用哪个微信号」下拉 + 「读取本机微信账号」按钮（走 `call_api`，不阻塞界面） | 用户指定账号 |
| `tests/test_active_wechat_account.py` | 新增 `DisconnectSafetyTests`（假客户端带 `ShutDown`，断言**没被调用**）、`AccountChoiceTests`、`ConfiguredAccountTests`、`NewestMessageAccountTests` | 把这条纪律与排序钉死 |
| `tests/test_wechat_recovery.py` | 新增 `AccountSettingTests`、`WeChatAccountsRouteTests` | 设置项与接口 |

### 验证

- `unittest discover -s tests`：**850 条全绿**（787 + 28 + 12 + 23）。
- **真实微信回归（关键证据）**：用修好的代码 `connect()` → `start_listening()` → `disconnect()`，
  前后微信进程列表完全一致（`8996,9860,20864,21548,28836` 全部存活）、主窗口句柄仍在、
  日志里没有 `taskkill`；同时 `connect -> True`、`start_listening -> True`，
  账号判定依据打印为「微信进程打开的库」→ 选中当前登录的 `wxid_2rc4z7dz591922_c185`（西莲寺紫）。
- 修复前的同一条路径会打印 `SUCCESS: The process with PID 2768 has been terminated.` 并让该微信实例消失
  —— 这就是 1.2.0/1.2.1 里仍然存在的现场证据。

### 需要向用户说明的

诊断期间（修复前）那次真实连接测试**确实杀掉了用户的一个微信实例**（PID 2768）。
微信数据未被破坏（各账号目录、消息库都在），重新打开微信即可；这属于排查动作造成的副作用，
已如实告知。

### 用户账号现状

`active_database_account` 本次返回 `wxid_2rc4z7dz591922_c185`（昵称"西莲寺紫"），
即当前登录的是西莲寺紫；两个账号目录（西莲寺紫 / 鲸鱼娘）都有近期会话活动，
所以"同时登录"或"刚切换过"都可能，自动判断以**进程打开的库**为准。
设置页可随时改成指定账号。

## 批次四：模型服务兼容（2026-10-14，版本 1.2.1）

### 背景：Key 换对了，回复还是全失败

用户给了一个可用的 Kimi Key（`sk-…`，51 字符，`GET /v1/models` 200：`kimi-k2.7-code`、
`kimi-k2.7-code-highspeed`、`kimi-k2.6`、`kimi-k3`）。但 `AIEngine.chat()` 依旧一条都回不出来，
日志里只有一句 `HTTP 400：请求参数或模型不兼容`。按 RPM=3 的节奏逐条手工复现后，剥出三层问题：

| # | 实测证据 | 影响 |
| --- | --- | --- |
| 1 | `{"error":{"message":"invalid temperature: only 1 is allowed for this model"}}` | **所有** Kimi 思考模型只接受 `temperature=1`；人设发的是 0.9/0.95，于是每条消息都 400 |
| 2 | 同样载荷去掉 `temperature` → 200，但 `max_tokens=100` 时 `finish_reason=length` 且正文为空、`reasoning_tokens` 已占 74~305 | 思维链算在 `max_tokens` 里；人设给 100（`styles/custom/tsundere_whale_girl.yaml` 覆盖了内置的 300）→ 正文必空 |
| 3 | 429 `request reached organization max RPM: 3, please try again after 1 seconds` | 账号**每分钟只允许 3 次请求**；按服务端说的等 1 秒重试仍超限（滚动一分钟窗口） |

三者叠加的一个副作用：`HTTP 400：请求参数或模型不兼容` 把服务端那句 `invalid temperature: …`
**吞掉了**，所以原本一眼能看出的问题查了很久。

### 改了什么

| 文件 | 改动 | 原因 |
| --- | --- | --- |
| `src/ai_engine.py` | `_service_error_message()`：取出服务端 `error.message` 并附到 `last_error` | 不再吞掉服务端的诊断信息 |
| `src/ai_engine.py` | `_no_temperature` 集合 + `_build_payload` 按模型跳过 `temperature`；`chat()` 遇到 400 temperature 拒绝时去掉该字段**重发且不消耗重试次数** | 实测 Kimi 思考模型只接受 1；每条都先撞一次 400 既慢又吵 |
| `src/ai_engine.py` | `_read_completion()` 返回 `(正文, finish_reason)`；正文为空且 `length` 时把额度 ×3（上限 8192）重发一次 | 思维链吃光额度时，原实现只会报"模型未返回内容" |
| `src/ai_engine.py` | `_rpm_from_message()` + `_wait_for_rate_slot()`：从 429 报文里学 `max RPM`，在本地按滚动窗口排队；429 退避取 `max(Retry-After, 3×2^attempt)` | 账号上限只有 3 RPM 时，靠撞 429 重试是撞不过去的；排队虽慢但能回出去。429 的报错里也写明"上限是每分钟 N 次、充值可提高" |
| `src/ai_engine.py` | 重试循环由 `for range(max_retries)` 改为显式 `while` + `attempt`，429 的等待时间可覆盖默认退避 | 上面两条"免费重发"要在不消耗次数、不退避的前提下生效 |
| `styles/tsundere_whale_girl.yaml`、`styles/default.yaml` | `max_tokens` 300 → 800 | 思考模型的思维链会把它吃光 |
| `styles/custom/tsundere_whale_girl.yaml` | `max_tokens` 100 → 1200 | 这份 `custom/` 覆盖了同名内置文件，是**实际生效**的那份；100 对任何思考模型都必然空回复 |
| `tests/test_ai_service_compat.py` | **新增 12 条测试** | temperature 自愈（含"只重发一次、后续不再带"）、额度放大（含"只放大一次"、"真空回复不重试"）、RPM 解析与本地排队、429 提示带上限与充值建议、服务端原话进 `last_error` |

### 验证（都是真实 API，按 3 RPM 排期）

- `unittest discover -s tests`：**827 条全绿**（原 787 + 批次三 28 + 批次四 12）。
- `AIEngine.chat(temperature=0.9, max_tokens=100)`（完全复刻当时的配置）→ 自动"去 temperature → 放大额度"，
  返回 **"在呢！说呗～"**。
- **真实端到端**（用户真实配置 + 模拟微信后端，避免动桌面）：群消息 `@鲸鱼娘 你昨天说的那个电影叫什么`
  → 插件链路 → 真实 Kimi 模型 → 发送，状态 `sent`，回复
  **"哼，本鲸鱼娘昨天明明在海底睡觉，才没说过什么电影呢……主人是不是认错鲸鱼了？"**，
  `usage` 显示 `completion_tokens=668`（印证 100 的额度必然不够）。
- 429 路径实测：报文里读到 `max RPM: 3`，退避从服务端说的 1 秒改为 ≥3 秒，并在报错里写明上限与充值建议。

### 用户账号现状（要告诉用户）

速率上限 **3 RPM** 是账号等级决定的（官方文档：累计充值金额决定等级与限速）；余额 ¥9.42 全部是代金券、
`cash_balance=0`。程序侧的排队能让它"慢但能回"，要真正顺畅需要充值提高等级。

## 批次三：界面卡死 + 连不上微信 + 不回消息（2026-10-14，版本 1.2.0）

### 现场证据（先读这段，别再从零查）

用户装的是 **1.1.0 安装包**（注册表 `DisplayName=鲸鱼娘 微信控制台 1.1.0`，安装目录文件时间 20:56~21:00），
而源码已经到 1.1.2 —— **1.1.1（等待超时）与 1.1.2（账号选择）两个修复从没进过安装包**。
用户日志 `%LOCALAPPDATA%\WhaleGirlWeChat\logs\startup.log` 21:31~21:33 段显示：

- 同一分钟内 `WeChatGUI 初始化成功` 出现 **4 次**（每次 15~20 秒的窗口定位 + 布局校准）；
- 账号先连成 `西莲寺紫`、后连成 `鲸鱼娘` —— 本机 `Documents\xwechat_files` 下有**两个账号目录**
  （`wxid_2rc4z7dz591922_c185`=西莲寺紫、`wxid_rerhua6zafil22_7ba3`=鲸鱼娘），
  1.1.0 没有 `active_database_account()`，只能按 `.db` 最近修改时间挑，会挑到已退出的旧账号；
- `[wechat] 发送失败（wxid_2rc4z7dz591922）：发送接口返回失败` + `HTTP 404：接口地址或模型不存在`
  —— 配置里 `ai.model: Kimi K2.7 Code` 是服务商网页上的**显示名**，不是 model id；
- `wechatauto` 日志里两次 `自动最小化遮挡窗口 1 个（累计待还原 1 个）`，而项目**从不调用**
  `release_foreground()` / `restore_blockers()` —— 被按下去的窗口（可能包含控制台自己）永不还原。

### 改了什么

| 文件 | 改动 | 原因 |
| --- | --- | --- |
| `wechat_gui.py` | 新增 `ApiJob` / `TaskJob` / `call_api()` / `_run_api()` / `_run_task()`；把设置读写、人设、插件清单与开关、插件安装检查、读取群列表、模拟对话**全部**改为后台线程执行 | 1.1.0 的同步 `requests` 跑在 Qt 主线程，后端一慢整窗冻结；1.1.1 只改了启动/停止/测试四项，其余仍是同步 |
| `wechat_gui.py` | `_start_backend` 拆成 `ensure_backend()` + `apply_backend_probe()` 并丢到后台线程 | 原来在主线程 `for _ in range(20): sleep(0.25)` 等旧后台退出，最多冻 5 秒 |
| `wechat_gui.py` | 结果派发用 `job.done.connect(job.deliver)`（`deliver` 是 job 自己的方法） | 信号连到普通 lambda 时 Qt 会在**工作线程**直接执行回调，跨线程碰控件会随机崩 |
| `wechat_gui.py` | 设置页新增「读取可用模型」按钮 + 下拉框；控制台卡片显示「正在连接微信…」；`_load_settings` 改异步 | 模型名填错没有出路；连接期间界面必须有话说 |
| `wechat_gui.py` | 人设删除改用 `DELETE /api/personas/{name}`（原来先 POST 再退化成裸 `requests.delete`） | 裸调用绕过统一错误处理，失败原因被吞 |
| `src/wechat_bridge.py` | `_connect_wechatauto` 账号三级选择（进程打开的库 → `account_hint.json` → 库启发式），并记录库**最终选中**的账号 | 多账号机器上会连到已退出的旧账号 → 收不到消息、发出去也失败 |
| `src/wechat_bridge.py` | 新增 `read_account_hint` / `write_account_hint` / `_restore_minimized_windows`；`_after_send` 里调用还原并作废 `_last_visible_*` 缓存 | wechatauto 发送前最小化重叠窗口且不还原；作废缓存是为了下一次发送重新前置微信，否则键鼠会点到别的窗口上 |
| `src/wechat_bridge.py` | `status()` 增加 `account_dir` / `restored_windows` | 用户自查"连的是哪个账号" |
| `src/wechat_app.py` | `save()` 拆出 `WECHAT_SETTING_KEYS` + `_same_setting()`：只有微信接入配置真变了才 `stop()`/`start()` | 只为改模型名就断开重连微信二十秒 —— 用户看到的就是"卡死 + 连不上"。`_same_setting` 还要处理配置缺键读到 `None` 与界面提交 `""`/`[]` 的差异，否则每次保存都会被判成"改了" |
| `src/wechat_app.py` | 新增 `_note_account_switch()`：账号切换**连续命中两次**才重连；`_health` 在 `connecting`/`reconnecting` 期间不再发起重连 | psutil 单次读微信进程句柄会抖动，一次抖动就重连 = 界面反复卡二十秒 |
| `src/wechat_app.py` | `start()` 暴露 `connecting` / `last_connect_ms`；`status()` 增加 `connecting` / `reconnecting` / `last_connect_ms`，并把"模型名像显示名"顶到 `ai_test` | 界面要能说明"正在连接"而不是看起来死了 |
| `src/wechat_app.py` | 新增 `WeChatRuntime.list_models()` 与 `GET /api/models`；`test-wechat` 输出增加「账号目录」与"监听没在跑"的提示 | 让用户能拿到真能用的 model id、能看出账号对不对 |
| `src/ai_engine.py` | 新增 `model_id_problem()`（拒绝带空格的模型名）与 `list_models()`；404 报错带上模型名与接口地址 | `Kimi K2.7 Code` 这类显示名请求必然 404，而原来的报错只有一句"接口地址或模型不存在" |
| `src/gui/smoke.py` | 新增 `_pump()`，人设/插件/控制台三处等后台结果回来再断言 | 请求挪到后台线程后，同步断言会跑在结果之前（断言一个都没删） |
| `tests/test_keyword_editor.py` | 假 `api_post` 补 `timeout=10` 形参 | `_request` 现在总是显式传 timeout |
| `tests/test_wechat_recovery.py` | **新增 28 条回归测试** | 覆盖上面每一条：窗口还原与缓存作废、账号记忆、切换去抖、AI-only 保存不断连、模型名校验、模型列表解析（含 401 的提示必须指向 Key 而不是"服务商没这个接口"）、`/api/models` 路由、`call_api` 分发、回调在主线程、配置自备份与恢复 |

### 顺手补的：配置自备份（事故驱动）

排查期间实测发现本机 `%LOCALAPPDATA%\WhaleGirlWeChat` 被**整体清空**过一次
（`config.wechat.yaml`、`data/`、`wechatauto_logs/` 全没了，只剩 `logs/` 与重建出来的 `styles/`）。
用金丝雀文件验证过：**1.2.0 的静默安装与静默卸载都不会删用户数据**（金丝雀全部存活），
所以不是安装包干的；真实原因未能确定（用户机器上的清理工具、或手工操作都可能）。
为让这类丢失可恢复：

- `wechat_app.save()` 覆盖前先把当前配置复制成 `config.wechat.yaml.bak`；
- `WeChatRuntime.__init__` 发现主配置不在而 `.bak` 在时自动取回，并在运行记录里写明；
- 已用早先读到的内容把用户当时的配置（含加密的 API Key、50 个群名、触发词）**原样还原**。

### 验证

- `tools/selfcheck_backend.py`：通过（插件 31，端到端发送 1，routes 29）。
- `tools/selfcheck_http.py`：全部通过。
- `unittest discover -s tests`：**815 条全绿**（原 787 + 新增 28）。
- `wechat_gui.py --self-test`：通过（含 6 张界面截图：控制台/设置/人设/插件/上传/关键词），
  即冻结自检的同一套断言。
- `tools/verify_installer.ps1`：静默安装 → 装好的程序自检 → 静默卸载，全部通过。
- 用户数据金丝雀实验：静默安装、**覆盖安装（升级路径）**、静默卸载之后，
  `%LOCALAPPDATA%\WhaleGirlWeChat\CANARY.txt`、`data\wechat-only\CANARY.txt` 均存活，
  `config.wechat.yaml` 哈希不变 —— 安装包确实不动用户数据。
- 用**用户自己还原后的配置**跑 TestClient 端到端：`/api/runtime` 版本 1.2.0、数据目录正确，
  `/api/settings` 读到 `kimi-k2.7-code` 与 50 个群，`/api/models` 返回 401 且提示指向 API Key。

## 批次一：卡顿 + 聊天死板（2026-10-14）

### 改了什么
| 文件 | 改动 | 原因 |
| --- | --- | --- |
| `src/wechat_app.py` | `inbox` 表建 `idx_inbox_created`、`idx_inbox_state` 两索引 + 启动时 `DELETE` 保留最近 2 万条 | `status()` 每 3s `ORDER BY created DESC` 全表扫描，跑得越久 UI 越卡；`_work()` 的 `WHERE state='pending'` 也受益 |
| `wechat_gui.py` | 新增 `_StatusPoller(QThread)`，3s 轮询的同步 `requests` 挪出 Qt 主线程；`closeEvent` 停定时器 + `wait(2000)` | 后端慢时整窗冻结 |
| `src/style_manager.py` | `prompt_text()` 放宽"最多 2 句 / 60 字"死约束 → "长短随语境"风格指引 | 把人设压成复读机，回复死板 |
| `styles/tsundere_whale_girl.yaml` | temperature 0.7→0.95、max_tokens 200→300 | 更有变化 |
| `tests/test_style_prompt.py` | 同步更新 `REQUIRED_RULES` 锚点（保留同等意图） | 上面文案变更 |

**坑（已踩）**：`inbox` 清理最初用 `now-14天` 绝对时间，误删测试里的模拟时间戳（`created=1234.0`）。改成"相对最新记录 OFFSET 20000"后解决，且不再依赖系统时钟。

## 批次二：模拟模式 + 回复去重 + 人设鲜明（2026-10-14）

### 改了什么
| 文件 | 改动 | 原因 |
| --- | --- | --- |
| `src/wechat_bridge.py` | `_record_mock_send` 追加写 `data_dir/mock_sent.jsonl`；`status()` 暴露 `mock_sent`（最近 20 条，仅 mock 后端） | mock 发送可本地翻查，像真实发送 |
| `src/wechat_app.py` | 新增路由 `POST /api/mock/recv`、`GET /api/mock/sent` | GUI 能直接推假入站消息，端到端离线验证 |
| `wechat_gui.py` | `ConsoleTab` 加"模拟对话"面板（mock 后端才显示），输入框 + @我 + 发送 → 调 `/api/mock/recv` | mock 真正可用，不连微信也能看回复 |
| `src/plugins/reply_dedupe.py` | **新插件**：`on_sent` 记每会话最近 N 条回复；`on_pre_ai` 注入"你最近回过这些，换个说法" | 旧版 4 层防重复，当前只有入站去重，**回复侧**会复读 |
| `src/plugins/manager.py` | `PLUGIN_MODULES` 注册 `reply_dedupe` | 插件生效 |
| `src/style_manager.py` | `prompt_text()` 末尾加"身份锁 + 本轮回复优先级"（动态带 display_name） | 人设被通用规则稀释成客服腔，易被对话/注入带跑 |

### 验证
- 语法 OK；`tools/selfcheck_backend.py` 通过；`unittest discover` 774 全绿。
- TestClient 冒烟：`/api/mock/recv`、`/api/mock/sent` 200；身份锁正确注入且末尾位置；无人设名时不注入。
- `reply_dedupe` 插件数 30→31，`run_on_sent` 闭环确认在 `wechat_app.py:633` 被调。

### 用户当前配置的结论（2026-10-14 22:06 实测）

用他配置里保存的密钥实际请求过两家端点，结论写在这里免得下一个人重复排查：

| 检查项 | 实测结果 | 结论 |
| --- | --- | --- |
| `https://platform.kimi.com/v1/models` 与 `/chat/completions` | **HTTP 404**，响应头是 `X-Powered-By: Next.js`、`x-internal-adhoc-canary` | `platform.kimi.com` 是**文档/控制台网站**，不是 API 端点。填它必然 404 |
| `https://api.moonshot.cn/v1/models`（用配置里的 Key） | **HTTP 401** | 这个 Key 不是**开放平台**的 API Key（官方文档：开放平台 / Kimi Code / Kimi 会员三套 Key 互不通用）。需要用户到开放平台控制台重建 Key |
| `ai.model` 原值 `Kimi K2.7 Code` | 带空格的显示名 | 官方文档里对应的是 **`kimi-k2.7-code`**（模型列表页给出的 id）。已按文档改成这个 id |
| 官方仍在售的模型（2026-10） | `kimi-k3`、`kimi-k2.7-code`、`kimi-k2.7-code-highspeed`、`kimi-k2.6` | `moonshot-v1-*`、`kimi-k2-*`、`kimi-latest`、`kimi-k2.5` **均已下线**，别再推荐 |

也就是说"无法回复信息"是**两个原因叠加**：模型名是显示名（404）+ Key 不是开放平台的（401，
这一条只能用户自己去控制台重发一个 Key）。代码侧的护栏已经加好：保存时拒绝带空格的模型名、
`GET /api/models` 能一键拉真清单、404/401 的报错都带上"用的是哪个模型/哪个接口"。

来源：<https://platform.kimi.com/docs/models>（模型列表与已下线清单）、
<https://platform.kimi.com/docs/api/list-models.md>（接口地址 `https://api.moonshot.cn/v1` 与 401 说明）、
<https://platform.kimi.com/docs/guide/troubleshooting>（三套 Key 不通用）。

## 残存问题（给后续 agent 优先处理）

1. **「配置不成功」根因未定，需用户现象**：全局 `ai.temperature`/`ai.max_tokens` 在 `wechat_app.py` 的 `_work()` 里被 `float(persona.temperature or 0.7)` 盖死，且**设置页没有 temperature/max_tokens 入口**——用户想调 AI 语气/长度却配不动。
   - 建议：`_load_style_file` 里人设缺 temperature 时回落 `config.get("ai.temperature")`；设置页加两项。**动手前先问用户"配置不成功"具体指什么现象/报错。**
2. **`_work()` 的过期阈值是 `max(max_age, 60)`**：配置项 `wechat.max_message_age: 25` 实际被 60 秒下限顶掉。是有意留的安全余量（别把刚到期的消息丢掉），但和配置项的字面意思不一致，要在文档或代码里说清。
3. **发送失败仍可能是"微信窗口被别的窗口挡住"**：`_restore_minimized_windows` 解决的是"我们把窗口按下去"，挡在前面的**用户自己的**窗口仍会让键鼠点不到。真要做稳，得在发送前用 UIA 校验一次输入框焦点。
4. ~~**尚未重打安装包**~~：批次一~三都已进 **1.2.0** 安装包，并跑过 `tools/verify_installer.ps1` 往返验证。
5. ~~**`tools/selfcheck_http.py` 假 404**~~：1.2.0 复跑已全部通过（29 条路由），不再是噪声来源。
6. **封号红线核查**：批次三改的全是连接/界面/配置链路，**没有**新增任何外发通道，未调 `SendToLabel/Forward/Moment/Poke/Recall/AtAll`，未引入主动群发；`/api/models` 只向用户自己配置的模型服务发一次只读 GET。

## 1.1.0：参照 QQ 版优化长截图阅读

在现有图片识别插件增加可选长图切片、分段上限和未读区域提示。解码限制 3200 万像素，最多三段、一次视觉调用；切片在线程中运行，局部结果不污染整图缓存。新增真实图片切片与插件管线回归测试。

验证时发现旧 GUI 测试仍 mock `requests.get`，而产品已经使用 `_PROXY_FREE` 会话；测试改为 mock 实际会话，所有原断言保留。

打包自检定位并修复：构建环境 PATH 中 Poppler 的 `icuuc.dll` 被误收集，与 Qt 所需的 Windows ICU 导出接口不兼容。spec 排除系统已有的同名 ICU 库，交给 Windows 加载。

## 1.1.1：控制台等待超时修复

启动、停止、测试 AI、测试微信与保存配置改为后台线程等待，最长等待 120 秒，界面可继续刷新。请求超时会明确提示操作可能仍在执行，避免误以为启动失败并重复点击。操作进行中暂不关闭窗口，以免销毁仍运行的线程。微信发送通道的 25 秒超时及不重发规则不变。

## 1.1.2：切换微信账号后的消息接收

本机存在多个账号目录时，优先根据正在运行的微信进程打开的数据库选账号；切换账号后由连接守护检测并重新连接监听。拿不到进程文件信息或同时运行多个账号时，保留库的自动选择方式。监听启动失败会明确报错，避免显示运行中却没有监听；重复注册同一接收回调不再重复投递。
# 1.2.23

- 修复会话名称回退为群发言者或原始群 ID；当前账号备注优先、发送前刷新，未解析或重名时不操作窗口。
- 新增跨盘数据目录选择与层级规范化，支持账号/db_storage、中文空格、环境变量及旧配置发现；显式路径无效时拒绝自动换库。
- 补齐冻结程序 win32ui 的 MFC 依赖，新增原生依赖离线体检和安装版/源码版错误指引。
- 发送失败显示阶段，超过 10 秒保留子进程堆栈；保留 25 秒超时不重发和停止清理约束。
- 继续覆盖安装，保留用户配置、人设、记忆和对话记录。

# 1.2.24

- 修复最小化微信窗口定位失败：不再以 Weixin.dll 模块枚举结果作为窗口发现前提，按已验证句柄恢复唯一最小化主窗口。
- Windows 锁屏、远程会话断开及安全桌面时等待恢复，不生成重复回复、不操作输入；解锁后复查消息过期和回复规则。
- 模型生成期间锁屏保留已生成正文，解锁后只发送一次；停止取消等待任务，失败和结果待核实记录不重发。
- 空控件树、多窗口、托盘收起和未登录分别明确提示；保持禁止 OCR、进程修改、读屏标记修改和自动登录。

# 1.2.40（2026-10-19）

- 修复账号列表忽略界面草稿目录、优先使用过期连接目录；与连接共用跨盘/账号/db_storage 发现规则，显示具体错误。
- 修复每次连接重置 send_compat 与 auto_open_chat，保存后重新检查发送环境。
- 增加系统托盘，关闭窗口继续运行，右键开启/停止/退出，双击打开；退出等待请求完成并先停止机器人。
- 按用户明确选择增加严格后台文字发送选项，保留原发送配置默认行为。只调用当前目标聊天的控件写值与唯一发送按钮，不切会话、不抢鼠标、不置前、不操作剪贴板。草稿/锁屏/最小化/缺控件时明确等待，过期不补发，发送不确定不重试；此模式跳过附加表情。
- 使用独立目录、模拟控件、真实本机 HTTP/Qt 进行回归；本机无运行中微信，真实后台收发未验证。验收与安装包记录另见《托盘与后台模式验证-1.2.40.md》。

# 1.2.50

- 本机群聊/私聊记录持久化，按账号会话隔离，最近 N 条资料与确认的长期记忆共同辅助当前问答。保留速度档角色历史上限，过滤其他群员的 assistant 历史；强调当前问题和人设优先级。
- 对话页提供本地记录查看、搜索、单条删除、清空与 JSON 合并/覆盖导入导出；删除后不从旧发送历史重新引用。升级不裁剪原历史。
- 新增用户请求触发的当前群文字总结：活跃成员、时段、主题、待解决问题；使用本机记录，无定时或私聊推送。
- 人设页显示实际生效 ID；切换失败不确认、不保存；提交发送前取消切换过程中生成的旧人设回复。
- 非默认人设不再命中写死鲸鱼娘身份的寒暄模板，交给当前人设生成；本地即时回复也绑定排队时的人设。
- 合并 1.2.49 的识图超时、透明图/方向处理和严格后台开关冲突修复。已有配置不会被自动重置。

