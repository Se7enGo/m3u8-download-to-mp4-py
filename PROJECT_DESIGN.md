# M3U8 视频下载工具 - 项目设计文档

## 项目概述

一个基于 Python 的 M3U8 视频下载工具，使用 DrissionPage 控制浏览器，自动检测视频流链接并下载合并为 MP4 格式。

## 功能特性

- ✅ 使用 DrissionPage 控制 Chrome 浏览器
- ✅ 自动监听并检测 m3u8 链接
- ✅ 解析 m3u8 文件获取 ts 片段列表
- ✅ 多线程并发下载（最大4个并发）
- ✅ 实时显示下载进度
- ✅ 智能错误处理和重试机制
- ✅ 自动合并 ts 片段为 mp4
- ✅ 支持多清晰度选择（自动选最低清晰度）

## 技术栈

```txt
核心依赖（见 requirements.txt）：
- DrissionPage >= 4.1.0   # 启动浏览器、多标签页网络监听、页面标题/Cookie
- requests >= 2.31.0      # 请求 m3u8 与 ts 片段
- m3u8 >= 3.5.0           # m3u8 文件解析
- rich >= 13.7.0          # 终端美化输出、进度面板

外部依赖：
- FFmpeg（命令行，需在 PATH 中）—— 通过 subprocess 调用，不依赖 ffmpeg-python
```

## 已确认的实现决策

| 决策点 | 结论 |
|--------|------|
| 浏览器配置 | 使用 DrissionPage 管理的独立 Chromium 实例（`auto_port()`），不影响日常 Chrome |
| 监听范围 | 监听浏览器内**所有标签页**，捕获时记录该标签页的标题、URL、Cookie、UA |
| 清晰度 | 主播放列表自动选择最低清晰度；同一批出现多个不同 m3u8 时由用户选择 |
| 加密视频 | 检测到 AES-128 / SAMPLE-AES 直接提示不支持，不下载 |
| 下载并发 | 4 个并发，所有请求共享 0.15 秒全局间隔（可配置） |
| 进度展示 | 实时显示 已完成/总数、百分比、成功/失败/重试、速度、当前下载片段 |
| 失败处理 | 单片段重试 3 次；失败跳过并记录原因；累计 3 个失败暂停，让用户选择继续/重试/放弃 |
| 视频名称 | 默认取该标签页的页面标题，可手动修改；非法字符自动清理 |
| 合并方式 | ffmpeg `-c copy` 无损合并（不重新编码），输出 mp4 |
| 合并前确认 | 存在失败片段时，合并前再次确认 |
| 完成后行为 | 回到监听状态，继续下载下一个视频 |
| 退出方式 | 关闭浏览器（0.3 秒轮询连接状态，通常 1 秒内退出）或按 `Ctrl+C`；下载中关闭浏览器会立即中止并保留已下载片段 |

## 系统架构

### 模块划分

```
video_downloader/
├── main.py                   # 程序入口
├── browser_monitor.py        # 浏览器监控模块
├── m3u8_handler.py           # M3U8 解析和处理
├── downloader.py             # 下载管理器
├── video_merger.py           # 视频合并模块
├── config.py                 # 配置管理
├── utils.py                  # 工具函数
├── requirements.txt          # 依赖列表
├── temp/                     # 临时下载目录（自动创建）
│   └── [视频名称]/
│       ├── *.ts
│       └── download_log.txt
└── output/                   # 输出目录（自动创建）
    └── [视频名称].mp4
```

## 核心流程

### 1. 启动流程

```
[启动程序]
    ↓
[初始化 DrissionPage]
    ↓
[打开 Chrome 浏览器]
    ↓
[启动网络监听线程]
    ↓
[等待用户操作]
```

### 2. M3U8 检测流程

