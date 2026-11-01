"""通用工具函数。"""

from __future__ import annotations

import os
import re
import time
import unicodedata

_INVALID_CHARS = re.compile(r'[\\/:*?"<>|\x00-\x1f]+')
_MULTI_SPACE = re.compile(r"\s+")

_SEGMENT_EXTS = {".ts", ".m4s", ".mp4", ".aac", ".m4a", ".mp3"}


def sanitize_filename(name: str, max_length: int = 100) -> str:
    """把页面标题等文本清理为合法的文件/文件夹名。"""
    name = (name or "").strip()
    name = _INVALID_CHARS.sub("_", name)
    # 去掉控制符、私用区等无法可靠显示/写入的字符（例如编码错误的网页标题）。
    name = "".join(ch for ch in name if not unicodedata.category(ch).startswith("C"))
    name = _MULTI_SPACE.sub(" ", name).strip(" ._")
    if len(name) > max_length:
        name = name[:max_length].rstrip(" .")
    return name or time.strftime("video_%Y%m%d_%H%M%S")


def unique_path(base_dir: str, name: str, suffix: str = "") -> str:
    """在 base_dir 下生成不冲突的路径：name、name_1、name_2 ..."""
    stem, ext = os.path.splitext(name + suffix)
    path = os.path.join(base_dir, stem + ext)
    index = 1
    while os.path.exists(path):
        path = os.path.join(base_dir, f"{stem}_{index}{ext}")
        index += 1
    return path


def human_size(num: float) -> str:
    num = float(num or 0)
    for unit in ("B", "KB", "MB", "GB"):
        if num < 1024 or unit == "GB":
            return f"{num:.0f} B" if unit == "B" else f"{num:.1f} {unit}"
        num /= 1024
    return f"{num:.1f} GB"


def human_duration(seconds: float) -> str:
    seconds = max(0, int(seconds or 0))
    hours, rem = divmod(seconds, 3600)
    minutes, secs = divmod(rem, 60)
    if hours:
        return f"{hours}:{minutes:02d}:{secs:02d}"
    return f"{minutes:02d}:{secs:02d}"


def guess_segment_ext(url: str) -> str:
    """根据片段 URL 猜测扩展名，未知时按 .ts 处理。"""
    path = url.split("?", 1)[0].split("#", 1)[0]
    ext = os.path.splitext(path)[1].lower()
    return ext if ext in _SEGMENT_EXTS else ".ts"
