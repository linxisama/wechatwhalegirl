"""sticker_admin.py - 机器人本地表情库的开关与抽帧设置。

界面上「表情包管理 · 机器人本地图库」这个窗口归它管：关掉它就等于藏起管理入口
（接口会返回 403），已收藏的图片不会被删除。
"""
from __future__ import annotations

from . import PluginBase, PluginMeta, FieldSchema


class StickerAdmin(PluginBase):
    META = PluginMeta(
        id="sticker_admin",
        name="表情包管理",
        description="管理机器人本地图库：查看、改标签、AI 识别、删除；自动收集与上传的图片都进这里",
        category="media",
        enabled_by_default=True,
        config_schema={
            "enabled":   FieldSchema(type="bool", label="启用表情包管理"),
            "gifFrames": FieldSchema(type="int", label="动图识别抽几帧", min=1, max=3),
            "videoFrames": FieldSchema(type="int", label="视频识别抽几帧", min=1, max=3),
            "autoRecognize": FieldSchema(type="bool", label="新入库图片自动AI标注",
                description="开启后上传/回复中收集的新图进入后台标注队列，会使用识图接口；聊天已有结果直接复用。旧图库需点补全，不扫描闲聊图片。"),
        },
        default_settings={"enabled": True, "gifFrames": 2, "videoFrames": 3, "autoRecognize": False},
    )