```
[监听所有网络请求]
    ↓
[过滤 .m3u8 后缀的 URL]
    ↓
[解析 m3u8 文件]
    ↓
[判断是否为 Master Playlist]
    ├─ 是 → [提取所有清晰度]
    │         ↓
    │      [自动选择最低清晰度]
    │         ↓
    │      [如果检测到多个，让用户选择]
    │
    └─ 否 → [直接解析 Media Playlist]
    ↓
[显示 ts 片段信息]
    ↓
[等待用户确认]
```

### 3. 下载流程

```
[用户确认下载]
    ↓
[创建临时目录: temp/[视频名称]/]
    ↓
[启动下载管理器]
    ├─ 并发数: 4
    ├─ 重试次数: 3
    └─ 请求间隔: 0.1-0.2秒
    ↓
[顺序并发下载 ts 片段]
    ├─ 成功 → [保存到本地]
    ├─ 失败 → [记录错误，跳过]
    └─ 错误数 >= 3 → [暂停任务，询问用户]
    ↓
[所有片段下载完成]
    ↓
[显示下载报告]
```

### 4. 合并流程

```
[检查下载完整性]
    ↓
[调用 FFmpeg 合并]
    ↓
[输出到: output/[视频名称].mp4]
    ↓
[询问是否删除临时文件]
    ↓
[完成]
```

## 详细设计

### 1. 浏览器监控模块 (browser_monitor.py)

```python
class BrowserMonitor:
    """
    使用 DrissionPage 监控浏览器网络请求
    """
    
    def __init__(self):
        self.page = None
        self.m3u8_urls = []
        self.listening = False
    
    def start_browser(self):
        """启动 Chrome 浏览器"""
        # 使用 DrissionPage 启动浏览器
        # 开启网络监听
        pass
    
    def listen_network(self):
        """监听网络请求，捕获 m3u8 链接"""
        # 过滤规则：
        # - URL 包含 .m3u8
        # - Content-Type 包含 application/vnd.apple.mpegurl
        pass
    
    def get_page_title(self) -> str:
        """获取当前页面标题"""
        pass
    
    def get_current_url(self) -> str:
        """获取当前页面 URL（用于 Referer）"""
        pass
```

### 2. M3U8 处理模块 (m3u8_handler.py)

```python
class M3U8Info:
    """M3U8 信息数据类"""
    url: str                    # m3u8 URL
    segments: List[str]         # ts 片段 URL 列表
    total_duration: float       # 总时长（秒）
    is_encrypted: bool          # 是否加密
    key_url: str               # 密钥 URL（如果加密）
    base_url: str              # 基础 URL
    
class M3U8Handler:
    """
    解析和处理 m3u8 文件
    """
    
    def parse_m3u8(self, url: str) -> M3U8Info:
        """解析 m3u8 文件"""
        # 1. 下载 m3u8 文件内容
        # 2. 使用 m3u8 库解析
        # 3. 检查是否为 Master Playlist
        # 4. 检查是否加密
        # 5. 提取 ts 片段列表
        # 6. 转换相对路径为绝对路径
        pass
    
    def is_master_playlist(self, content: str) -> bool:
        """判断是否为 Master Playlist"""
        # 检查是否包含 #EXT-X-STREAM-INF
        pass
    
    def get_all_qualities(self, url: str) -> List[tuple]:
        """
        从 Master Playlist 提取所有清晰度
        返回: [(bandwidth, resolution, url), ...]
        """
        pass
    
    def select_lowest_quality(self, qualities: List[tuple]) -> str:
        """选择最低清晰度的 m3u8 URL"""
        # 按 bandwidth 排序，取最小值
        pass
    
    def check_encryption(self, m3u8_obj) -> tuple:
        """
        检查是否加密
        返回: (is_encrypted: bool, key_url: str)
        """
        pass
```

### 3. 下载管理器 (downloader.py)

