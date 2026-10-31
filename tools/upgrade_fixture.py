"""安装包验证使用的离线夹具入口，目录参数必须由验证脚本明确指定。"""
import json
import sys
from pathlib import Path

ROOT=Path(__file__).resolve().parent.parent
sys.path.insert(0,str(ROOT))

if __name__=='__main__':
    from src.gui.upgrade_check import seed_fixture,verify_fixture
    action,root=sys.argv[1:3]
    if action=='seed':
        result=seed_fixture(root,ROOT/'styles')
    elif action=='verify':
        result=verify_fixture(root)
    else:
        raise ValueError('只支持 seed 或 verify')
    print(json.dumps(result,ensure_ascii=False))
