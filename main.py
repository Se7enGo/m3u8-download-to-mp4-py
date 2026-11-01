"""M3U8 视频下载工具 - 程序入口。"""

from __future__ import annotations

import os
import queue
import shutil
import sys
from urllib.parse import urlparse

import requests
from rich.console import Console
from rich.panel import Panel
from rich.prompt import Confirm, Prompt
from rich.table import Table

from browser_monitor import BrowserMonitor, Detection
from config import Config
from downloader import Downloader
from m3u8_handler import EncryptedStreamError, M3U8Handler, M3U8Info, ParseError
from utils import human_duration, human_size, sanitize_filename, unique_path
from video_merger import VideoMerger

DEFAULT_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)


def shorten_url(url: str, limit: int = 76) -> str:
    if len(url) <= limit:
        return url
    keep = (limit - 3) // 2
    return url[:keep] + "..." + url[-keep:]


class App:
    def __init__(self, config: Config) -> None:
        self.config = config
        self.console = Console()
        self.monitor = BrowserMonitor(config)
        self.merger = VideoMerger(self.console)

    # ---------- 主循环 ----------

    def run(self) -> int:
        self._print_banner()
        if not VideoMerger.find_ffmpeg():
            self.console.print(
                Panel(
                    "[yellow]未检测到 ffmpeg，下载完成后将无法合并为 mp4。[/]\n\n"
                    "Windows 可执行：[bold]winget install ffmpeg[/] 或 [bold]choco install ffmpeg[/]\n"
                    "安装完成后请重新打开终端再运行本程序。",
                    title="环境提示",
                    border_style="yellow",
                )
            )
        self.config.ensure_dirs()

        self.console.print("[cyan]正在启动浏览器，请稍候...[/]")
        try:
            self.monitor.start()
        except Exception as exc:
            self.console.print(f"[red]浏览器启动失败：{exc}[/]")
            return 1

        self.console.print(
            Panel(
                "1. 在打开的浏览器中访问视频网站，登录并播放或刷新页面\n"
                "2. 新开标签页同样会被监听，程序检测到 m3u8 后会在终端提示\n"
                "3. 关闭浏览器窗口或按 Ctrl+C 退出程序",
                title="使用说明",
                border_style="cyan",
            )
        )

        try:
            while not self.monitor.closed.is_set():
                self.console.print("[dim]等待 m3u8 链接中...[/]")
                batch = self._collect_batch()
                if self.monitor.closed.is_set():
                    break
                if batch:
                    self._handle_batch(batch)
        except KeyboardInterrupt:
            self.console.print("\n[dim]收到退出指令，正在关闭浏览器...[/]")
        finally:
            self.monitor.stop(close_browser=True)
        if self.monitor.closed.is_set():
            self.console.print("[yellow]浏览器已关闭，程序退出。[/]")
        return 0

    def _print_banner(self) -> None:
        self.console.print(
            Panel.fit(
                "[bold cyan]M3U8 视频下载工具[/]\n[dim]DrissionPage + FFmpeg[/]",
                border_style="cyan",
            )
        )

    def _collect_batch(self) -> list[Detection]:
        """等待第一个链接，然后把 1.5 秒内陆续出现的链接一起收进来。"""
        first = self.monitor.queue.get()
        if first is None:  # 浏览器已关闭
            return []
        items = [first]
        while True:
            try:
                item = self.monitor.queue.get(timeout=1.5)
            except queue.Empty:
                break
            if item is None:  # 批次收集期间浏览器被关闭
                break
            items.append(item)
        return items

    # ---------- 检测结果处理 ----------

    def _handle_batch(self, batch: list[Detection]) -> None:
        if self.monitor.closed.is_set():
            return
        self.console.print(f"[green]捕获到 {len(batch)} 个候选链接，正在解析...[/]")

        candidates: list[tuple[M3U8Info, Detection]] = []
        seen: set[tuple] = set()
        sessions: dict[str, requests.Session] = {}
        for detection in batch:
            detection = self.monitor.refresh_detection(detection)
            session = self._session_for(detection, sessions)
            handler = M3U8Handler(session, timeout=self.config.timeout)
            info = self._parse_candidate(handler, detection.url, detection.body)
            if info is None or info.signature in seen:
                continue
            seen.add(info.signature)
            candidates.append((info, detection))

        if not candidates:
            self.console.print("[dim]这批链接都不可用，继续监听...[/]")
            return

        if self.monitor.closed.is_set():
            return
        selected = self._choose_candidate(candidates)
        if selected is None:
            return
        info, detection = selected

        self._show_detail(info)
        if self.monitor.closed.is_set():
            return
        if not Confirm.ask("是否开始下载？", default=True, console=self.console):
            self.console.print("[dim]已取消，继续监听...[/]")
            return
        self._download_and_merge(info, detection, sessions[detection.tab_id])

    def _parse_candidate(
        self,
        handler: M3U8Handler,
        url: str,
        body: str | None,
    ) -> M3U8Info | None:
        try:
            return handler.parse(url, body)
        except EncryptedStreamError as exc:
            self.console.print(
                f"[yellow]跳过加密视频（{exc.method or '未知方式'}），当前版本不支持：[/]"
                f" {shorten_url(url)}"
            )
            return None
        except ParseError as exc:
            if not body:
                self.console.print(f"[dim]跳过 {shorten_url(url)}：{exc}[/]")
                return None
            # 浏览器抓到的响应体可能不可靠，重新请求一次再试。
            try:
                return handler.parse(url)
            except EncryptedStreamError as retry_exc:
                self.console.print(
                    f"[yellow]跳过加密视频（{retry_exc.method or '未知方式'}），当前版本不支持：[/]"
                    f" {shorten_url(url)}"
                )
                return None
            except (ParseError, requests.RequestException) as retry_exc:
                self.console.print(f"[dim]跳过 {shorten_url(url)}：{retry_exc}[/]")
                return None
        except requests.RequestException as exc:
            self.console.print(f"[yellow]获取 m3u8 失败 {shorten_url(url)}：{exc}[/]")
            return None

    def _choose_candidate(
        self,
        candidates: list[tuple[M3U8Info, Detection]],
    ) -> tuple[M3U8Info, Detection] | None:
        if len(candidates) == 1:
            return candidates[0]

        table = Table(title="检测到多个 m3u8 链接", border_style="cyan")
        table.add_column("#", justify="right")
        table.add_column("清晰度")
        table.add_column("片段数", justify="right")
        table.add_column("预估时长", justify="right")
        table.add_column("地址", overflow="fold")
        for index, (info, _) in enumerate(candidates, 1):
            table.add_row(
                str(index),
                info.quality or "未知",
                str(info.segment_count),
                human_duration(info.duration),
                shorten_url(info.media_url),
            )
        self.console.print(table)

        choices = [str(i) for i in range(0, len(candidates) + 1)]
        choice = Prompt.ask(
            "请选择要下载的视频（0 = 不下载）",
            choices=choices,
            default="1",
            console=self.console,
        )
        if choice == "0":
            self.console.print("[dim]已跳过，继续监听...[/]")
            return None
        return candidates[int(choice) - 1]

    def _show_detail(self, info: M3U8Info) -> None:
        segment_names = [self._segment_name(url) for url in info.segments]
        lines = [
            f"[bold]M3U8 地址：[/]{shorten_url(info.media_url, 90)}",
            f"[bold]片段数量：[/]{info.segment_count} 个",
            f"[bold]预估时长：[/]{human_duration(info.duration)}",
            f"[bold]清晰度：[/]{info.quality or '未知'}（自动选择最低清晰度）",
            "[bold]加密状态：[/]未加密",
            "",
            "[bold]片段列表（缩略）：[/]",
        ]
        if len(segment_names) <= 10:
            shown = segment_names
            for index, name in enumerate(shown):
                lines.append(f"  [{index + 1:03d}] {name}")
        else:
            for index, name in enumerate(segment_names[:5]):
                lines.append(f"  [{index + 1:03d}] {name}")
            lines.append(f"  ...[省略 {len(segment_names) - 10} 个]...")
            start = len(segment_names) - 5
            for offset, name in enumerate(segment_names[-5:]):
                lines.append(f"  [{start + offset + 1:03d}] {name}")

        if info.is_live:
            lines.append("")
            lines.append("[yellow]注意：未找到 ENDLIST，疑似直播/滚动播放列表，[/]")
            lines.append("[yellow]可能无法下载完整内容。[/]")

        self.console.print(Panel("\n".join(lines), title="视频信息", border_style="cyan"))

    @staticmethod
    def _segment_name(url: str) -> str:
        name = urlparse(url).path.rsplit("/", 1)[-1] or url
        return name if len(name) <= 60 else name[:57] + "..."

    # ---------- 下载与合并 ----------

    def _download_and_merge(
        self,
        info: M3U8Info,
        detection: Detection,
        session: requests.Session,
    ) -> None:
        if self.monitor.closed.is_set():
            return
        default_name = sanitize_filename(detection.title or "video", self.config.max_filename_length)
        name = Prompt.ask("视频名称", default=default_name, console=self.console)
        name = sanitize_filename(name or default_name, self.config.max_filename_length)

        temp_dir = unique_path(self.config.temp_dir, name)
        os.makedirs(temp_dir, exist_ok=True)
        self.console.print(f"[dim]临时目录：{temp_dir}[/]")

        headers = dict(session.headers)
        cookies = session.cookies.get_dict()
        downloader = Downloader(self.config, self.console, cancel_event=self.monitor.closed)
        result = downloader.download(info, temp_dir, headers, cookies)

        if self.monitor.closed.is_set():
            self.console.print(
                Panel(
                    "[yellow]浏览器已关闭，已停止下载。[/]\n\n"
                    f"临时文件保留在：{temp_dir}",
                    title="已取消",
                    border_style="yellow",
                )
            )
            return

        if not result.success:
            self.console.print(
                Panel(
                    "[red]没有任何片段下载成功，本次任务结束。[/]\n\n"
                    f"失败片段：{len(result.failed)} 个\n"
                    f"临时目录保留在：{temp_dir}",
                    title="下载失败",
                    border_style="red",
                )
            )
            return

        if result.failed:
            preview = "\n".join(
                f"  [red]×[/] {filename} - {reason}" for filename, reason in result.failed[:10]
            )
            more = f"\n  … 另有 {len(result.failed) - 10} 个" if len(result.failed) > 10 else ""
            self.console.print(
                Panel(
                    f"{preview}{more}\n\n"
                    f"[yellow]共 {len(result.failed)} 个片段失败，合并后将缺少这部分内容。[/]",
                    title="部分片段失败",
                    border_style="yellow",
                )
            )
            if self.monitor.closed.is_set():
                self.console.print(f"[dim]浏览器已关闭，未合并，临时文件保留在：{temp_dir}[/]")
                return
            if not Confirm.ask("是否继续合并？", default=True, console=self.console):
                self.console.print(f"[dim]已保留临时文件：{temp_dir}[/]")
                return

        segment_files = ([result.init_file] if result.init_file else []) + sorted(result.success)
        output_path = unique_path(self.config.output_dir, name, ".mp4")
        ok, error = self.merger.merge(temp_dir, segment_files, output_path, info.duration)

        if not ok:
            self.console.print(
                Panel(
                    f"[red]合并失败：{error}[/]\n\n片段已保存在：{temp_dir}",
                    title="合并失败",
                    border_style="red",
                )
            )
            return

        size = os.path.getsize(output_path)
        self.console.print(
            Panel(
                f"[green]视频已保存到：[/]{output_path}\n"
                f"大小：{human_size(size)}    成功片段：{len(result.success)}/{result.total}\n"
                f"失败：{len(result.failed)}    重试：{result.retries}    "
                f"用时：{human_duration(result.elapsed)}",
                title="下载完成",
                border_style="green",
            )
        )

        if self.monitor.closed.is_set():
            self.console.print(f"[dim]浏览器已关闭，临时文件保留在：{temp_dir}[/]")
            return
        if Confirm.ask("是否删除临时文件？", default=True, console=self.console):
            shutil.rmtree(temp_dir, ignore_errors=True)
        self.console.print("[dim]继续监听中...[/]")

    # ---------- 请求会话 ----------

    def _session_for(
        self,
        detection: Detection,
        cache: dict[str, requests.Session],
    ) -> requests.Session:
        """同一标签页复用会话，带上该页面的 UA、Referer 和 Cookie。"""
        session = cache.get(detection.tab_id)
        if session is not None:
            return session
        session = requests.Session()
        session.headers.update(
            {
                "User-Agent": detection.user_agent or DEFAULT_UA,
                "Accept": "*/*",
                "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
            }
        )
        if detection.page_url:
            session.headers["Referer"] = detection.page_url
        if detection.cookies:
            session.cookies.update(detection.cookies)
        cache[detection.tab_id] = session
        return session


def _configure_stdio() -> None:
    """避免个别字符（如异常网页标题里的私用区字符）让终端输出直接崩溃。"""
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(errors="replace")
        except Exception:
            pass


def main() -> int:
    _configure_stdio()
    config = Config.from_args()
    return App(config).run()


if __name__ == "__main__":
    sys.exit(main())