```python
class DownloadResult:
    """下载结果数据类"""
    total: int                  # 总片段数
    success: int                # 成功数
    failed: int                 # 失败数
    skipped: int                # 跳过数
    error_logs: List[dict]      # 错误日志
    
class Downloader:
    """
    多线程下载管理器
    """
    
    def __init__(self, max_workers=4, retry_times=3):
        self.max_workers = max_workers
        self.retry_times = retry_times
        self.error_count = 0
        self.error_threshold = 3
        self.paused = False
        
    def download_all(
        self,
        segments: List[str],
        save_dir: str,
        headers: dict,
        progress_callback
    ) -> DownloadResult:
        """
        下载所有 ts 片段
        
        Args:
            segments: ts 片段 URL 列表
            save_dir: 保存目录
            headers: HTTP 请求头（包含 Referer, User-Agent）
            progress_callback: 进度回调函数
        """
        # 使用 ThreadPoolExecutor
        # 顺序提交任务，最多4个并发
        pass
    
    def download_segment(
        self,
        url: str,
        save_path: str,
        headers: dict,
        index: int
    ) -> dict:
        """
        下载单个 ts 片段（带重试）
        
        返回: {
            'index': int,
            'success': bool,
            'error': str,
            'retry_count': int
        }
        """
        pass
    
    def handle_error(self, error_info: dict) -> bool:
        """
        处理下载错误
        
        返回: True=继续, False=暂停
        """
        self.error_count += 1
        if self.error_count >= self.error_threshold:
            # 暂停任务，询问用户
            return False
        return True
```

### 4. 视频合并模块 (video_merger.py)

```python
class VideoMerger:
    """
    使用 FFmpeg 合并 ts 片段
    """
    
    def merge_ts_to_mp4(
        self,
        ts_dir: str,
        output_path: str,
        total_count: int
    ) -> bool:
        """
        合并 ts 片段为 mp4
        
        方法:
        1. 生成 filelist.txt
        2. 使用 FFmpeg concat 协议合并
        
        命令: ffmpeg -f concat -safe 0 -i filelist.txt -c copy output.mp4
        """
        pass
    
    def check_ffmpeg(self) -> bool:
        """检查 FFmpeg 是否已安装"""
        pass
```

### 5. 配置管理 (config.py)

```python
class Config:
    """全局配置"""
    
    # 下载配置
    MAX_WORKERS = 4                # 最大并发数
    RETRY_TIMES = 3                # 重试次数
    REQUEST_INTERVAL = 0.15        # 请求间隔（秒）
    TIMEOUT = 30                   # 超时时间（秒）
    ERROR_THRESHOLD = 3            # 错误阈值
    
    # 路径配置
    TEMP_DIR = "./temp"
    OUTPUT_DIR = "./output"
    
    # 请求头配置
    USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
    
    # 视频名称配置
    MAX_FILENAME_LENGTH = 100      # 最大文件名长度
    INVALID_CHARS = r'[\\/:*?"<>|]'  # 非法字符
```

## UI 交互设计

### 1. 启动界面

```
╔══════════════════════════════════════════════════════════╗
║          M3U8 视频下载工具 v1.0                          ║
╚══════════════════════════════════════════════════════════╝

[INFO] 正在启动 Chrome 浏览器...
[INFO] 浏览器已启动，等待用户操作
[INFO] 网络监听已开启，正在检测 m3u8 链接...

提示：
1. 在浏览器中访问视频网站
2. 播放视频或刷新页面
3. 程序会自动检测 m3u8 链接

关闭浏览器窗口或按 Ctrl+C 退出程序
```

### 2. 检测到 M3U8

```
╔══════════════════════════════════════════════════════════╗
║          检测到 M3U8 链接                                ║
╚══════════════════════════════════════════════════════════╝

[1] https://example.com/video1/index.m3u8
    片段数: 431 个
    预估时长: 21分35秒
    清晰度: 720p (最低)

[2] https://example.com/video2/index.m3u8
    片段数: 256 个
    预估时长: 12分48秒
    清晰度: 480p (最低)

请选择要下载的视频 [1-2, 0=取消]: _
```

### 3. ts 片段列表展示

