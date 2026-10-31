"""本地图片识别（读图）：把收到的微信图片 OCR 成文字，喂给上下文 / AI。

设计目标
--------
鲸鱼娘接的是**纯文本** AI 接口（如 DeepSeek chat completions），模型本身
看不到图。要让它"读懂"图里写了啥，就走「本地 OCR 出文字 → 写进对话上下文 →
让后续 AI 回复参考这段文字」的路线，全程不上传图片到第三方。

实现
----
- OCR 用项目自带的 ``ocr_worker.py``（Windows 下走 WinRT 中文 OCR，子进程跑，
  不阻塞主事件循环）。非 Windows（Linux）没有 WinRT OCR，``available()`` 返回
  False，读图自动降级为"图里写了啥看不太清"（不影响其它功能）。
- 结果按 (路径, mtime) 缓存，同一张图不重复 OCR。
- 有大小/条数/字符上限，避免大图卡死或把上下文撑爆。

线程安全：OCR 在独立子进程里跑，本类只做进程编排 + 缓存，可被 asyncio
``run_in_executor`` 调用。
"""

from __future__ import annotations

import os
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Optional

from loguru import logger


class ImageReader:
    """本地 OCR 读图服务（Windows 走 WinRT，其它平台降级不可用）。"""

    def __init__(
        self,
        enabled: bool = True,
        ocr_timeout: float = 20.0,
        max_image_mb: float = 12.0,
        max_chars: int = 800,
        ocr_worker_path: str = "",
    ):
        self.enabled = bool(enabled)
        self.ocr_timeout = max(3.0, float(ocr_timeout))
        self.max_image_mb = max(0.5, float(max_image_mb))
        self.max_chars = max(40, int(max_chars))
        # 默认找项目根目录下的 ocr_worker.py
        if ocr_worker_path:
            self._ocr_worker = Path(ocr_worker_path)
        else:
            root = Path(__file__).resolve().parent.parent
            self._ocr_worker = root / "ocr_worker.py"
        self._cache: dict[str, tuple[float, str]] = {}
        self._cache_lock = threading.Lock()

    # ------------------------------------------------------------------
    # 可用性
    # ------------------------------------------------------------------
    def available(self) -> bool:
        """当前环境能不能读图（Windows 有 ocr_worker 才算可用）。"""
        if not self.enabled:
            return False
        if sys.platform != "win32":
            return False
        if not self._ocr_worker.is_file():
            return False
        return True

    def status(self) -> dict:
        return {
            "enabled": self.enabled,
            "available": self.available(),
            "platform": sys.platform,
            "ocr_worker": str(self._ocr_worker),
            "ocr_worker_exists": self._ocr_worker.is_file(),
            "max_image_mb": self.max_image_mb,
            "max_chars": self.max_chars,
        }

    # ------------------------------------------------------------------
    # 读图
    # ------------------------------------------------------------------
    def read(self, image_path: str) -> str:
        """OCR 出图片里的文字；不可用 / 失败 / 无文字 一律返回空串。

        调用方（handler）通常在 ``run_in_executor`` 里跑本方法。
        """
        if not image_path or not os.path.isfile(image_path):
            return ""
        if not self.available():
            return ""

        size_mb = os.path.getsize(image_path) / 1048576
        if size_mb > self.max_image_mb:
            logger.debug(f"读图跳过：图片 {size_mb:.1f}MB 超过上限 {self.max_image_mb}MB")
            return ""

        mtime = os.path.getmtime(image_path)
        with self._cache_lock:
            hit = self._cache.get(image_path)
            if hit and abs(hit[0] - mtime) < 1.0:
                return hit[1][: self.max_chars]

        text = self._run_ocr(image_path)
        with self._cache_lock:
            if len(self._cache) > 128:
                # 简单按插入顺序清一半，避免无限增长
                for key in list(self._cache.keys())[:64]:
                    self._cache.pop(key, None)
            self._cache[image_path] = (mtime, text)
        return text[: self.max_chars]

    def _run_ocr(self, image_path: str) -> str:
        """起 ocr_worker 子进程做 WinRT OCR，带超时。

        打包（PyInstaller）后 `sys.executable` 是控制台自己的 exe，
        直接 `exe ocr_worker.py 图片` 会被当成"再启动一个控制台"。
        所以冻结运行时改走入口的 `--ocr-worker` 分支（见 gui_launcher.py），
        由它把参数交给 ocr_worker 的 main。
        """
        if getattr(sys, "frozen", False):
            command = [sys.executable, "--ocr-worker", image_path]
        else:
            command = [sys.executable, str(self._ocr_worker), image_path]
        try:
            proc = subprocess.run(
                command,
                capture_output=True,
                text=True,
                # 必须显式指定 UTF-8：ocr_worker 输出的是 UTF-8 中文，
                # 中文 Windows 下 subprocess 默认按 GBK 解码，会在读取线程里
                # 抛 UnicodeDecodeError，结果 stdout 变成空 → 表现为"读图读不出字"。
                encoding="utf-8",
                errors="replace",
                timeout=self.ocr_timeout,
            )
            import json

            if proc.returncode != 0:
                logger.debug(f"读图 OCR 进程返回 {proc.returncode}: {proc.stderr[:120]}")
                return ""
            payload = json.loads(proc.stdout or "{}")
            return str(payload.get("text", "") or "").strip()
        except subprocess.TimeoutExpired:
            logger.warning(f"读图 OCR 超时（>{self.ocr_timeout:.0f}s）: {image_path}")
            return ""
        except Exception as exc:
            logger.warning(f"读图 OCR 失败: {exc}")
            return ""
