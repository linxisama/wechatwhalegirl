"""只启动微信后台（不打开控制台窗口）：python wechat_main.py

日常使用请直接运行 `wechat_gui.py`（控制台会自动拉起这个后台）。
"""
import os
from pathlib import Path

import uvicorn

from src.logging_setup import install_excepthook, setup_logging
from src.runtime_identity import PORT
from src.wechat_app import create_app

if __name__ == "__main__":
    os.chdir(Path(__file__).resolve().parent)
    # 先配日志再建应用：导入/启动阶段的异常也要落进 logs/backend.log
    setup_logging(Path.cwd() / "logs")
    install_excepthook()
    uvicorn.run(create_app(), host="127.0.0.1", port=PORT, log_level="info")
