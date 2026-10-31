"""speech_to_text_wechat.py - 语音转文字（本地 faster-whisper）。

为什么要有这个插件：微信语音在入站消息里只是一个 ``[语音]`` 占位符，模型完全
不知道对方说了什么。这里在**构造对话之前**（``on_prepare``，可以 await）把语音
下载到本地、用本地模型转写成文字，追加进 ``message['text']``。

三条底线：
- 只用本地模型，不上传音频到任何第三方；
- 转写失败只记一笔运行记录，保留 ``[语音]`` 占位，**绝不猜测内容**；
- 任何失败都不允许让本轮回复失败。
"""

from __future__ import annotations

import asyncio
from pathlib import Path

from . import PluginBase, PluginMeta, FieldSchema
from ..asr_service import ASRService


def _patch_av_open():
    """兼容新版 `av`：它不再接受 `metadata_errors`，而 faster-whisper 内部还在传。

    实测日志：`语音转写失败（open() got an unexpected keyword argument 'metadata_errors'）`
    —— 这是 **依赖之间**的版本冲突，不是我们的调用写错；用户看到的是"语音一直转不出来"。
    这里把 `av.open` 包一层、丢掉这个参数即可，不改动第三方包文件。
    """
    try:
        import av
    except Exception:
        return False
    if getattr(av, '_whale_open_patched', False):
        return True
    original = av.open

    def open_(*args, **kwargs):
        kwargs.pop('metadata_errors', None)
        return original(*args, **kwargs)

    open_._whale_original = original
    av.open = open_
    av._whale_open_patched = True
    return True


