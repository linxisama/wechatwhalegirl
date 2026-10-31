"""表情包与自定义图库 插件包入口（薄封装）。

真正实现在 src/plugins/image_library.py，这里只转出插件类：
插件包被单独安装时，加载器扫到的就是这个文件。
"""
from __future__ import annotations

from src.plugins.image_library import ImageLibrary as PLUGIN

META = PLUGIN.META
