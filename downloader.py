"""ts 片段下载器：4 并发、全局限速、实时进度、失败暂停与重试。"""

from __future__ import annotations

import os
import random
import threading
import time
from collections import deque
from concurrent.futures import FIRST_COMPLETED, Future, ThreadPoolExecutor, wait
from dataclasses import dataclass, field

import requests
from rich.console import Console
from rich.live import Live
from rich.markup import escape
from rich.panel import Panel
from rich.progress_bar import ProgressBar
from rich.prompt import Prompt
from rich.table import Table
from rich.text import Text

from config import Config
from m3u8_handler import M3U8Info
from utils import guess_segment_ext, human_duration, human_size

_STATUS_HINTS = {
    401: "需要登录或 Cookie 已失效",
    403: "被拒绝，可能触发了服务器限制",
    404: "文件不存在",
    410: "文件已失效",
}


class _AbortedError(Exception):
    """用户放弃任务。"""


class _NoRetryError(Exception):
    """确定性失败（如 404），无需重试。"""


@dataclass
class SegmentTask:
    index: int
    url: str
    filename: str
    status: str = "pending"  # pending / downloading / success / failed / cancelled
    error: str = ""
    attempts: int = 0
    size: int = 0
    total_size: int = 0


@dataclass
class DownloadResult:
    total: int
    success: list[str] = field(default_factory=list)
    failed: list[tuple[str, str]] = field(default_factory=list)
    cancelled: int = 0
    elapsed: float = 0.0
    bytes_total: int = 0
    retries: int = 0
    init_file: str = ""


class DownloadDisplay:
    """rich.Live 的动态渲染对象，所有下载线程共享其状态。"""

    def __init__(self, title: str, tasks: list[SegmentTask]) -> None:
        self.title = title
        self.tasks = tasks
        self.by_index = {task.index: task for task in tasks}
        self.total = len(tasks)
        self.lock = threading.RLock()
        self.completed = 0
        self.failed_count = 0
        self.retries = 0
        self.bytes_done = 0
        self.start_time = time.monotonic()
        self.events: deque[str] = deque(maxlen=3)
        self._samples: deque[tuple[float, int]] = deque(maxlen=40)

    # ---------- 由下载线程调用 ----------

    def set_downloading(self, index: int, filename: str, total_size: int) -> None:
        with self.lock:
            task = self.by_index[index]
            task.status = "downloading"
            task.filename = filename
            task.total_size = total_size
            task.size = 0

    def add_bytes(self, index: int, count: int) -> None:
        with self.lock:
            task = self.by_index[index]
            task.size += count
            self.bytes_done += count
            now = time.monotonic()
            if not self._samples or now - self._samples[-1][0] >= 0.5:
                self._samples.append((now, self.bytes_done))

    def mark_retry(self) -> None:
        with self.lock:
            self.retries += 1

    # ---------- 由调度线程调用 ----------

    def set_success(self, index: int, filename: str) -> None:
        with self.lock:
            task = self.by_index[index]
            task.status = "success"
            task.filename = filename
            self.completed += 1

    def set_failed(self, index: int, filename: str, reason: str) -> None:
        with self.lock:
            task = self.by_index[index]
            task.status = "failed"
            task.filename = filename
            task.error = reason
            self.completed += 1
            self.failed_count += 1
            self.events.appendleft(f"{filename} - {reason}")

    def reset_failed(self, indexes: list[int]) -> None:
        with self.lock:
            for index in indexes:
                task = self.by_index[index]
                if task.status == "failed":
                    task.status = "pending"
                    task.error = ""
                    task.size = 0
                    self.completed = max(0, self.completed - 1)
            self.failed_count = 0
            self.events.clear()

    # ---------- 渲染 ----------

    def _speed_locked(self) -> float:
        now = time.monotonic()
        while self._samples and now - self._samples[0][0] > 4:
            self._samples.popleft()
        if len(self._samples) < 2:
            return 0.0
        delta = self._samples[-1][0] - self._samples[0][0]
        if delta <= 0:
            return 0.0
        return (self._samples[-1][1] - self._samples[0][1]) / delta

    def __rich_console__(self, console: Console, options):
        with self.lock:
            completed = self.completed
            failed = self.failed_count
            elapsed = max(time.monotonic() - self.start_time, 0.001)
            speed = self._speed_locked()
            active = sorted(
                (task for task in self.tasks if task.status == "downloading"),
                key=lambda task: task.index,
            )
            events = list(self.events)

        percent = completed / self.total if self.total else 0.0
        progress_line = Table.grid(expand=True)
        progress_line.add_column(ratio=1, no_wrap=True)
        progress_line.add_column(width=20, justify="right", no_wrap=True)
        progress_line.add_row(
            ProgressBar(total=self.total, completed=completed),
            Text(f"{completed}/{self.total} {percent:.1%}", style="bold"),
        )

        yield Text(f"下载中：{self.title}", style="bold cyan")
        yield progress_line
        yield Text(
            f"成功 {completed - failed}   失败 {failed}   重试 {self.retries}   "
            f"用时 {human_duration(elapsed)}   速度 {human_size(speed)}/s",
            style="dim",
        )

        if active:
            grid = Table.grid(padding=(0, 2))
            grid.add_column(width=10, no_wrap=True)
            grid.add_column(no_wrap=True)
            grid.add_column(justify="right", no_wrap=True)
            for task in active:
                if task.total_size:
                    # 服务器给的 Content-Length 偶尔偏小（压缩/估算值），比例封顶 100%。
                    ratio = min(1.0, task.size / task.total_size)
                    detail = f"{human_size(task.size)} / {human_size(task.total_size)}  {ratio:.0%}"
                else:
                    detail = human_size(task.size)
                grid.add_row(
                    Text(f"{task.index + 1}/{self.total}", style="dim"),
                    Text(task.filename),
                    Text(detail, style="cyan"),
                )
            yield grid

        if events:
            yield Text("最近失败：", style="bold red")
            for event in events:
                yield Text.from_markup(f"[red]×[/] {escape(event)}")


