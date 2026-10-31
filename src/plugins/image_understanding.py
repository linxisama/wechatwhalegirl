"""Awaited image recognition for WeChat media (downloaded to local files first).

微信的图片没有可直接交给模型的公开 URL，插件必须先用 ``transport.download_media``
把图片落到本机，再读成 base64 data URL 交给视觉模型；超过大小上限的图直接跳过并写状态，
绝不假装看过。
"""
from pathlib import Path
from urllib.parse import urlsplit
import asyncio
import base64
import hashlib
import tempfile
import time
import threading

from . import PluginBase, PluginMeta, FieldSchema

# 微信下载下来的单张图片上限。base64 之后体积再涨约 1/3，
# 超过这个尺寸既拖慢识图也容易顶到模型请求上限，所以宁可不看并如实说明。
# 动图（GIF/WebP）经常超过 6MB，旧上限会把它们直接判成"无法读取"；
# 抬到 12MB，送模型前照样按最长边压缩，成本不变。
MAX_IMAGE_BYTES = 12 * 1024 * 1024


def file_to_data_url(path) -> str:
    """把本机图片读成 ``data:image/...;base64,...``；不可用或超限返回空串。

    格式判定交给 ``image_input.image_source``（按魔数识别 PNG/JPEG/GIF/WebP），
    免得把改名过的文件当成受支持图片送给模型。
    """
    from ..image_input import image_source
    try:
        target = Path(str(path))
        if not target.is_file():
            return ""
        if target.stat().st_size > MAX_IMAGE_BYTES:
            return ""
        import base64
        encoded = base64.b64encode(target.read_bytes()).decode('ascii')
    except Exception:
        return ""
    return image_source('base64://' + encoded)


