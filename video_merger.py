"""使用 FFmpeg 把 ts 片段无损合并为 mp4。"""

from __future__ import annotations

import os
import shutil
import subprocess
import threading

from rich.console import Console


class VideoMerger:
    def __init__(self, console: Console) -> None:
        self.console = console

    @staticmethod
    def find_ffmpeg() -> str | None:
        return shutil.which("ffmpeg")

    def merge(
        self,
        ts_dir: str,
        segment_files: list[str],
        output_path: str,
        duration: float = 0.0,
    ) -> tuple[bool, str]:
        ffmpeg = self.find_ffmpeg()
        if not ffmpeg:
            return (
                False,
                "未检测到 ffmpeg，请先安装（Windows: winget install ffmpeg / "
                "choco install ffmpeg），安装后重新打开终端再试。",
            )

        list_path = os.path.join(ts_dir, "filelist.txt")
        with open(list_path, "w", encoding="utf-8") as handle:
            for name in segment_files:
                safe_name = name.replace("'", "'\\''")
                handle.write(f"file '{safe_name}'\n")

        command = [
            ffmpeg,
            "-hide_banner",
            "-y",
            "-f", "concat",
            "-safe", "0",
            "-i", list_path,
            "-c", "copy",
            "-movflags", "+faststart",
            "-progress", "pipe:1",
            "-nostats",
            output_path,
        ]
        try:
            process = subprocess.Popen(
                command,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                encoding="utf-8",
                errors="ignore",
            )
        except OSError as exc:
            return False, f"无法启动 ffmpeg：{exc}"

        stderr_lines: list[str] = []

        def drain_stderr() -> None:
            assert process.stderr is not None
            for line in process.stderr:
                stderr_lines.append(line)
                if len(stderr_lines) > 100:
                    del stderr_lines[0]

        reader = threading.Thread(target=drain_stderr, daemon=True)
        reader.start()

        with self.console.status("[cyan]正在合并视频...[/]") as status:
            assert process.stdout is not None
            for raw_line in process.stdout:
                line = raw_line.strip()
                if line.startswith("out_time_ms=") or line.startswith("out_time_us="):
                    _, _, value = line.partition("=")
                    try:
                        seconds = int(value) / 1_000_000
                    except ValueError:
                        continue
                    if duration > 0:
                        percent = min(seconds / duration, 1.0)
                        status.update(f"[cyan]正在合并视频...[/] {percent:.0%}")
                elif line == "progress=end":
                    status.update("[cyan]正在合并视频...[/] 100%")
            code = process.wait()
        reader.join(timeout=1)

        if code != 0 or not os.path.exists(output_path):
            tail = "".join(stderr_lines[-15:]).strip()
            return False, tail or f"ffmpeg 退出码 {code}"
        return True, ""
