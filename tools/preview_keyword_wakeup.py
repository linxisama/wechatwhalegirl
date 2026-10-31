"""离线显示关键词设置的填写说明，不连接微信、不改用户配置。"""
import argparse
import json
import os
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('output', type=Path)
    parser.add_argument('--native', action='store_true')
    args = parser.parse_args()
    if not args.native:
        os.environ['QT_QPA_PLATFORM'] = 'offscreen'
    from PyQt6.QtWidgets import QApplication
    from PyQt6.QtGui import QFont, QFontDatabase
    from src.plugins.keyword_trigger import KeywordTrigger
    import wechat_gui as gui
    app = QApplication([])
    for name in ('msyh.ttc', 'msyhbd.ttc', 'simsun.ttc'):
        QFontDatabase.addApplicationFont(str(Path(os.environ.get('WINDIR', 'C:/Windows')) / 'Fonts' / name))
    app.setFont(QFont('Microsoft YaHei UI', 10))
    app.setStyleSheet(gui.STYLESHEET)
    meta = KeywordTrigger.META
    dialog = gui.PluginConfigDialog({'id': meta.id, 'name': meta.name,
        'description': meta.description, 'schema': meta.schema_dict(),
        'defaults': meta.default_settings, 'settings': dict(meta.default_settings)})
    args.output.parent.mkdir(parents=True, exist_ok=True)
    dialog.show(); app.processEvents()
    dialog.grab().save(str(args.output))
    assert dialog.values() == meta.default_settings
    result = {'ok': True, 'defaults_preserved': True, 'size': [dialog.width(), dialog.height()],
              'summary': dialog.keyword_summary.text()}
    args.output.with_suffix('.json').write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding='utf-8')
    dialog.close()


if __name__ == '__main__':
    main()
