"""使用 DrissionPage 打开浏览器，并监听所有标签页中的 m3u8 请求。"""

from __future__ import annotations

import queue
import threading
import time
from dataclasses import dataclass, field

from DrissionPage import ChromiumOptions, ChromiumPage

from config import Config


@dataclass
class Detection:
    """一次 m3u8 捕获，附带产生它的标签页上下文。"""

    url: str
    body: str | None
    tab_id: str
    page_url: str
    title: str
    cookies: dict[str, str] = field(default_factory=dict)
    user_agent: str = ""


class BrowserMonitor:
    """启动独立的 Chromium 实例，为每个标签页起后台线程收集 m3u8 请求。"""

    def __init__(self, config: Config) -> None:
        self.config = config
        self.page: ChromiumPage | None = None
        self.browser = None
        self.queue: "queue.Queue[Detection | None]" = queue.Queue()
        self.closed = threading.Event()  # 浏览器被用户关闭 / 连接断开
        self._seen: set[str] = set()
        self._seen_lock = threading.Lock()
        self._stop = threading.Event()
        self._tabs: dict[str, object] = {}
        self._listeners: dict[str, threading.Thread] = {}
        self._tabs_lock = threading.Lock()
        self._watch_thread: threading.Thread | None = None

    # ---------- 生命周期 ----------

    def start(self) -> None:
        options = ChromiumOptions()
        # 使用独立实例与随机端口，避免与日常使用的 Chrome 冲突。
        options.auto_port()
        self.page = ChromiumPage(options)
        self.browser = self.page.browser
        self._watch_thread = threading.Thread(target=self._watch_tabs, name="tab-watcher", daemon=True)
        self._watch_thread.start()

    def stop(self, close_browser: bool = False) -> None:
        self._stop.set()
        with self._tabs_lock:
            tabs = list(self._tabs.values())
        for tab in tabs:
            try:
                tab.listen.stop()
            except Exception:
                pass
        if self.page is not None and close_browser:
            try:
                self.page.quit()
            except Exception:
                pass

    # ---------- 标签页发现 ----------

    def _watch_tabs(self) -> None:
        assert self.browser is not None
        empty_since: float | None = None
        while not self._stop.is_set():
            try:
                if not self.browser.states.is_alive:
                    self._on_browser_closed()
                    return
                tabs = self.browser.get_tabs()
            except Exception:
                if not self._stop.is_set():
                    self._on_browser_closed()
                return

            # 有些环境下关掉全部标签页后浏览器进程仍在后台，也要退出。
            if tabs:
                empty_since = None
            else:
                if empty_since is None:
                    empty_since = time.monotonic()
                elif time.monotonic() - empty_since > 5:
                    self._on_browser_closed()
                    return

            current_ids: set[str] = set()
            for tab in tabs:
                tab_id = str(getattr(tab, "tab_id", "") or "")
                if not tab_id:
                    continue
                current_ids.add(tab_id)
                with self._tabs_lock:
                    self._tabs[tab_id] = tab
                    started = tab_id in self._listeners
                if started:
                    continue
                thread = threading.Thread(
                    target=self._listen_tab,
                    args=(tab_id, tab),
                    name=f"m3u8-{tab_id[:6]}",
                    daemon=True,
                )
                with self._tabs_lock:
                    self._listeners[tab_id] = thread
                thread.start()

            with self._tabs_lock:
                for tab_id, thread in list(self._listeners.items()):
                    if tab_id not in current_ids and not thread.is_alive():
                        self._listeners.pop(tab_id, None)
                        self._tabs.pop(tab_id, None)
            self._stop.wait(0.3)

    def _on_browser_closed(self) -> None:
        """通知主线程退出：置位 closed，并往队列里放哨兵唤醒阻塞的 get()。"""
        if self.closed.is_set():
            return
        self.closed.set()
        self._stop.set()
        self.queue.put(None)

    # ---------- 单标签页监听 ----------

    def _listen_tab(self, tab_id: str, tab) -> None:
        try:
            tab.listen.start(targets="m3u8")
        except Exception:
            return
        while not self._stop.is_set():
            try:
                packet = tab.listen.wait(timeout=0.5)
            except Exception:
                break
            if not packet:
                continue
            url = (getattr(packet, "url", "") or "").strip()
            if "m3u8" not in url.lower():
                continue
            with self._seen_lock:
                if url in self._seen:
                    continue
                self._seen.add(url)
            detection = Detection(
                url=url,
                body=self._extract_body(packet),
                tab_id=tab_id,
                page_url=self._safe_str(tab, "url"),
                title=self._safe_str(tab, "title"),
                cookies=self._cookies(tab),
                user_agent=self._safe_str(tab, "user_agent"),
            )
            self.queue.put(detection)

    def refresh_detection(self, detection: Detection) -> Detection:
        """处理前刷新标签页快照：捕获瞬间页面可能还没加载完标题/Cookie。"""
        with self._tabs_lock:
            tab = self._tabs.get(detection.tab_id)
        if tab is None:
            return detection
        title = self._safe_str(tab, "title")
        if title:
            detection.title = title
        page_url = self._safe_str(tab, "url")
        if page_url:
            detection.page_url = page_url
        cookies = self._cookies(tab)
        if cookies:
            detection.cookies = cookies
        if not detection.user_agent:
            detection.user_agent = self._safe_str(tab, "user_agent")
        return detection

    # ---------- 工具 ----------

    @staticmethod
    def _safe_str(tab, name: str) -> str:
        try:
            value = getattr(tab, name, "")
            return str(value or "").strip()
        except Exception:
            return ""

    @staticmethod
    def _extract_body(packet) -> str | None:
        """尽量直接复用浏览器已拿到的响应体，失败时调用方会重新请求。

        DrissionPage 的 raw_body 在响应被 CDP 标记为 base64 时就是 base64 字符串，
        必须走 Response.body 让它自行判断并解码。
        """
        try:
            response = getattr(packet, "response", None)
            if response is None:
                return None
            body = getattr(response, "body", None)
            if isinstance(body, (bytes, bytearray)):
                return body.decode("utf-8", errors="ignore")
            if isinstance(body, str) and body:
                return body
            raw = getattr(response, "raw_body", None)
            if isinstance(raw, (bytes, bytearray)):
                return raw.decode("utf-8", errors="ignore")
            if isinstance(raw, str) and raw:
                return raw
        except Exception:
            return None
        return None

    def _cookies(self, tab) -> dict[str, str]:
        try:
            items = tab.cookies(all_domains=True)
        except Exception:
            try:
                items = tab.cookies()
            except Exception:
                return {}
        result: dict[str, str] = {}
        for cookie in items:
            if isinstance(cookie, dict):
                name = cookie.get("name")
                value = cookie.get("value", "")
            else:
                name = getattr(cookie, "name", None)
                value = getattr(cookie, "value", "")
            if name:
                result[str(name)] = str(value or "")
        return result