```
╔══════════════════════════════════════════════════════════╗
║          视频片段信息                                    ║
╚══════════════════════════════════════════════════════════╝

视频名称: Python入门教程-第1集
M3U8 URL: https://example.com/video/index.m3u8
总片段数: 431 个
预估时长: 21分35秒
加密状态: 未加密 ✓

前 5 个片段:
  [001] seg-0.ts
  [002] seg-1.ts
  [003] seg-2.ts
  [004] seg-3.ts
  [005] seg-4.ts
  ...
后 5 个片段:
  [427] seg-426.ts
  [428] seg-427.ts
  [429] seg-428.ts
  [430] seg-429.ts
  [431] seg-430.ts

是否开始下载? [Y/n]: _
```

### 4. 下载进度显示

```
╔══════════════════════════════════════════════════════════╗
║          下载中: Python入门教程-第1集                    ║
╚══════════════════════════════════════════════════════════╝

总进度: ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━ 245/431 (56.8%)

[✓] 成功: 243 个
[✗] 失败: 2 个
[↻] 重试: 5 次
[⏱] 用时: 02:15
[⚡] 速度: 2.3 MB/s

当前下载:
  [1/4] seg-245.ts ━━━━━━━━━━━━━━━━ 100% (1.2 MB)
  [2/4] seg-246.ts ━━━━━━━━━━━━━━━━ 78% (0.9 MB)
  [3/4] seg-247.ts ━━━━━━━━━━━━━━━━ 45% (0.5 MB)
  [4/4] seg-248.ts ━━━━━━━━━━━━━━━━ 12% (0.1 MB)

失败记录:
  [×] seg-023.ts - 连接超时 (已重试 3 次)
  [×] seg-089.ts - HTTP 404 (已重试 3 次)
```

### 5. 错误暂停提示

```
╔══════════════════════════════════════════════════════════╗
║          下载遇到问题                                    ║
╚══════════════════════════════════════════════════════════╝

[警告] 检测到 3 个片段下载失败，任务已暂停

失败详情:
  [×] seg-023.ts - 连接超时
  [×] seg-089.ts - HTTP 404
  [×] seg-156.ts - 连接被拒绝

可能原因:
  • 服务器触发了下载限制
  • 网络连接不稳定
  • 部分片段已失效

请选择操作:
  [1] 继续下载（跳过失败的片段）
  [2] 重新尝试失败的片段
  [3] 放弃下载
  
您的选择 [1-3]: _
```

### 6. 合并进度

```
╔══════════════════════════════════════════════════════════╗
║          合并视频                                        ║
╚══════════════════════════════════════════════════════════╝

[INFO] 正在使用 FFmpeg 合并 428 个片段...
[INFO] 输出文件: output/Python入门教程-第1集.mp4

合并进度: ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━ 68%

处理中...
```

### 7. 完成界面

```
╔══════════════════════════════════════════════════════════╗
║          下载完成                                        ║
╚══════════════════════════════════════════════════════════╝

✓ 视频下载并合并成功！

视频信息:
  名称: Python入门教程-第1集
  片段: 428/431 (99.3%)
  时长: 21分35秒
  大小: 856.4 MB
  位置: output/Python入门教程-第1集.mp4

下载统计:
  ✓ 成功: 428 个
  × 失败: 3 个（已跳过）
  ↻ 总重试: 12 次
  ⏱ 总用时: 08分42秒

是否删除临时文件? [Y/n]: _

继续检测新的 m3u8 链接中...
关闭浏览器窗口或按 Ctrl+C 退出程序
```

## 错误处理策略

### 1. 加密检测

```python
if m3u8_info.is_encrypted:
    print("[错误] 检测到加密的 m3u8 文件")
    print("当前版本不支持加密视频下载")
    print("加密方式: AES-128")
    print(f"密钥 URL: {m3u8_info.key_url}")
    return
```

### 2. 下载错误分类

