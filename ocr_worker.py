"""独立图片 OCR 工作进程，避免 WinRT OCR 卡住机器人主事件循环。

命令行：
    python ocr_worker.py <图片路径>            只输出 {"text": "..."}
    python ocr_worker.py <图片路径> --lines    额外输出每行的文字与坐标
                                               {"text": "...", "lines": [{"text","x","y","w","h"}]}

坐标以**原图像素**为准（内部放大后会把坐标换算回去），供"在截图里点某一行"用。
"""

import asyncio
import json
import sys
import uuid
from pathlib import Path


async def extract(image_path: str, with_lines: bool = False) -> dict:
    from PIL import Image, ImageEnhance, ImageOps
    from winsdk.windows.graphics.imaging import BitmapDecoder
    from winsdk.windows.media.ocr import OcrEngine
    from winsdk.windows.storage import FileAccessMode, StorageFile

    image = Image.open(image_path)
    if getattr(image, "n_frames", 1) > 1:
        image.seek(0)
    image = ImageOps.exif_transpose(image)
    original_w, original_h = image.size
    longest = max(image.size)
    if longest < 1600:
        scale = min(2.5, 1600 / max(1, longest))
        image = image.resize(
            (
                max(1, int(image.width * scale)),
                max(1, int(image.height * scale)),
            )
        )
    scaled_w, scaled_h = image.size
    image = ImageEnhance.Contrast(image.convert("L")).enhance(1.6)

    temporary_dir = Path.cwd() / "data" / "ocr_temp"
    temporary_dir.mkdir(parents=True, exist_ok=True)
    temporary = temporary_dir / f"{uuid.uuid4().hex}.png"
    try:
        image.save(temporary, format="PNG")
        storage_file = await StorageFile.get_file_from_path_async(str(temporary))
        stream = await storage_file.open_async(FileAccessMode.READ)
        decoder = await BitmapDecoder.create_async(stream)
        bitmap = await decoder.get_software_bitmap_async()
        engine = OcrEngine.try_create_from_user_profile_languages()
        if not engine:
            return {"text": "", "lines": []}
        result = await engine.recognize_async(bitmap)
        text = (result.text or "").strip()

        lines: list[dict] = []
        if with_lines:
            # OCR 在放大后的图上做，坐标要按比例换回原图
            sx = original_w / max(1, scaled_w)
            sy = original_h / max(1, scaled_h)
            for line in result.lines or []:
                words = list(line.words or [])
                if not words:
                    continue
                x0 = min(w.bounding_rect.x for w in words) * sx
                y0 = min(w.bounding_rect.y for w in words) * sy
                x1 = max(w.bounding_rect.x + w.bounding_rect.width for w in words) * sx
                y1 = max(w.bounding_rect.y + w.bounding_rect.height for w in words) * sy
                lines.append(
                    {
                        "text": (line.text or "").strip(),
                        "x": int(round(x0)),
                        "y": int(round(y0)),
                        "w": int(round(x1 - x0)),
                        "h": int(round(y1 - y0)),
                    }
                )
        return {"text": text, "lines": lines}
    finally:
        try:
            temporary.unlink(missing_ok=True)
        except OSError:
            pass


def main() -> None:
    payload: dict = {"text": "", "lines": []}
    args = [a for a in sys.argv[1:] if a != "--lines"]
    want_lines = "--lines" in sys.argv[1:]
    if args:
        try:
            payload = asyncio.run(extract(args[0], with_lines=want_lines))
        except Exception:
            payload = {"text": "", "lines": []}
    if not want_lines:
        payload.pop("lines", None)
    print(json.dumps(payload, ensure_ascii=False))


if __name__ == "__main__":
    main()
