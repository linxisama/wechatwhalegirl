"""联网资讯（时间/新闻/热搜） 插件包入口（薄封装）。

真正实现在 src/plugins/web_context.py，这里只转出插件类：
插件包被单独安装时，加载器扫到的就是这个文件。
"""
from __future__ import annotations

from src.plugins.web_context import WebContext as PLUGIN

META = PLUGIN.META