class Downloader:
    def __init__(
        self,
        config: Config,
        console: Console,
        cancel_event: threading.Event | None = None,
    ) -> None:
        self.config = config
        self.console = console
        self._pause = threading.Event()
        self._abort = threading.Event()
        self._cancel = cancel_event
        self._pace_lock = threading.Lock()
        self._next_request_at = 0.0

    def _cancelled(self) -> bool:
        """外部取消信号，例如用户关闭了浏览器。"""
        return self._cancel is not None and self._cancel.is_set()

    # ---------- 对外入口 ----------

    def download(
        self,
        info: M3U8Info,
        save_dir: str,
        headers: dict[str, str],
        cookies: dict[str, str],
    ) -> DownloadResult:
        os.makedirs(save_dir, exist_ok=True)
        self._pause.clear()
        self._abort.clear()
        started_at = time.monotonic()

        if self._cancelled():
            return DownloadResult(
                total=info.segment_count,
                cancelled=info.segment_count,
                elapsed=time.monotonic() - started_at,
            )

        init_name = ""
        if info.init_segment:
            init_name = self._download_init_segment(info.init_segment, save_dir, headers, cookies)
            if not init_name:
                return DownloadResult(
                    total=info.segment_count,
                    failed=[("init.mp4", "初始化片段下载失败")],
                    elapsed=time.monotonic() - started_at,
                )

        tasks = [
            SegmentTask(index, url, f"seg-{index:05d}{guess_segment_ext(url)}")
            for index, url in enumerate(info.segments)
        ]
        display = DownloadDisplay(f"{info.segment_count} 个片段", tasks)

        thread_local = threading.local()

        def get_session() -> requests.Session:
            session = getattr(thread_local, "session", None)
            if session is None:
                session = requests.Session()
                session.headers.update(headers)
                if cookies:
                    session.cookies.update(cookies)
                thread_local.session = session
            return session

        def task_worker(task: SegmentTask) -> None:
            self._run_segment(task, save_dir, get_session(), display)

        pending: deque[int] = deque(range(len(tasks)))
        running: dict[Future, int] = {}
        failures: dict[int, SegmentTask] = {}
        errors_since_ack = 0

        with Live(display, console=self.console, refresh_per_second=10) as live:
            with ThreadPoolExecutor(max_workers=self.config.max_workers) as executor:
                try:
                    while pending or running:
                        if self._abort.is_set() or self._cancelled():
                            self._abort.set()
                            pending.clear()
                        while (
                            pending
                            and not self._pause.is_set()
                            and not self._abort.is_set()
                            and len(running) < self.config.max_workers
                        ):
                            index = pending.popleft()
                            running[executor.submit(task_worker, tasks[index])] = index

                        if not running:
                            if self._abort.is_set() or not pending:
                                break
                            self._pause.clear()
                            continue

                        done, _ = wait(list(running), timeout=0.25, return_when=FIRST_COMPLETED)
                        for future in done:
                            index = running.pop(future)
                            task = tasks[index]
                            exc = future.exception()
                            if exc is not None:
                                task.status = "failed"
                                task.error = self._describe_error(exc)
                            if task.status == "success":
                                display.set_success(index, task.filename)
                            elif task.status == "failed":
                                display.set_failed(index, task.filename, task.error)
                                if index not in failures:
                                    errors_since_ack += 1
                                failures[index] = task

                        if not self._abort.is_set() and errors_since_ack >= self.config.error_threshold:
                            self._pause.set()
                            action = self._prompt_pause(live, failures)
                            if self._cancelled():
                                self._abort.set()
                                pending.clear()
                            elif action == "2":
                                retry_indexes = list(failures)
                                display.reset_failed(retry_indexes)
                                failures.clear()
                                for index in reversed(retry_indexes):
                                    pending.appendleft(index)
                            elif action == "3":
                                self._abort.set()
                                pending.clear()
                            errors_since_ack = 0
                            self._pause.clear()
                except KeyboardInterrupt:
                    self._abort.set()
                    self._pause.clear()
                    raise

        elapsed = time.monotonic() - started_at
        success = sorted(task.filename for task in tasks if task.status == "success")
        failed = [(task.filename, task.error) for task in tasks if task.status == "failed"]
        cancelled = sum(1 for task in tasks if task.status in ("cancelled", "pending"))
        return DownloadResult(
            total=len(tasks),
            success=success,
            failed=failed,
            cancelled=cancelled,
            elapsed=elapsed,
            bytes_total=sum(task.size for task in tasks if task.status == "success"),
            retries=display.retries,
            init_file=init_name,
        )

    # ---------- 单个片段 ----------

    def _run_segment(
        self,
        task: SegmentTask,
        save_dir: str,
        session: requests.Session,
        display: DownloadDisplay,
    ) -> None:
        path = os.path.join(save_dir, task.filename)
        tmp_path = path + ".part"
        last_error = ""

        for attempt in range(1, self.config.retry_times + 1):
            if self._abort.is_set() or self._cancelled():
                task.status = "cancelled"
                return
            while self._pause.is_set() and not self._abort.is_set() and not self._cancelled():
                time.sleep(0.15)
            if self._abort.is_set() or self._cancelled():
                task.status = "cancelled"
                return

            task.attempts = attempt
            if attempt > 1:
                display.mark_retry()
                time.sleep(min(2 ** (attempt - 2), 4) + random.uniform(0, 0.5))

            try:
                self._wait_turn()
                if self._abort.is_set() or self._cancelled():
                    task.status = "cancelled"
                    return
                display.set_downloading(task.index, task.filename, 0)
                with session.get(task.url, stream=True, timeout=self.config.timeout) as resp:
                    self._raise_for_status(resp)
                    total = int(resp.headers.get("Content-Length") or 0)
                    display.set_downloading(task.index, task.filename, total)
                    with open(tmp_path, "wb") as handle:
                        for chunk in resp.iter_content(chunk_size=64 * 1024):
                            if self._abort.is_set() or self._cancelled():
                                raise _AbortedError()
                            if chunk:
                                handle.write(chunk)
                                # 字节计数统一交给 display 在锁内累加，避免重复计账。
                                display.add_bytes(task.index, len(chunk))
                os.replace(tmp_path, path)
                task.status = "success"
                task.error = ""
                return
            except _NoRetryError as exc:
                last_error = str(exc)
                self._cleanup(tmp_path)
                break
            except _AbortedError:
                task.status = "cancelled"
                self._cleanup(tmp_path)
                return
            except Exception as exc:
                last_error = self._describe_error(exc)
                self._cleanup(tmp_path)

        task.status = "failed"
        task.error = last_error or "未知错误"

    def _download_init_segment(
        self,
        url: str,
        save_dir: str,
        headers: dict[str, str],
        cookies: dict[str, str],
    ) -> str:
        name = "init.mp4"
        path = os.path.join(save_dir, name)
        if os.path.exists(path) and os.path.getsize(path) > 0:
            return name
        for attempt in range(1, self.config.retry_times + 1):
            try:
                with self.console.status("[cyan]下载初始化片段...[/]"):
                    resp = requests.get(url, headers=headers, cookies=cookies, timeout=self.config.timeout)
                    resp.raise_for_status()
                    with open(path, "wb") as handle:
                        handle.write(resp.content)
                return name
            except Exception:
                if attempt < self.config.retry_times:
                    time.sleep(attempt)
        return ""

    # ---------- 限速与错误 ----------

    def _wait_turn(self) -> None:
        """全局请求节流：任意两次请求之间至少间隔 request_interval 秒。"""
        with self._pace_lock:
            now = time.monotonic()
            if now < self._next_request_at:
                time.sleep(self._next_request_at - now)
            self._next_request_at = time.monotonic() + self.config.request_interval

    @staticmethod
    def _raise_for_status(resp: requests.Response) -> None:
        status = resp.status_code
        if status in (401, 403, 404, 410):
            hint = _STATUS_HINTS.get(status, "")
            raise _NoRetryError(f"HTTP {status}（{hint}）" if hint else f"HTTP {status}")
        if status == 429:
            raise requests.HTTPError("HTTP 429（请求过于频繁）", response=resp)
        resp.raise_for_status()

    @staticmethod
    def _describe_error(exc: BaseException) -> str:
        if isinstance(exc, requests.exceptions.Timeout):
            return "连接超时"
        if isinstance(exc, requests.exceptions.ChunkedEncodingError):
            return "传输中断"
        if isinstance(exc, requests.exceptions.ConnectionError):
            return "连接失败"
        if isinstance(exc, requests.exceptions.HTTPError):
            if exc.response is not None:
                return f"HTTP {exc.response.status_code}"
            return "HTTP 错误"
        if isinstance(exc, requests.exceptions.RequestException):
            return f"请求错误（{exc.__class__.__name__}）"
        return f"未知错误（{exc.__class__.__name__}）"

    @staticmethod
    def _cleanup(path: str) -> None:
        try:
            if os.path.exists(path):
                os.remove(path)
        except OSError:
            pass

    # ---------- 暂停询问 ----------

    def _prompt_pause(self, live, failures: dict[int, SegmentTask]) -> str:
        live.stop()
        self.console.print()
        details = "\n".join(
            f"  [red]×[/] {task.filename} - {escape(task.error)}"
            for _, task in sorted(failures.items())[:10]
        )
        more = f"\n  … 另有 {len(failures) - 10} 个失败片段" if len(failures) > 10 else ""
        self.console.print(
            Panel(
                f"[yellow]已有 {len(failures)} 个片段下载失败，任务已暂停。[/]\n\n"
                f"{details}{more}\n\n"
                "[bold]可能原因[/]：服务器触发了下载限制、网络不稳定或片段已失效。\n\n"
                "[bold]请选择[/]\n"
                "  [cyan]1[/] 继续下载（跳过失败片段）\n"
                "  [cyan]2[/] 重新尝试所有失败片段\n"
                "  [cyan]3[/] 放弃本次任务",
                title="[bold red]下载遇到问题[/]",
                border_style="red",
            )
        )
        choice = Prompt.ask("您的选择", choices=["1", "2", "3"], default="1", console=self.console)
        live.start(refresh=True)
        return choice