| 错误类型 | 处理方式 |
|---------|---------|
| 连接超时 / 传输中断 / 网络错误 | 重试（默认 3 次，指数退避），失败后记录原因并跳过 |
| HTTP 401 / 403 / 404 / 410 | 不重试，记录原因并跳过 |
| HTTP 429 | 按普通可重试错误处理（带退避），并计入失败阈值 |
| HTTP 5xx | 重试，失败后跳过 |
| 失败累计达到阈值（默认 3 个） | 暂停任务，询问用户：继续（跳过）/ 重试所有失败片段 / 放弃 |

### 3. FFmpeg 错误

```python
# 检查 FFmpeg 是否安装
if not check_ffmpeg():
    print("[错误] 未检测到 FFmpeg")
    print("请安装 FFmpeg: https://ffmpeg.org/download.html")
    print("或使用命令: choco install ffmpeg (Windows)")
    return False
```

## 文件命名规范

### 视频名称提取

```python
def sanitize_filename(title: str) -> str:
    """
    清理文件名
    
    规则:
    1. 移除非法字符: \ / : * ? " < > |
    2. 替换为下划线或删除
    3. 限制长度为 100 字符
    4. 去除首尾空格
    """
    # 示例转换:
    # "【教程】Python入门 - 第1集_哔哩哔哩" 
    # → "Python入门-第1集"
    pass
```

### 临时文件命名

```
temp/
└── Python入门-第1集_20240315_143022/
    ├── seg-000.ts
    ├── seg-001.ts
    ├── ...
    ├── seg-430.ts
    ├── filelist.txt          # FFmpeg 输入文件列表
    └── download_log.txt      # 下载日志
```

## 依赖安装

### requirements.txt

```txt
DrissionPage>=4.1.0
requests>=2.31.0
m3u8>=3.5.0
rich>=13.7.0
```

### 安装命令

```bash
pip install -r requirements.txt
```

### FFmpeg 安装

**Windows:**
```bash
choco install ffmpeg
# 或下载: https://ffmpeg.org/download.html
```

**Linux:**
```bash
sudo apt install ffmpeg
```

**macOS:**
```bash
brew install ffmpeg
```

## 使用说明

### 1. 启动程序

```bash
python main.py
```

### 2. 操作流程

1. 程序自动打开 Chrome 浏览器
2. 在浏览器中访问视频网站（如 B站、YouTube 等）
3. 播放视频或刷新页面
4. 程序检测到 m3u8 后会提示
5. 选择要下载的视频
6. 确认下载
7. 等待下载和合并完成

### 3. 注意事项

- 首次使用需要安装 FFmpeg
- 下载速度取决于网络和服务器限制
- 部分网站可能需要登录后才能获取视频链接
- 加密的视频暂不支持

## 后续优化方向

- [ ] 支持 AES-128 加密视频
- [ ] 支持断点续传
- [ ] 添加 GUI 界面
- [ ] 支持批量下载
- [ ] 支持自定义下载配置
- [ ] 添加下载历史记录
- [ ] 支持更多视频格式
- [ ] 添加代理支持

## 版本记录

### v1.0.0 (2024-03-15)
- ✅ 基础功能实现
- ✅ M3U8 检测和解析
- ✅ 多线程下载
- ✅ FFmpeg 合并
- ✅ 错误处理
- ✅ 进度显示

---

**项目状态:** 已开发完成并通过本地端到端验证 

**已完成的验证：**

1. m3u8 解析：主列表→最低清晰度展开、加密检测、直播检测、fMP4 初始化片段
2. 浏览器监听：真实 Chrome 中捕获 m3u8（修正了 DrissionPage 对 base64 响应体的处理）
3. 多标签页：同时捕获两个标签页，并正确使用各自页面的标题、Referer 和 Cookie
4. 下载：并发限速、进度面板、失败重试、达到阈值暂停（继续/重试/放弃三条路径）
5. 合并：ffmpeg `-c copy` 输出可播放 mp4，合并进度显示
6. 命名：页面标题作为文件名，异常字符（私用区字符）安全过滤

完成，可以开始下一步了。