class SpeechToTextWeChat(PluginBase):
    META = PluginMeta(
        id="speech_to_text_wechat",
        name="语音转文字（本地）",
        category="media",
        description=(
            "把收到的微信语音下载到本地并用 faster-whisper 转写成文字，让聊天模型能读懂"
            "语音说了什么。全程本地推理，不上传音频；转写失败只保留 [语音] 占位并记一笔"
            "运行记录，绝不猜测语音内容。首次使用需要自行放好模型文件。"
        ),
        enabled_by_default=True,
        config_schema={
            "enabled": FieldSchema(type="bool", label="启用"),
            "maxAudioMb": FieldSchema(
                type="float", label="单条语音大小上限（MB）", min=1, max=200,
                description="超过就不转写，直接保留 [语音] 占位。默认 20。",
            ),
            "language": FieldSchema(
                type="string", label="转写语言",
                description="默认 zh（中文）。填 auto 交给模型自己判断会慢一些。",
            ),
            "model": FieldSchema(
                type="string", label="模型名（模型目录下的子目录名）",
                description="默认 base。可填 tiny/base/small/medium/large-v3 等；模型放在下面的目录里。",
            ),
            "computeType": FieldSchema(
                type="string", label="计算精度",
                description="默认 int8（CPU 上最快最省内存）。可填 int8/float16/float32。",
            ),
            "debugLog": FieldSchema(type="bool", label="调试日志"),
        },
        default_settings={
            "enabled": True,
            "maxAudioMb": 20.0,
            "language": "zh",
            "model": "base",
            "computeType": "int8",
            "debugLog": False,
        },
    )

    #: 配置里没写模型目录时的兜底位置（相对项目根目录/启动目录）。
    DEFAULT_MODEL_DIR = "./data/models/faster-whisper"

    def __init__(self, manager):
        super().__init__(manager)
        self._service = None
        self._service_key = None
        # 自有锁：**不能借 runtime.ai_lock** —— 那把锁是给聊天模型调用用的，
        # 拿着它跑几十秒的语音识别会把所有聊天请求一起堵死。这把锁只保证
        # "同一时刻只跑一次转写"，避免多核 CPU 被并发转写拖垮。
        self._lock: asyncio.Lock | None = None
        # 模型预取状态：安装包不带 141MB 的 whisper 模型，第一次用语音时要现场下载。
        # 以前这一等就是几分钟，上层看起来就是"语音识别失效"；现在先跳过、后台下完再提示。
        self._prefetching = False
        self._prefetch_failed = ''

    # ------------------------------------------------------------------
    # 内部工具
    # ------------------------------------------------------------------
    def _transcribe_lock(self) -> asyncio.Lock:
        # asyncio.Lock 需要事件循环，而插件实例可能在循环启动前就构造好了，
        # 所以延迟到真正要用的时候再建。
        if self._lock is None:
            self._lock = asyncio.Lock()
        return self._lock

    @staticmethod
    def _note(runtime, text: str) -> None:
        """只记一笔运行记录；记日志本身也不允许抛异常。"""
        try:
            runtime.note(text)
        except Exception:
            pass

    @staticmethod
    def _reason(exc: Exception) -> str:
        detail = str(exc).strip() or type(exc).__name__
        return detail[:80]

    @staticmethod
    def _append_transcripts(text, transcripts: list[str]) -> str:
        body = "\n".join(f"[语音转写] {item}" for item in transcripts)
        return (str(text or "").rstrip() + "\n" + body).strip()

    def _model_dir(self, runtime) -> str:
        """模型目录：优先配置 asr.model_dir，取不到就用默认相对路径。"""
        value = None
        try:
            config = getattr(runtime, "config", None)
            if config is not None:
                value = config.get("asr.model_dir")
        except Exception:
            value = None
        raw = value.strip() if isinstance(value, str) and value.strip() else self.DEFAULT_MODEL_DIR
        # 相对路径必须挂到**可写数据目录**上：打包后工作目录是安装目录，
        # 模型会解析到安装目录里（那里既不该写、也读不到用户之前下好的模型），
        # 结果就是"语音识别经常失效"。runtime.root 才是数据目录。
        path = Path(raw).expanduser()
        if not path.is_absolute():
            root = getattr(runtime, "root", None)
            if root:
                path = Path(root) / raw
        return str(path)

    def _settings_key(self, runtime) -> tuple:
        try:
            max_mb = float(self.get("maxAudioMb", 20) or 20)
        except (TypeError, ValueError):
            max_mb = 20.0
        return (
            str(self.get("model", "base") or "base").strip() or "base",
            str(self.get("language", "zh") or "zh").strip() or "zh",
            str(self.get("computeType", "int8") or "int8").strip() or "int8",
            max_mb,
            self._model_dir(runtime),
        )

    def _service_for(self, runtime) -> ASRService:
        """懒加载单例：模型只加载一次；改了模型/语言/精度就重建（并丢弃旧模型）。"""
        key = self._settings_key(runtime)
        if self._service is not None and self._service_key == key:
            return self._service
        model, language, compute_type, max_mb, model_dir = key
        self._service = ASRService(
            model=model,
            model_dir=model_dir,
            language=language,
            compute_type=compute_type,
            max_audio_mb=max_mb,
            cache_dir=str(Path(runtime.root)/'data'/'voice_inbox'),
        )
        self._service_key = key
        return self._service

    # ------------------------------------------------------------------
    # 下载与转写
    # ------------------------------------------------------------------
    async def _download(self, message: dict, item: dict, runtime) -> str:
        """下载一条语音，返回本地路径；任何一步失败都返回空串并记一笔。"""
        local_id = item.get("local_id")
        if local_id is None:
            self._note(runtime, "语音消息缺少 local_id，无法下载，保留 [语音] 占位")
            return ""
        try:
            result = await runtime.transport.call(
                "download_media",
                {
                    "chat": message.get("target"),
                    "local_id": local_id,
                    "msg_type": "voice",
                    "shard": item.get("shard", ""),
                },
            )
        except Exception as exc:
            self._note(runtime, f"语音下载失败（{self._reason(exc)}），保留 [语音] 占位")
            return ""
        if not isinstance(result, dict) or not result.get("ok", True):
            detail = ""
            if isinstance(result, dict):
                detail = str(result.get("message") or result.get("error") or "")
            suffix = f"：{detail[:60]}" if detail else ""
            self._note(runtime, f"语音下载失败{suffix}，保留 [语音] 占位")
            return ""
        path = str(result.get("path") or "").strip()
        if not path or not Path(path).is_file():
            self._note(runtime, "语音下载失败：下载接口没有返回可用的本地文件")
            return ""
        return path

    def _model_ready(self, runtime) -> bool:
        """模型目录里是否有可用的快照（faster-whisper 的 model.bin）。"""
        try:
            root = Path(self._model_dir(runtime))
        except Exception:
            return False
        if not root.is_dir():
            return False
        return self._service_for(runtime).local_model_path() is not None

    async def transcribe_file(self, path, runtime) -> str:
        """工作台附件入口，使用已有所选模型，避免上传时隐式下载大型模型。"""
        if not self.get('enabled',True):raise ValueError('本地语音插件已关闭')
        file=Path(path)
        if not file.is_file() or file.suffix.lower() not in ASRService.SUPPORTED_SUFFIXES:
            raise ValueError('音频文件不存在或格式不受支持')
        service=self._service_for(runtime)
        if file.stat().st_size>service.max_audio_bytes:raise ValueError('音频文件超过语音插件大小上限')
        if not self._model_ready(runtime):
            raise ValueError('所选本地语音模型尚未安装：'+service.model_name+'；请在插件页核对型号与模型目录')
        async with self._transcribe_lock():
            result=await service.transcribe(str(file))
        text=str(getattr(result,'text','') or '').strip()
        if not text:raise ValueError('没有识别到有效语音内容')
        return text

    async def _ensure_model(self, runtime) -> bool:
        """模型没准备好就先返回 False（本条跳过），并在后台预取一次。

        为什么不在请求里等下载：141MB 下载 + 加载会占几分钟，
        而那几条语音早就超时/被上层判成失败了，用户看到的就是"语音识别失效"。
        """
        if self._model_ready(runtime):
            return True
        if self._prefetching:
            self._note(runtime, "语音模型正在后台下载（约 140MB），本条先保留 [语音] 占位")
            return False
        self._prefetching = True
        self._prefetch_failed = ''
        service = self._service_for(runtime)
        model_dir = self._model_dir(runtime)

        async def warm():
            try:
                await asyncio.to_thread(service._ensure_model)
            except Exception as exc:
                self._prefetch_failed = self._reason(exc)
                self._note(runtime, f"语音模型下载失败（{self._prefetch_failed}）；"
                                    f"可手动把 faster-whisper 模型放到 {model_dir}")
            else:
                self._note(runtime, "语音模型已就绪，之后的语音可以正常转写")
            finally:
                self._prefetching = False

        asyncio.create_task(warm())
        self._note(runtime, f"首次使用语音识别：正在后台下载模型（约 140MB，存到 {model_dir}），"
                            "本条先保留 [语音] 占位")
        return False

    async def _transcribe_one(self, message: dict, item: dict, runtime) -> str:
        """转写一条语音，返回文本；失败返回空串（[语音] 占位保持原样）。"""
        if not await self._ensure_model(runtime):
            return ""
        path = await self._download(message, item, runtime)
        if not path:
            return ""
        try:
            size = Path(path).stat().st_size
        except OSError as exc:
            self._note(runtime, f"语音文件读不到（{self._reason(exc)}），保留 [语音] 占位")
            return ""
        max_mb = self._settings_key(runtime)[3]
        if size > max_mb * 1024 * 1024:
            self._note(
                runtime,
                f"语音 {size / 1048576:.1f}MB 超过 {max_mb:g}MB 上限，已跳过转写，保留 [语音] 占位",
            )
            return ""
        try:
            async with self._transcribe_lock():
                # 真正的识别在 ASRService.transcribe 内部用 asyncio.to_thread 丢到线程池
                # 里跑（见 src/asr_service.py 第 146 行），事件循环不会被阻塞；
                # 这里 await 的是"等结果"，不是"在主线程里算"。
                result = await self._service_for(runtime).transcribe(path)
        except Exception as exc:
            self._note(runtime, f"语音转写失败（{self._reason(exc)}），保留 [语音] 占位")
            return ""
        text = str(getattr(result, "text", "") or "").strip()
        if not text:
            self._note(runtime, "语音转写没有识别到内容，保留 [语音] 占位")
            return ""
        if self.get("debugLog", False):
            self._note(runtime, f"语音转写完成（{len(text)} 字）")
        return text

    # ------------------------------------------------------------------
    # 钩子
    # ------------------------------------------------------------------
    async def on_prepare(self, message, runtime) -> None:
        try:
            if not self.get("enabled", True):
                return
            if not isinstance(message, dict):
                return
            media = message.get("media")
            if not isinstance(media, list):
                return
            voices = [
                item
                for item in media
                if isinstance(item, dict) and item.get("type") == "voice"
            ]
            if not voices:
                return
            transcripts = []
            for item in voices:
                text = await self._transcribe_one(message, item, runtime)
                if text:
                    transcripts.append(text)
            if not transcripts:
                # 失败原因各自已经记过一笔，这里直接返回：契约要求保留 [语音] 占位。
                return
            message["voice_text"] = "\n".join(transcripts)
            message["text"] = self._append_transcripts(message.get("text"), transcripts)
        except Exception as exc:
            # 兜底：连 on_prepare 的外层都不能抛，否则管理器只能记一句笼统的失败。
            self._note(runtime, f"语音转写异常（{type(exc).__name__}），本轮保留 [语音] 占位")
