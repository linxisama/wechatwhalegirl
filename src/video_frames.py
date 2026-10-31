"""视频抽帧：把本机视频文件按时间等间隔解码成几张 data URL，交给视觉模型逐帧看。

为什么单独一个文件：抽帧是纯函数（路径进、data URL 列表出），不碰 Qt、不碰插件
状态，离线可测；解码库缺失或视频损坏时只降级、绝不抛异常。

为什么用 PyAV：冻结版 `_internal` 里已经带了 `av` 与 `av.libs`（内含 FFmpeg 运行时），
进程内就能解码，不需要外部 ffmpeg.exe，也就没有"打包后找不到命令行"这类事故。
"""
from __future__ import annotations

from pathlib import Path
from typing import List, Tuple

from PIL import Image

# 视频体积上限：微信里的聊天视频通常 5~50MB，200MB 已经远超正常范围；
# 再大只说明它不是"看一眼就够"的聊天视频，宁可不看也不要把识图管线拖死。
MAX_VIDEO_BYTES = 200 * 1024 * 1024
# 时长上限：抽帧是等间隔的，十分钟以上的视频抽三帧也读不出内容，白花解码时间。
MAX_VIDEO_SECONDS = 600.0
# 与 sticker_admin 的 videoFrames(min=1,max=3) 对齐：一个视频最多三帧，和图片共享 3 画面上限。
MAX_FRAMES = 3
NO_DECODER = "本端缺少视频解码组件"


def _data_url(image, max_side: int) -> str:
    """PIL 图 → ``data:image/png;base64,...``；最长边按识图上限压。

    写法与 ``sticker_library.frames_for_vision`` 里的一致（那个文件不归本任务改，
    所以在这里留一份），保证视频帧与动图帧送进模型时是同一种编码与分辨率。
    """
    import base64
    import io

    side = max(256, int(max_side or 1024))
    frame = image.convert('RGB')
    if frame.width > side or frame.height > side:
        frame.thumbnail((side, side), Image.LANCZOS)
    buffer = io.BytesIO()
    frame.save(buffer, 'PNG')
    return 'data:image/png;base64,' + base64.b64encode(buffer.getvalue()).decode('ascii')


def _stamp(frame, stream) -> float:
    """帧时间戳（秒）；拿不到返回 -1，调用方就"取到就用"。"""
    try:
        if frame.pts is None or stream.time_base is None:
            return -1.0
        return float(frame.pts * stream.time_base)
    except (TypeError, ValueError):
        return -1.0


def _duration(container, stream) -> float:
    """视频时长（秒）；读不到返回 0.0，调用方改走顺序等步长解码。"""
    try:
        if stream.duration is not None and stream.time_base is not None:
            seconds = float(stream.duration * stream.time_base)
            if seconds > 0:
                return seconds
    except (TypeError, ValueError, AttributeError):
        pass
    try:
        if container.duration:
            # av.time_base 就是 1e6（微秒）；container.duration 用这个单位。
            return max(0.0, float(container.duration) / 1_000_000)
    except (TypeError, ValueError, AttributeError):
        pass
    return 0.0


def _by_timestamp(container, stream, count: int, duration: float) -> List:
    """有时长：第 i 帧取 (i+0.5)/N * duration。

    取"时间戳刚好到目标点的第一帧"，所以同一段视频每次抽到的帧完全一致，
    不会随解码速度或 CPU 快慢漂移。
    """
    images: List = []
    for index in range(count):
        target = (index + 0.5) / count * duration
        try:
            container.seek(int(target / float(stream.time_base)), stream=stream)
        except Exception:
            return []
        picked = None
        for frame in container.decode(stream):
            picked = frame
            stamp = _stamp(frame, stream)
            if stamp < 0 or stamp >= target:
                break
        if picked is None:
            break
        images.append(picked.to_image())
    return images


def _by_index(path: str, count: int) -> List:
    """读不到时长：先顺序解一遍数帧，再按等步长取 N 帧（与动图抽帧同一套规则）。"""
    import av

    container = av.open(path)
    try:
        stream = container.streams.video[0]
        total = sum(1 for _ in container.decode(stream))
        if total <= 0:
            return []
        wanted = sorted({round(index * (total - 1) / max(1, count - 1))
                         for index in range(count)})
        container.close()
        container = av.open(path)
        stream = container.streams.video[0]
        images: List = []
        for index, frame in enumerate(container.decode(stream)):
            if index < wanted[len(images)]:
                continue
            images.append(frame.to_image())
            if len(images) == len(wanted):
                break
        return images
    finally:
        try:
            container.close()
        except Exception:
            pass


def frames_for_video(path, want: int = 3, max_side: int = 1024) -> Tuple[List[str], str]:
    """抽 N 帧，返回 ``(data URL 列表, 说明)``。

    成功时说明是空串；失败时列表为空、说明是可读中文原因（写进 runtime.note 与
    vision_status 用）。任何异常都在这里拦下——一个坏视频不能让整条消息的识别崩掉。
    """
    try:
        target = Path(str(path or ''))
    except (TypeError, ValueError):
        return [], '视频路径无效'
    try:
        if not target.is_file():
            return [], '视频文件不存在'
        size = target.stat().st_size
    except OSError:
        return [], '视频文件无法读取'
    if size <= 0:
        return [], '视频文件是空的'
    if size > MAX_VIDEO_BYTES:
        return [], f'视频超过 {MAX_VIDEO_BYTES // (1024 * 1024)}MB'
    try:
        import av
    except ImportError:
        return [], NO_DECODER
    count = max(1, min(int(want or 1), MAX_FRAMES))
    try:
        container = av.open(str(target))
    except Exception:
        return [], '视频文件无法解码'
    try:
        try:
            stream = container.streams.video[0]
        except (IndexError, AttributeError):
            return [], '视频里没有画面轨道'
        duration = _duration(container, stream)
        if duration > MAX_VIDEO_SECONDS:
            return [], f'视频超过 {int(MAX_VIDEO_SECONDS)} 秒'
        images = _by_timestamp(container, stream, count, duration) if duration > 0 else []
        if not images:
            images = _by_index(str(target), count)
        if not images:
            return [], '视频没有解出可用画面'
        return [_data_url(image, max_side) for image in images], ''
    except Exception:
        return [], '视频解码失败'
    finally:
        try:
            container.close()
        except Exception:
            pass
