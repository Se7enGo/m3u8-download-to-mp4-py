"""全局配置。"""

from __future__ import annotations

import argparse
import os
from dataclasses import dataclass


@dataclass
class Config:
    # 下载
    max_workers: int = 4          # 最大并发数
    retry_times: int = 3          # 单个片段最多尝试次数
    request_interval: float = 0.15  # 所有下载请求之间的全局间隔（秒），避免触发服务器限速
    timeout: float = 30.0         # 单次请求超时（秒）
    error_threshold: int = 3      # 失败片段达到该数量时暂停并询问用户

    # 目录
    temp_dir: str = "temp"
    output_dir: str = "output"

    # 文件名
    max_filename_length: int = 100

    def ensure_dirs(self) -> None:
        os.makedirs(self.temp_dir, exist_ok=True)
        os.makedirs(self.output_dir, exist_ok=True)

    @classmethod
    def from_args(cls, argv: list[str] | None = None) -> "Config":
        parser = argparse.ArgumentParser(description="M3U8 视频下载工具")
        parser.add_argument("--workers", type=int, default=4, help="最大并发数（默认 4）")
        parser.add_argument("--retry", type=int, default=3, help="单个片段最多尝试次数（默认 3）")
        parser.add_argument("--interval", type=float, default=0.15, help="下载请求之间的全局间隔秒数（默认 0.15）")
        parser.add_argument("--timeout", type=float, default=30.0, help="单次请求超时秒数（默认 30）")
        parser.add_argument("--error-threshold", type=int, default=3, help="失败片段达到该数量时暂停询问（默认 3）")
        parser.add_argument("--temp-dir", default="temp", help="临时下载目录（默认 temp）")
        parser.add_argument("--output-dir", default="output", help="输出目录（默认 output）")
        args = parser.parse_args(argv)
        return cls(
            max_workers=max(1, args.workers),
            retry_times=max(1, args.retry),
            request_interval=max(0.0, args.interval),
            timeout=max(1.0, args.timeout),
            error_threshold=max(1, args.error_threshold),
            temp_dir=args.temp_dir,
            output_dir=args.output_dir,
        )
