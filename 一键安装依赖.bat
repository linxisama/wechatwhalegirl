@echo off
chcp 65001 >nul 2>&1
setlocal
cd /d "%~dp0"
echo ============================================================
echo   鲸鱼娘 · 微信控制台   安装依赖
echo ============================================================
echo.

where python >nul 2>&1
if errorlevel 1 (
    echo [错误] 没有找到 python。请先安装 Python 3.12 64 位，
    echo        安装时勾选 "Add python.exe to PATH"，然后重新运行本脚本。
    pause
    exit /b 1
)

if not exist ".venv-wechat\Scripts\python.exe" (
    echo [1/3] 创建虚拟环境 .venv-wechat ...
    python -m venv .venv-wechat
    if errorlevel 1 (
        echo [错误] 虚拟环境创建失败。
        pause
        exit /b 1
    )
) else (
    echo [1/3] 虚拟环境已存在，跳过创建。
)

echo [2/3] 安装依赖（首次约 3-8 分钟，会自动切换到国内镜像）...
".venv-wechat\Scripts\python.exe" -m pip install --upgrade pip -i https://pypi.tuna.tsinghua.edu.cn/simple
".venv-wechat\Scripts\python.exe" -m pip install -r requirements.wechat.txt -i https://pypi.tuna.tsinghua.edu.cn/simple
if errorlevel 1 (
    echo     清华镜像失败，改用阿里云镜像重试...
    ".venv-wechat\Scripts\python.exe" -m pip install -r requirements.wechat.txt -i https://mirrors.aliyun.com/pypi/simple/
)
if errorlevel 1 (
    echo [错误] 依赖安装失败，请把上面的报错信息发给开发者。
    pause
    exit /b 1
)

echo [3/3] 检查依赖是否可用 ...
".venv-wechat\Scripts\python.exe" -c "import fastapi, uvicorn, httpx, yaml, cryptography, loguru, PyQt6, requests, psutil; print('  后端与界面依赖：通过')"
if errorlevel 1 (
    echo [错误] 后端或界面依赖不可用。
    pause
    exit /b 1
)
".venv-wechat\Scripts\python.exe" -c "import wechatauto; print('  微信 4.x 接入（wechatauto）：通过')" 2>nul
if errorlevel 1 echo  [提示] wechatauto 没装上，微信接入不可用（仍可用模拟模式看界面）。
".venv-wechat\Scripts\python.exe" -c "import wxauto4; print('  微信 4.0 备用后端（wxauto4）：通过')" 2>nul
if errorlevel 1 echo  [提示] wxauto4 没装上（只在微信 4.0.x 时才会用到，可忽略）。
".venv-wechat\Scripts\python.exe" -c "import faster_whisper; print('  本地语音识别：通过')" 2>nul
if errorlevel 1 echo  [提示] faster-whisper 没装上，语音转文字不可用（其它功能不受影响）。
".venv-wechat\Scripts\python.exe" -c "import winsdk; print('  本地读图 OCR：通过')" 2>nul
if errorlevel 1 echo  [提示] winsdk 没装上，本地读图不可用（其它功能不受影响）。

echo.
echo 安装完成。接下来：
echo   1) 双击「以管理员身份启动控制台.bat」（微信 4.x 发送需要管理员权限）
echo   2) 在「设置」页填写 API 地址 / Key / 模型，选择微信后端，保存
echo   3) 在「控制台」页点「启动机器人」，再点「测试微信」确认连通
echo.
pause
