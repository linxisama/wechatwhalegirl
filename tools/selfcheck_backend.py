"""后端离线自检：不连微信、不连模型，验证「消息 → 插件 → 回复 → 发送」链路。

用法：.venv-wechat\\Scripts\\python.exe tools\\selfcheck_backend.py

为什么单独有一个脚本：控制台自检（`wechat_gui.py --self-test`）需要 PyQt6 与图形环境，
这个脚本只要后端依赖就能跑，出问题时更容易定位是后端还是界面。
"""
import json
import shutil
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))


def main() -> int:
    from src.gui.smoke import _run_backend_checks

    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        shutil.copytree(ROOT / "styles", root / "styles")
        result = _run_backend_checks(root)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    print("后端自检通过")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
