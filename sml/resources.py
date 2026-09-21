"""查询资源预算与超限处置。

策略：超限即拒绝（fail-fast），抛出 ResourceLimitError；
引擎在抛出前不向外暴露任何部分结果，失败查询不残留状态。
"""
from __future__ import annotations

import time
from dataclasses import dataclass

from .errors import ResourceLimitError


@dataclass(frozen=True)
class ResourceLimits:
    max_scan_rows: int = 100_000      # 扫描（含关联后）行数上限
    max_output_rows: int = 10_000     # 分组结果行数上限（高基数保护）
    max_elapsed_ms: float = 5_000.0   # 单次查询耗时上限


class ResourceGuard:
    """执行期预算计数器；每次计费都可能拒绝。"""

    def __init__(self, limits: ResourceLimits) -> None:
        self._limits = limits
        self._start = time.monotonic()
        self.scanned = 0

    def charge_rows(self, n: int, *, stage: str) -> None:
        self.scanned += n
        if self.scanned > self._limits.max_scan_rows:
            raise ResourceLimitError(
                f"扫描行数超限（阶段 {stage}）",
                limit_kind="scan_rows",
                budget=self._limits.max_scan_rows, observed=self.scanned)
        self.check_time(stage=stage)

    def check_output(self, n: int) -> None:
        if n > self._limits.max_output_rows:
            raise ResourceLimitError(
                "分组结果行数超限（高基数）",
                limit_kind="output_rows",
                budget=self._limits.max_output_rows, observed=n)

    def check_time(self, *, stage: str) -> None:
        elapsed = (time.monotonic() - self._start) * 1000.0
        if elapsed > self._limits.max_elapsed_ms:
            raise ResourceLimitError(
                f"查询耗时超限（阶段 {stage}）",
                limit_kind="elapsed_ms",
                budget=self._limits.max_elapsed_ms,
                observed=round(elapsed, 3))
