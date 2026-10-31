"""无需 Qt 的启动监护与后台入口，原生崩溃也能留下可见诊断。"""
import json
import os
import platform
import subprocess
import sys
from pathlib import Path
from .runtime_identity import APP_VERSION, PORT


def worker_command(flag,*args):
    if getattr(sys,'frozen',False):return [sys.executable,flag,*map(str,args)]
    return [sys.executable,str(Path(__file__).resolve().parent.parent/'gui_launcher.py'),flag,*map(str,args)]


def child_environment():
    env=dict(os.environ)
    if getattr(sys,'frozen',False):
        plugins=Path(sys._MEIPASS)/'PyQt6/Qt6/plugins'
        if plugins.is_dir():
            env['QT_PLUGIN_PATH']=str(plugins)
            env['QT_QPA_PLATFORM_PLUGIN_PATH']=str(plugins/'platforms')
        env.setdefault('QT_OPENGL','software')
    return env


def stop_backend_process(process):
    """仅清理本控制台创建的后台及其子进程，避免 Python 启动器留下占库进程。"""
    if process is None or process.poll() is not None:return
    if os.name=='nt':
        subprocess.run(['taskkill','/PID',str(process.pid),'/T','/F'],capture_output=True,
            creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0),timeout=10,check=False)
    else:process.terminate()
    try:process.wait(timeout=5)
    except subprocess.TimeoutExpired:
        process.kill();process.wait(timeout=5)


def failure_report(log_dir,code):
    report={'version':APP_VERSION,'exit_code':code,'exit_hex':f'0x{code & 0xffffffff:08X}',
        'windows':platform.platform(),'architecture':platform.machine(),'python_bits':64 if sys.maxsize>2**32 else 32,
        'frozen':bool(getattr(sys,'frozen',False)),'logs':str(log_dir)}
    path=Path(log_dir)/'startup-diagnostics.json'
    path.write_text(json.dumps(report,ensure_ascii=False,indent=2),'utf-8')
    return path


def show_failure(log_dir,code):
    message=(f'鲸鱼娘控制台未能正常启动或已异常退出（0x{code & 0xffffffff:08X}）。\n\n'
        f'诊断已保留在：\n{log_dir}\n\n请将 startup-diagnostics.json、startup.log 和 crash.log 提供给开发者排查。原配置和对话未被清空。')
    if os.name=='nt':
        import ctypes
        ctypes.windll.user32.MessageBoxW(None,message,'鲸鱼娘 · 启动异常',0x10)
    else:print(message,file=sys.stderr)


def supervise(log_dir,args,notify=True):
    try:
        process=subprocess.Popen(worker_command('--gui-worker',*args),env=child_environment(),
            creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0))
        code=process.wait()
    except OSError:
        import traceback
        traceback.print_exc();code=1
    if code:
        failure_report(log_dir,code)
        if notify:show_failure(log_dir,code)
    return code


def run_backend(args):
    import argparse
    parser=argparse.ArgumentParser();parser.add_argument('--root',required=True)
    parser.add_argument('--port',type=int,default=PORT)
    options=parser.parse_args(args)
    root=Path(options.root).resolve();root.mkdir(parents=True,exist_ok=True)
    import uvicorn
    from .wechat_app import WeChatRuntime,create_app
    runtime=WeChatRuntime(root=root,config_path=root/'config.wechat.yaml',db_path=root/'data/wechat-only/messages.sqlite3')
    uvicorn.run(create_app(runtime),host='127.0.0.1',port=options.port,log_level='warning',log_config=None)
    return 0
