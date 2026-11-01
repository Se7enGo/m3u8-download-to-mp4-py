"""M3U8 解析：合并主播放列表的清晰度信息，展开最低清晰度并提取 ts 片段。"""

from __future__ import annotations

import re
from dataclasses import dataclass
from urllib.parse import urljoin

import m3u8


class ParseError(Exception):
    """m3u8 解析失败。"""


class EncryptedStreamError(ParseError):
    """视频已加密，当前版本不支持下载。"""

    def __init__(self, method: str, key_url: str) -> None:
        super().__init__(f"视频已加密（{method}）")
        self.method = method
        self.key_url = key_url


@dataclass
class M3U8Info:
    url: str
    media_url: str
    segments: list[str]
    duration: float
    encrypted: bool = False
    encryption_method: str = ""
    key_url: str = ""
    is_live: bool = False
    init_segment: str = ""
    quality: str = ""

    @property
    def segment_count(self) -> int:
        return len(self.segments)

    @property
    def signature(self) -> tuple:
        """用于去掉主列表和媒体列表重复出现的指纹。"""
        if not self.segments:
            return (self.media_url,)
        return (self.segments[0], self.segments[-1], len(self.segments))


class M3U8Handler:
    """解析 m3u8。传入的 session 需已携带浏览器的 Cookie/Referer。"""

    def __init__(self, session, timeout: float = 30.0) -> None:
        self.session = session
        self.timeout = timeout

    def parse(self, url: str, body: str | None = None) -> M3U8Info:
        text = (body or "").strip() or self._fetch_text(url)
        info = self._parse_text(url, text, depth=0)
        info.url = url
        return info

    def _fetch_text(self, url: str) -> str:
        resp = self.session.get(url, timeout=self.timeout)
        resp.raise_for_status()
        return resp.text

    def _parse_text(self, url: str, text: str, depth: int) -> M3U8Info:
        if depth > 3:
            raise ParseError("m3u8 嵌套层数过多")
        try:
            obj = m3u8.loads(text, uri=url)
        except Exception as exc:  # m3u8 库对异常输入抛出的类型不统一
            raise ParseError(f"m3u8 格式错误：{exc}") from exc

        if obj.is_variant:
            playlists = [p for p in obj.playlists if getattr(p, "stream_info", None)]
            if not playlists:
                raise ParseError("主播放列表中没有可用的清晰度")
            playlists.sort(key=lambda p: (getattr(p.stream_info, "bandwidth", None) or 0))
            chosen = playlists[0]  # 按需求自动选择最低清晰度
            media_url = urljoin(url, chosen.uri)
            info = self._parse_text(media_url, self._fetch_text(media_url), depth + 1)
            info.quality = self._quality_label(chosen)
            return info

        method, key_url = self._detect_encryption(obj, text, url)
        if method:
            raise EncryptedStreamError(method, key_url)

        segments = []
        for segment in obj.segments:
            uri = getattr(segment, "uri", "") or ""
            if uri:
                segments.append(urljoin(url, uri))
        if not segments:
            raise ParseError("播放列表中没有 ts 片段（可能是空列表）")

        duration = sum(float(getattr(s, "duration", 0) or 0) for s in obj.segments)
        if duration <= 0 and obj.target_duration:
            duration = float(obj.target_duration) * len(segments)

        init_segment = ""
        try:
            if obj.segment_map:
                init_segment = urljoin(url, obj.segment_map[0].uri)
        except Exception:
            pass

        return M3U8Info(
            url=url,
            media_url=url,
            segments=segments,
            duration=duration,
            is_live=not obj.is_endlist,
            init_segment=init_segment,
        )

    @staticmethod
    def _detect_encryption(obj, text: str, base_url: str) -> tuple[str, str]:
        for key in getattr(obj, "keys", []) or []:
            method = (getattr(key, "method", "") or "").upper()
            if method and method != "NONE":
                return method, urljoin(base_url, getattr(key, "uri", "") or "")
        match = re.search(r"#EXT-X-KEY:[^\n]*METHOD=([A-Za-z0-9\-]+)", text)
        if match and match.group(1).upper() != "NONE":
            return match.group(1).upper(), ""
        return "", ""

    @staticmethod
    def _quality_label(playlist) -> str:
        info = playlist.stream_info
        parts = []
        resolution = getattr(info, "resolution", None)
        if resolution and len(resolution) == 2:
            parts.append(f"{resolution[0]}x{resolution[1]}")
        bandwidth = getattr(info, "bandwidth", None)
        if bandwidth:
            parts.append(f"{bandwidth / 1000:.0f} kbps")
        return " ".join(parts) or "未知清晰度"