class ImageUnderstanding(PluginBase):
    def __init__(self,manager):
        super().__init__(manager)
        self._storage_lock=threading.Lock()
        self._received_store=None

    META = PluginMeta(
        id="image_understanding", name="图片与表情包识别", category="utility",
        description=("把收到的图片及以图片形式收到的表情包送给视觉模型识别，再按现有人设回复。"
                     "GIF/WebP 动图先抽帧；微信加密动画表情无法导出时会明确告知，可手动上传原文件。"
                     "仅处理需要回复的图片：先存入本机图片储藏区，再读取识别；超过 50 张删除最旧一半。"
                     "单张超过 12MB 的图跳过并如实告知；不删除聊天记录、表情库或微信原图。"
                     "每条图片消息增加一次识图调用；沿用现有 API 地址与密钥。"
                     "DeepSeek 官方接口自动使用 deepseek-flash，其他接口需支持图片输入。"
                     "仅识别收到的图片，不提供发送表情包能力。"),
        enabled_by_default=True,
        config_schema={
            "enabled": FieldSchema(type="bool", label="启用图片识别"),
            "visionModel": FieldSchema(type="string", label="识图模型（可留空）",
                description="留空：DeepSeek 官方接口用 deepseek-flash，其他接口跟随聊天模型；可填写服务商的视觉模型名。"),
            "maxImages": FieldSchema(type="int", label="每条消息最多识别图片数", min=1, max=3),
            "longImage": FieldSchema(type="bool", label="长截图分段阅读",
                description="单张长截图按从上到下分段，同一次视觉调用最多读取三段；未读区域会明确提示。"),
            "maxTiles": FieldSchema(type="int", label="长截图最多读取段数", min=2, max=3),
            "visionMaxSide": FieldSchema(type="int", label="送模型的图片最长边（像素）",
                                         min=512, max=4096,
                                         description="越大越清楚、也越慢越贵。表格/小字截图建议 2048；"
                                                     "普通照片 1536 够用。动图抽帧同样用这个上限。"),
            "visionApiUrl": FieldSchema(type="string", label="识图专用接口（留空=用主接口）",
                description="很多中转只支持文字。填一个支持看图的 OpenAI 兼容地址"
                            "（例如本地 Ollama 的 http://127.0.0.1:11434/v1/chat/completions）"
                            "就能绕开限制；只影响识图，不影响聊天。"),
            "visionApiKey": FieldSchema(type="string", label="识图专用密钥（留空=用主密钥）",
                description="明文保存在本机插件配置里，仅本机使用。"),
            "visionTimeout": FieldSchema(type="int", label="识图最多等几秒", min=3, max=120,
                description="识图挡在回复前面。超时就当没看清、直接回文字，避免整条消息被拖到过期。"),
        },
        default_settings={"visionMaxSide": 2048, "enabled": True, "visionModel": "", "maxImages": 3,
                          "longImage": False, "maxTiles": 3, "visionTimeout": 25},
    )

    def _max_side(self) -> int:
        """送模型的图片最长边：用户嫌糊就调大，夹在 512..4096 之间防呆。"""
        try:
            return max(512, min(int(self.get("visionMaxSide", 2048) or 2048), 4096))
        except (TypeError, ValueError):
            return 2048

    async def _remote_source(self, url: str) -> str:
        """从微信 CDN 直链取图（表情 XML 里的 `cdnurl`），失败返回空串。

        为什么走这条路：微信 4.x 把"动画表情"的本地文件加密了，`download_media`
        只能拿到乱码；但消息 XML 里给了 CDN 直链，下载回来就是正常 JPEG/PNG/GIF。
        `fetch_image` 负责"只允许公网地址 + 大小上限"，这里只做格式归一。
        """
        try:
            from ..sticker_library import fetch_image
            from ..image_input import image_source
            raw = await fetch_image(url)
            return image_source('base64://'+base64.b64encode(raw).decode('ascii')) if raw else ''
        except Exception:
            return ""

    async def _acquire_source(self, runtime, message, item):
        """把一条媒体段变成 data URL；失败返回空串。"""
        if item.get('_library_source'):
            from ..image_input import image_source
            return image_source(item['_library_source'])
        # 有 CDN 直链就先走直链（表情/图片都适用；图片本地导出失败时也是兜底）
        link = str(item.get("url") or "")
        if link:
            source = await self._remote_source(link)
            if source:
                return source
        if str(item.get("type")) == "emotion":
            # 表情的本地文件是加密的，别再拿它去试 download_media（只会白等 60 秒）
            return ""
        try:
            result = await runtime.transport.call('download_media', {
                'chat': message.get('target'),
                'local_id': item.get('local_id'),
                'msg_type': 'image',
                'shard': item.get('shard', ''),
            })
        except Exception:
            return ""
        if not isinstance(result, dict) or not result.get('ok'):
            return ""
        if '_thumb' in Path(str(result.get('path') or '')).stem:
            message['_vision_preview'] = True
        return file_to_data_url(result.get('path'))

    async def _local_source(self, runtime, message, item):
        source=await self._acquire_source(runtime,message,item)
        if not source:
            return ''
        try:
            from ..received_images import ReceivedImageStore
            from ..image_input import image_source
            root=getattr(runtime,'root',None)
            folder=(Path(root)/'data/received_images' if root is not None
                    else self._manager.data_dir/'received_images')
            raw=base64.b64decode(source.split(',',1)[1],validate=True)
            def persist():
                # 所有下载入口共用一个锁，含缓存命中也先确认原图副本可读取。
                with self._storage_lock:
                    if self._received_store is None or self._received_store.root!=folder.absolute():
                        self._received_store=ReceivedImageStore(folder)
                    return self._received_store.save_and_read(raw)
            result,saved=await asyncio.to_thread(persist)
            message.setdefault('_image_storage',[]).append(result)
            runtime.note(f"图片已保存到储藏区：{Path(result['path']).name}，当前 {result['count']} 张，清理 {result['deleted']} 张旧图")
            if result['cleanup_failed']:
                runtime.note(f"图片储藏区清理未完成：{result['cleanup_failed']} 张无法删除，将在下次保存时重试")
            return image_source('base64://'+base64.b64encode(saved).decode('ascii'))
        except (OSError,ValueError) as exc:
            message['_image_storage_error']=('失败：图片文件损坏、像素超限或储藏区校验失败' if isinstance(exc,ValueError)
                                            else '失败：图片储藏区保存或读取失败，请检查磁盘空间和目录权限')
            runtime.note('图片储藏区保存或读取失败：'+type(exc).__name__+'；未调用视觉模型')
            return ''

    async def _download_video(self, runtime, message, item) -> str:
        """把一条视频落到本机，返回路径；拿不到返回空串。

        走语音那条既有路线（transport.download_media + msg_type='video'）：
        下载细节归 bridge，插件只认消息字典，不自己碰数据库。
        """
        local_id = item.get('local_id')
        if local_id is None:
            local_id = message.get('local_id')
        try:
            result = await runtime.transport.call('download_media', {
                'chat': message.get('target'),
                'local_id': local_id,
                'msg_type': 'video',
                'shard': item.get('shard', ''),
            })
        except Exception:
            return ''
        if not isinstance(result, dict) or not result.get('ok'):
            return ''
        path = str(result.get('path') or '')
        return path if path and Path(path).is_file() else ''

    async def _frames_from_link(self, item, want, max_side):
        """视频若带 CDN 直链（沙盒通道目前只给 emotion/image 直链），直接取字节就地抽帧。"""
        url = str(item.get('url') or '')
        if not url:
            return []
        try:
            from ..sticker_library import fetch_image
            blob = await fetch_image(url)
        except Exception:
            return []
        if not isinstance(blob, (bytes, bytearray)) or not blob:
            return []
        frames = []
        try:
            with tempfile.TemporaryDirectory() as folder:
                clip = Path(folder) / 'clip.bin'
                clip.write_bytes(bytes(blob))
                from ..video_frames import frames_for_video
                frames, _reason = await asyncio.to_thread(
                    frames_for_video, str(clip), want, max_side)
        except Exception:
            return []
        return [f for f in (frames or []) if isinstance(f, str) and f.startswith('data:image/')][:want]

    async def _frames_from_guest(self, runtime, message, item, want, max_side):
        """沙盒后端下不了媒体：请隔离客人端就地抽帧，把 data URL 随回执送回来。"""
        local_id = item.get('local_id')
        if local_id is None:
            local_id = message.get('local_id')
        if local_id is None:
            return []
        try:
            result = await runtime.transport.call('video_frames', {
                'chat': message.get('target'),
                'local_id': local_id,
                'shard': item.get('shard', ''),
                'want': want,
                'max_side': max_side,
            })
        except Exception:
            return []
        if not isinstance(result, dict) or not result.get('ok'):
            return []
        frames = [f for f in (result.get('frames') or [])
                  if isinstance(f, str) and f.startswith('data:image/')]
        return frames[:want]

    async def _video_screens(self, runtime, message, item, want, max_side):
        """三级取画面：CDN 直链 → 隔离客人端抽帧 → 本机下载再抽帧。

        返回 (frames, 失败原因后半句)：任一层成功即停，三层全败才给原因，
        调用方拼成「第 N 个视频<原因>」，与旧文案逐字一致。
        """
        want = max(1, min(int(want or 1), 3))
        frames = await self._frames_from_link(item, want, max_side)
        if frames:
            return frames, ''
        frames = await self._frames_from_guest(runtime, message, item, want, max_side)
        if frames:
            return frames, ''
        try:
            path = await self._download_video(runtime, message, item)
        except Exception:
            path = ''
        if not path:
            return [], '没能落到本机，未抽帧'
        try:
            from ..video_frames import frames_for_video
            frames, reason = await asyncio.to_thread(frames_for_video, path, want, max_side)
        except Exception:
            frames, reason = [], ''
        if not frames:
            return [], f'抽帧失败：{reason or "原因未知"}'
        return frames[:want], ''

    @staticmethod
    def _record_collected(runtime, source, description):
        store = getattr(runtime, 'stickers', None)
        if store is not None:
            # 只回填已收集的条目，不让识图绕过用户的私聊/黑名单收集规则。
            try:
                digest = hashlib.sha256(base64.b64decode(source.split(',', 1)[1])).hexdigest()
                store.record_description(digest, description)
            except Exception:
                runtime.note('图片已识别，图库说明写入失败；保留本轮识别结果')

    async def describe(self, message, runtime):
        # 未点名的群聊图片：**在任何下载/保存/抽帧之前**就退出。
        # 交接指南 3.3 指出旧检查点位于取图之后，等于"先下了再说不识别"；这里移到最前面。
        if message.get('_picture_text_only'):
            message['vision_status'] = '略过：群聊未点名的图片不做识图'
            runtime.note('群聊未点名的图片：只回文字，不下载不识别')
            return
        # 表情（动画表情）也算"可识别的图"：只要消息里带了 CDN 直链就能下载原图。
        media_items = [item for item in message.get("media", []) if isinstance(item, dict)]
        images,videos,emotions = [],[],0
        for item in media_items:
            # 视频也进同一条视觉通路：画面从本机视频文件里等间隔抽帧，帧数取 videoFrames。
            if item.get('type')=='video':
                videos.append(item)
            elif item.get('type')=='image' or item.get('type')=='emotion' and item.get('url'):
                images.append(item)
            elif item.get('type')=='emotion':
                store=getattr(runtime,'stickers',None)
                raw=b''
                if store and callable(getattr(store,'matching_md5',None)) and item.get('md5') and self.enabled:
                    raw=await asyncio.to_thread(store.matching_md5,item['md5'])
                if raw:
                    images.append(dict(item,_library_source='base64://'+base64.b64encode(raw).decode('ascii')))
                else:
                    emotions+=1
        if not images and not videos and not emotions:
            return
        # Failure must erase any stale annotation before retrying this message.
        message.pop("vision_description", None)
        message.pop("vision_cache_hit", None)
        message.pop('_vision_preview', None)
        message.pop('_unavailable_emotion',None)
        message.pop('_image_storage_error',None)
        message.pop('_image_storage',None)
        message["vision_status"] = "失败：未取得可读取的图片文件"
        if not self.enabled:
            message["vision_status"] = "未启用图片识别"
            return
        if not images and not videos:
            message['_unavailable_emotion'] = True
            # 两个原因都写清楚：本地文件加密（不能靠下载）+ 没有 CDN 直链（也没法绕）
            message['vision_status'] = ('失败：这条动画表情的本地文件是加密的，'
                                        '且微信没给可下载地址（cdnurl）；'
                                        '请以图片形式发送，或在「表情包管理」里上传原文件')
            return

        memory = self._manager._plugins.get('sticker_memory')
        chat_key = f"{runtime.mode}|{getattr(runtime,'account',lambda:'')()}|{message.get('kind','')}:{message.get('target','')}"
        limit = max(1, int(self.get("maxImages", 3)))
        # 先下载（缓存键要按图片内容算，所以必须先拿到字节），再决定要不要调模型。
        # 交接指南 3.2：取图/落盘/抽帧与视觉要分开计时，否则"识图慢"永远说不清卡在哪一段。
        acquire_started = time.monotonic()
        sources = []
        indices = []
        skipped = 0
        missing = []
        for index, item in enumerate(images, 1):
            if len(sources) >= limit:
                skipped += 1
                continue
            data_url = await self._local_source(runtime, message, item)
            if not data_url:
                skipped += 1
                if not message.get('_image_storage_error'):
                    missing.append((index, item))
                continue
            sources.append(data_url)
            indices.append(index)

        # 数据库事件可能早于图片文件落盘；先读完所有图片再有限重查缺失项，
        # 不能让首张图的等待挤占其他图片的位置或改变原消息中的图片编号。
        for delay in (0.3, 0.6):
            if not missing or len(sources) >= limit:
                break
            await asyncio.sleep(delay)
            retry = []
            for index, item in missing:
                data_url = await self._local_source(runtime, message, item)
                if data_url and len(sources) < limit:
                    sources.append(data_url)
                    indices.append(index)
                    skipped -= 1
                else:
                    retry.append((index, item))
            missing = retry
        if sources:
            ordered = sorted(zip(indices, sources))
            indices, sources = [v[0] for v in ordered], [v[1] for v in ordered]

        tiled, partial = False, False
        if sources:
            runtime.note(f'取图/抽帧阶段耗时 {time.monotonic() - acquire_started:.2f} 秒'
                         f'（{len(sources)} 个画面待识别）')
        if len(images) == 1 and sources and self.get('longImage', False):
            try:
                from ..long_image import split_long_image
                tiles = await asyncio.to_thread(split_long_image, sources[0], self.get('maxTiles', 3))
                if tiles:
                    sources, partial = tiles
                    tiled = True
            except Exception:
                message['vision_status'] = '失败：长截图无法分段或尺寸超限，未读取其内容'
                runtime.note('长截图识别跳过：无法分段或尺寸超限')
                return

        if not sources and not videos:
            message["vision_status"] = message.get('_image_storage_error') or '失败：微信图片尚未落盘、无法解密或超过 12MB；可先在微信手动打开该图片，再发起识别'
            runtime.note("图片识别跳过：图片未能下载到本机或体积超过 12MB")
            return
        endpoint = str(self.get('visionApiUrl', '') or '').strip() or runtime.ai.api_url
        model = str(self.get('visionModel', '') or '').strip() or (
            'deepseek-flash' if urlsplit(endpoint).hostname == 'api.deepseek.com' else runtime.ai.model)
        admin = self._manager._plugins.get('sticker_admin')
        frame_count = int(admin.get('gifFrames', 2)) if admin else 2
        variant = repr((endpoint, model, self._max_side(), frame_count,
                        getattr(runtime.ai, 'api_style', 'auto')))
        if (not tiled and len(images) == 1 and len(sources) == 1 and memory and memory.enabled
                and not message.get('_vision_preview')):
            try:
                cached = memory.cached(sources[0], chat_key, variant)
            except Exception:
                runtime.note('识图缓存读取失败，继续识别本轮图片')
                cached = None
            if cached:
                message['vision_description'] = cached
                message['vision_status'] = ('部分缺失：另有表情没有可下载的地址，未能读取' if emotions else 'ok')
                message['vision_cache_hit'] = True
                self._record_collected(runtime, sources[0], cached)
                return
        originals = list(sources)
        if not tiled:
            from ..sticker_library import frames_for_vision
            admin = self._manager._plugins.get('sticker_admin')
            want = int(admin.get('gifFrames', 2)) if admin else 2
            normalized, mapped, gif_notes = [], [], []
            # 每张原图至少留一个位置，动图抽帧不能挤掉后面的图片。
            for offset, (source, index) in enumerate(zip(sources, indices)):
                remaining = 3 - len(normalized) - (len(sources) - offset - 1)
                if source.startswith(('data:image/gif;', 'data:image/webp;')):
                    try:
                        frames = await asyncio.to_thread(frames_for_vision,
                            base64.b64decode(source.split(',', 1)[1]), min(want, remaining),
                            self._max_side())
                    except (ValueError, OSError):
                        skipped += 1
                        continue
                else:
                    frames = [source]
                if len(frames) > 1:
                    gif_notes.append(f'第 {index} 张动图抽了 {len(frames)} 帧')
                normalized.extend(frames)
                mapped.extend([index] * len(frames))
            sources, indices = normalized, mapped
            if not sources and not videos:
                message['vision_status'] = '失败：图片或动图文件损坏、像素超限或格式不受支持'
                return
            if gif_notes:
                runtime.note('识图：' + '；'.join(gif_notes))

        # 视频：画面从本机视频文件里等间隔抽帧（抽帧实现见 src/video_frames.py，纯函数、坏视频只降级）。
        # 视频帧与图片帧共享同一条 3 画面上限，所以名额要按已取到的画面数实时算。
        video_screens, video_problems, video_notes = [], [], []
        if videos:
            video_started = time.monotonic()
            admin_video = self._manager._plugins.get('sticker_admin')
            want_video = int(admin_video.get('videoFrames', 3) or 3) if admin_video else 3
            from ..video_frames import frames_for_video
            for order, item in enumerate(videos, 1):
                if tiled:
                    video_problems.append('长截图分段已占满画面名额，本轮未抽视频帧')
                    continue
                remaining = 3 - len(sources) - len(video_screens)
                if remaining <= 0:
                    video_problems.append(f'第 {order} 个视频没有剩余画面名额，未抽帧')
                    continue
                try:
                    frames, reason = await self._video_screens(
                        runtime, message, item, min(want_video, remaining), self._max_side())
                except Exception:
                    video_problems.append(f'第 {order} 个视频抽帧异常，已跳过')
                    continue
                if not frames:
                    video_problems.append(f'第 {order} 个视频{reason}')
                    continue
                video_screens.extend(frames)
                video_notes.append(f'第 {order} 个视频抽了 {len(frames)} 帧')
            if video_notes:
                runtime.note('识图：' + '；'.join(video_notes))
            if video_problems:
                runtime.note('识图：' + '；'.join(video_problems))
            runtime.note(f'视频抽帧阶段耗时 {time.monotonic() - video_started:.2f} 秒'
                         f'（{len(video_screens)} 帧画面）')
        screens = list(sources) + video_screens
        if not screens:
            # 一条画面都没拿到：把图片与视频各自的原因都写清楚，再跳过视觉模型。
            reasons = []
            if images:
                reason = str(message.get('_image_storage_error')
                             or '失败：微信图片尚未落盘、无法解密或超过 12MB')
                reasons.append(reason[len('失败：'):] if reason.startswith('失败：') else reason)
            reasons.extend(video_problems)
            message['vision_status'] = '失败：' + '；'.join(part for part in reasons if part)
            runtime.note('识图跳过：' + message['vision_status'])
            return
        error = None
        try:
            # 识图挡在回复前面：给一个**硬超时**（默认 15 秒）。中转站一次视觉请求就可能 20 秒，
            # 以前会把整条消息拖到"过期、避免迟到回复"。超时就当没看清，让机器人先回话。
            limit = max(3.0, float(self.get("visionTimeout", 25) or 25))
            # 文字极速档不能偷偷把用户设置的识图等待压到 6 秒：同一视觉服务
            # 实际需要约 13 秒时，每张图都会被主动取消，形成“能收到但看不清”。
            started = time.monotonic()
            runtime.note(f'图片识别开始：模型「{model}」，{len(screens)} 个画面，上限 {limit:g} 秒')
            # 只在真正调用模型时占用锁；下载、抽帧与本地 OCR 不占文字模型锁。
            from contextlib import nullcontext
            async with getattr(runtime, 'ai_lock', nullcontext()):
                description = await asyncio.wait_for(runtime.ai.describe_images(
                    screens, model=model, max_side=self._max_side(),
                    api_url=str(self.get("visionApiUrl", "") or "").strip() or None,
                    api_key=str(self.get("visionApiKey", "") or "").strip() or None),
                    timeout=limit)
        except asyncio.TimeoutError:
            error = TimeoutError(f'识图超过 {int(limit)} 秒未返回')
            description = ""
            runtime.note(f'图片识别超时（超过 {int(limit)} 秒），本轮直接回复文字，不编造画面')
        except Exception as exc:
            error = exc
            description = ""
        if not str(description or '').strip():
            from ..image_input import vision_failure
            message["vision_status"] = vision_failure(runtime.ai, error)
            runtime.note('图片识别' + message['vision_status'])
            return
        runtime.note(f'图片识别成功：模型「{model}」，耗时 {time.monotonic() - started:.2f} 秒')
        if tiled:
            message['vision_description'] = (
                f'同一张长截图从上到下的 {len(sources)} 个分段，段间有重叠：\n' + str(description))[:3000]
            message['vision_status'] = ('部分缺失：已达到长图分段或体积上限，下方未读区域不能推断'
                                        if partial else 'ok')
            if emotions:
                message['vision_status'] = '部分缺失：微信加密表情无法读取；' + message['vision_status']
            # 分段结果不写入整图缓存，否则下次会把局部识别当作全图已读。
            return
        recognized = len(set(indices))
        frame_note = f'（动图抽取 {len(sources) - recognized + 1} 帧，仅代表可见画面）' if len(sources) > recognized else ''
        if video_screens:
            frame_note += f'（另有视频抽取的 {len(video_screens)} 帧画面，仅代表部分时间点）'
        if indices:
            message["vision_description"] = ("以下按顺序对应原消息第 " + "、".join(map(str, sorted(set(indices))))
                                             + " 张图片" + frame_note + "：\n" + str(description))[:3000]
        else:
            # 纯视频消息：没有"第几张图片"可对应，直接说明这就是抽帧画面。
            message["vision_description"] = (f'以下按顺序是视频里抽取的 {len(video_screens)} 帧画面'
                                             '（仅代表部分时间点，画面之间可能跳接）：\n'
                                             + str(description))[:3000]
        message["vision_status"] = (f"部分缺失：只识别了 {recognized}/{len(images)} 张图片，"
                                    "其余未识别（未能下载或超过 12MB），不能推断其内容"
                                    if skipped or recognized < len(images) else "ok")
        if video_problems:
            detail = '；'.join(video_problems)
            message['vision_status'] = ('部分缺失：' + detail + '；其余画面已识别'
                                        if message['vision_status'] == 'ok'
                                        else '部分缺失：' + detail + '；' + message['vision_status'])
        if emotions:
            message['vision_status'] = '部分缺失：另有微信加密表情无法读取；' + message['vision_status']
        if message.pop('_vision_preview', False):
            message['vision_status'] = '部分缺失：本机只有缩略图，细节与文字可能看不清；' + message['vision_status']
        if len(images) == 1 and not skipped and not video_screens and memory and memory.enabled:
            if message['vision_status'] == 'ok':
                try:
                    memory.record_description(originals[0], message['vision_description'], chat_key, variant)
                except Exception:
                    runtime.note('图片已识别，识图缓存写入失败；保留本轮识别结果')
        if len(images) == 1 and not skipped and not video_screens and message['vision_status'] == 'ok':
            self._record_collected(runtime, originals[0], message['vision_description'])
