; 鲸鱼娘 · 微信控制台 安装包脚本（Inno Setup 6）
;
; 编译：.build-tools\InnoSetup\ISCC.exe installer.iss
;       或双击「打包.bat」（会先跑 PyInstaller 再跑本脚本）
; 版本号由构建脚本用 /DAppVersion=x.y.z 传入，缺省 1.0.0。
;
; 为什么装到 {localappdata}\Programs：不需要管理员权限即可安装/卸载；
; 而程序本身在启动时通过 exe 清单申请提权（微信 4.x 的发送需要管理员）。

#define AppName "鲸鱼娘 微信控制台"
#define AppExe "WhaleGirlWeChat.exe"
#ifndef AppVersion
  #define AppVersion "1.0.0"
#endif
#define AppPublisher "鲸鱼娘"

[Setup]
AppId={code:GetInstallAppId}
; AppId 不随版本变化，升级直接写回原路径，不先卸载旧版。
UsePreviousAppDir=yes
UsePreviousTasks=yes
UninstallLogMode=append
DirExistsWarning=no
UsePreviousLanguage=no
AppName={#AppName}
AppVersion={#AppVersion}
AppVerName={#AppName} {#AppVersion}
AppPublisher={#AppPublisher}
DefaultDirName={localappdata}\Programs\WhaleGirlWeChat
DefaultGroupName={#AppName}
DisableProgramGroupPage=yes
PrivilegesRequired=lowest
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
OutputDir=installer
OutputBaseFilename=WhaleGirlWeChat-Setup-{#AppVersion}
SetupIconFile=icon.ico
UninstallDisplayIcon={app}\{#AppExe}
Compression=lzma2/max
SolidCompression=yes
WizardStyle=modern
CloseApplications=yes
RestartApplications=no
; 卸载时保留用户数据（%LOCALAPPDATA%\WhaleGirlWeChat 里的配置、聊天记录、插件）
; —— 删掉它们等于把用户的机器人和记忆一起删了，不能默认做这种事。

[Languages]
Name: "chinesesimplified"; MessagesFile: "compiler:Languages\ChineseSimplified.isl"

[Tasks]
Name: "desktopicon"; Description: "创建桌面快捷方式"; GroupDescription: "附加任务："; Flags: unchecked

[Files]
; 只发布程序与只读资源，防止 dist 中偶然混入用户配置和对话数据库。
Source: "dist\WhaleGirlWeChat\WhaleGirlWeChat.exe"; DestDir: "{app}"; Flags: ignoreversion
Source: "dist\WhaleGirlWeChat\_internal\*"; DestDir: "{app}\_internal"; Flags: ignoreversion recursesubdirs createallsubdirs
; 管理员启动器（微信 4.x 的发送需要管理员权限，用它会自己走 UAC）
Source: "启动（管理员）.cmd"; DestDir: "{app}"; Flags: ignoreversion
; 端口被旧后台占用时的清理工具（控制台也会自动清理，这里是给用户的手动入口）
Source: "清理占用端口的旧后台.bat"; DestDir: "{app}"; Flags: ignoreversion
Source: "tools\free_port.ps1"; DestDir: "{app}\tools"; Flags: ignoreversion
; 随包文档，方便用户装完就能看
Source: "README.md"; DestDir: "{app}"; Flags: ignoreversion
Source: "微信版使用说明.md"; DestDir: "{app}"; Flags: ignoreversion
Source: "封号风险与功能取舍.md"; DestDir: "{app}"; Flags: ignoreversion
Source: "功能对照与使用说明.md"; DestDir: "{app}"; Flags: ignoreversion
Source: "构建与发布说明.md"; DestDir: "{app}"; Flags: ignoreversion
Source: "LICENSE"; DestDir: "{app}"; Flags: ignoreversion
; 控制台操作指南（给最终用户）
Source: "控制台操作指南.md"; DestDir: "{app}"; Flags: ignoreversion skipifsourcedoesntexist
; 用户使用说明（按「装好 → 配好 → 发得出去 → 出问题怎么查」写的完整手册）
Source: "用户使用说明.md"; DestDir: "{app}"; Flags: ignoreversion skipifsourcedoesntexist
; 可选插件包：主程序不会自动安装，只会把包放在这里给用户手动启用
Source: "plugin-packs\*"; DestDir: "{app}\plugin-packs"; Flags: ignoreversion recursesubdirs createallsubdirs skipifsourcedoesntexist

[Icons]
; 推荐入口放前面：微信 4.x 发送需要管理员权限，普通启动会在界面里提示"无管理员权限"
;
; 为什么要显式写 IconFilename：指向 .cmd 的快捷方式**自己不带图标**，Windows 会画一个
; 通用齿轮图标（用户实测报过"这个图标有问题"）。exe 里已经嵌了 icon.ico
; （见 wechat_bot.spec 的 icon=），所以把图标指到 exe 上就对了。
; 不能改成直接指向 exe：exe 是刻意不提权的（uac_admin=False），提权靠 .cmd 里的
; 「已提权就直接启，否则 PowerShell -Verb RunAs」，改成 exe 会丢掉提权。
Name: "{code:GetStartMenuRoot}\{#AppName}（管理员启动）"; Filename: "{app}\启动（管理员）.cmd"; IconFilename: "{app}\{#AppExe}"; IconIndex: 0
Name: "{code:GetStartMenuRoot}\{#AppName}"; Filename: "{app}\{#AppExe}"; IconFilename: "{app}\{#AppExe}"; IconIndex: 0
Name: "{code:GetDesktopRoot}\{#AppName}"; Filename: "{app}\启动（管理员）.cmd"; IconFilename: "{app}\{#AppExe}"; IconIndex: 0; Check: ShouldCreateDesktopIcon

[Run]
Filename: "{app}\启动（管理员）.cmd"; Description: "启动 {#AppName}（管理员）"; Flags: nowait postinstall skipifsilent shellexec

[UninstallDelete]
; 覆盖安装前留下的上一版 exe（回滚用）是安装包自己产生的中间文件，不是用户数据：
; 卸载时一起清掉，否则 {app} 目录会留下一个孤零零的旧程序，用户以为卸载不干净。
; 名字与 [Code] 里 ChangeFileExt({#AppExe}, '.prev.exe') 一致。
Type: files; Name: "{app}\WhaleGirlWeChat.prev.exe"


[Code]
function IsVerificationInstall: Boolean;
begin
  Result := ExpandConstant('{param:VerifyInstall|0}') = '1';
end;

function GetInstallAppId(Param: String): String;
begin
  Result := '{CC9C9945-D704-468B-94E5-30AB08B37A21}';
  if IsVerificationInstall then
    Result := Result + '_Verification';
end;

function GetStartMenuRoot(Param: String): String;
begin
  if IsVerificationInstall then
    Result := ExpandConstant('{app}\.verification-shortcuts\StartMenu')
  else
    Result := ExpandConstant('{autoprograms}');
end;

function GetDesktopRoot(Param: String): String;
begin
  if IsVerificationInstall then
    Result := ExpandConstant('{app}\.verification-shortcuts\Desktop')
  else
    Result := ExpandConstant('{autodesktop}');
end;

function ShouldCreateDesktopIcon: Boolean;
begin
  { 升级时也修复旧快捷方式，不能因为桌面选项未勾选而保留通用脚本图标。 }
  Result := WizardIsTaskSelected('desktopicon') or
    FileExists(GetDesktopRoot('') + '\{#AppName}.lnk');
end;


// ---------------------------------------------------------------------------
// 覆盖升级前的两条硬要求（1.3.51）：
//   1) 旧版本必须先退出。以前只在复制失败时弹「无法复制文件：拒绝访问」，
//      用户根本看不出是「程序还在跑」，尤其是「启动（管理员）」起的后台窗口——
//      那种进程提权了，普通权限的安装包既关不掉也复制不过去。
//      所以这里在开始安装前主动检测，并把处理办法写成中文。
//   2) 覆盖前留一份上一版 exe（WhaleGirlWeChat.prev.exe），装坏了能自己回滚。
// ---------------------------------------------------------------------------

const
  // 换行必须走这个常量：把转义的换行符写在行首会被 ISPP 当成预处理指令
  // （实测报 "Unknown preprocessor directive"），整段 [Code] 就编译不过了。
  CRLF = #13#10;

// 为什么不用 CreateFileW 独占打开来判断：那需要对 kernel32 做 external 声明，
// 32 位无符号句柄常量在 Pascal Script 里的类型推断不稳，写错一次检测就静默失效；
// 而 tasklist 只回纯文本，判据是一个 ASCII 映像名，出错也只会是「没检测到」，
// 不会误报成「程序在跑」把用户挡在安装门外。
//
// 用 tasklist 看映像名：它对权限不敏感，管理员起的后台进程也能列出来；
// 输出里只比对 ASCII 的 exe 名，所以中文系统代码页不会影响判断。
function TargetProgramRunning: Boolean;
var
  TempFile: String;
  Lines: TArrayOfString;
  I: Integer;
  Code: Integer;
  Text: String;
begin
  Result := False;
  TempFile := ExpandConstant('{tmp}\running-check.txt');
  DeleteFile(TempFile);
  if not Exec(ExpandConstant('{cmd}'),
      '/C tasklist /FI "IMAGENAME eq {#AppExe}" /NH > "' + TempFile + '"',
      '', SW_HIDE, ewWaitUntilTerminated, Code) then
  begin
    Log('tasklist 调用失败，本次跳过运行中检测（' + IntToStr(Code) + '）');
    Exit;
  end;
  if not LoadStringsFromFile(TempFile, Lines) then
  begin
    Log('读取进程列表失败，本次跳过运行中检测');
    Exit;
  end;
  for I := 0 to GetArrayLength(Lines) - 1 do
  begin
    Text := Lowercase(Lines[I]);
    if Pos(Lowercase('{#AppExe}'), Text) > 0 then
    begin
      Result := True;
      Log('检测到仍在运行的进程：' + Lines[I]);
      Exit;
    end;
  end;
end;

// 覆盖前留一份上一版 exe。已有 .prev.exe 就保留不覆盖：
// 反复升级时用新 exe 顶掉它，真正的「上一个能用的版本」就丢了。
procedure BackupPreviousExe;
var
  Target: String;
  Backup: String;
begin
  Target := ExpandConstant('{app}\{#AppExe}');
  if not FileExists(Target) then
  begin
    Log('没有旧版本 exe，跳过回滚备份（全新安装）');
    Exit;
  end;
  Backup := ChangeFileExt(Target, '.prev.exe');
  if FileExists(Backup) then
  begin
    Log('已存在 ' + Backup + '，保留不覆盖');
    Exit;
  end;
  if FileCopy(Target, Backup, False) then
    Log('已备份上一版 exe 到 ' + Backup)
  else
  begin
    // 备份失败不中止安装：装不上比「少一个回滚点」严重得多，但要当场告诉用户。
    Log('备份上一版 exe 失败：' + Backup);
    MsgBox('没能在覆盖安装前备份上一版程序：' + CRLF + Backup + CRLF + CRLF +
      '安装会照常继续，但装完之后就没有可直接回滚的旧版 exe 了。' + CRLF +
      '需要回滚点的话，可以先把安装目录里的 ' + '{#AppExe}' + ' 复制到别处再继续。',
      mbInformation, MB_OK);
  end;
end;

// 返回非空字符串 = 中止安装并把这句原样显示给用户；回到「准备安装」页修好再点安装。
function PrepareToInstall(var NeedsRestart: Boolean): String;
var
  Target: String;
begin
  Result := '';
  Target := ExpandConstant('{app}\{#AppExe}');
  if TargetProgramRunning then
  begin
    Result := '「{#AppName}」现在还在运行，安装程序是普通权限，既不能结束它也覆盖不了它的文件。' +
      CRLF + CRLF +
      '请先把它全部关掉：界面右上角退出；如果它藏在后台（很可能是用「启动（管理员）」开的），' +
      '用任务管理器结束 ' + '{#AppExe}' + ' 进程。' + CRLF + CRLF +
      '然后回到这个页面点「安装」重试，不需要重新下载。';
    Exit;
  end;
  // 程序没在跑却仍可能被占用（杀毒软件正在扫描）。这种失败只能靠复制阶段暴露，
  // 所以这里只在日志里留个记号，方便事后判断是安装脚本还是环境问题。
  if not FileExists(Target) then
    Log('目标目录还没有旧版本程序，按全新安装处理');
end;

// ssInstall 是 [Files] 真正落地前的一刻：此时 {app} 已确定，旧 exe 还在，正是备份时机。
procedure CurStepChanged(CurStep: TSetupStep);
begin
  if CurStep = ssInstall then
    BackupPreviousExe;
end;
