"""查询资源上限与超限处置。

- 预算：输出行数、中间行数、分组数（高基数）、墙钟时间、内存字节
- 策略：``reject`` 直接拒绝（查询视为从未发生，不留部分结果）；
  ``truncate`` 允许截断但结果显式标记 ``truncated=True``
- 分组数、中间行数、输出行数在真正物化前预检，内存按近似字节数统计
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass
from enum import Enum

from .errors import ResourceLimitError


class LimitAction(str, Enum):
    REJECT = "reject"
    TRUNCATE = "truncate"


@dataclass(frozen=True)
class ResourceBudget:
    """资源预算；0 表示该项不限制。"""

    max_output_rows: int = 10_000
    max_intermediate_rows: int = 1_000_000
    max_groups: int = 100_000
    timeout_seconds: float = 5.0
    max_bytes: int = 256 * 1024 * 1024
    on_overflow: LimitAction = LimitAction.REJECT


class BudgetGuard:
    """查询执行期的预算守卫。

    任何一次超限调用都抛出 :class:`ResourceLimitError`；
    truncate 策略下，``request_rows`` 会返回允许的剩余行数而不是抛错，
    由调用方据此截断并记录 truncated 标记。
    """

    def __init__(self, budget: ResourceBudget) -> None:
        self.budget = budget
        self._start = time.monotonic()
        self._bytes = 0
        self._intermediate = 0
        self._groups = 0
        self._output = 0
        self.truncated = False
        self._lock = threading.Lock()
        # 零时间预算即「不允许任何耗时」，构造守卫时立即判定
        if budget.timeout_seconds is not None and budget.timeout_seconds <= 0:
            raise ResourceLimitError(
                f"查询时间预算 {budget.timeout_seconds}s 不允许执行",
                limit_kind="timeout_seconds",
                budget=budget.timeout_seconds,
                observed=0.0,
                rejected=True,
            )

    def elapsed(self) -> float:
        return time.monotonic() - self._start

    def check_time(self) -> None:
        b = self.budget.timeout_seconds
        if b and self.elapsed() > b:
            raise ResourceLimitError(
                f"查询耗时 {self.elapsed():.4f}s 超过预算 {b}s，已拒绝",
                limit_kind="timeout_seconds",
                budget=b,
                observed=self.elapsed(),
                rejected=True,
            )

    def add_intermediate_rows(self, n: int) -> None:
        with self._lock:
            self._intermediate += n
        b = self.budget.max_intermediate_rows
        if b and self._intermediate > b:
            raise ResourceLimitError(
                f"中间结果行数 {self._intermediate} 超过预算 {b}",
                limit_kind="max_intermediate_rows",
                budget=b,
                observed=self._intermediate,
                rejected=True,
            )

    def announce_group_count(self, n: int) -> None:
        """分组物化前按基数预检（高基数拒绝）。"""
        b = self.budget.max_groups
        if b and n > b:
            raise ResourceLimitError(
                f"分组基数 {n} 超过预算 {b}（高基数场景拒绝）",
                limit_kind="max_groups",
                budget=b,
                observed=n,
                rejected=True,
            )

    def register_groups(self, n: int) -> None:
        with self._lock:
            self._groups = n
        self.announce_group_count(n)

    def allow_output_rows(self, n: int) -> int:
        """返回允许输出的行数；truncate 模式下可能小于 n。"""
        b = self.budget.max_output_rows
        if not b:
            with self._lock:
                self._output += n
            return n
        with self._lock:
            remaining = b - self._output
        if n <= remaining:
            with self._lock:
                self._output += n
            return n
        if self.budget.on_overflow is LimitAction.REJECT:
            raise ResourceLimitError(
                f"输出行数将达 {self._output + n}，超过预算 {b}，查询被拒绝",
                limit_kind="max_output_rows",
                budget=b,
                observed=self._output + n,
                rejected=True,
            )
        allowed = max(remaining, 0)
        with self._lock:
            self._output += allowed
        if allowed < n:
            self.truncated = True
        return allowed

    def add_bytes(self, n: int) -> None:
        with self._lock:
            self._bytes += n
        b = self.budget.max_bytes
        if b and self._bytes > b:
            raise ResourceLimitError(
                f"查询内存占用约 {self._bytes} 字节，超过预算 {b}",
                limit_kind="max_bytes",
                budget=b,
                observed=self._bytes,
                rejected=True,
            )

    def stats(self) -> dict:
        return {
            "elapsed_seconds": round(self.elapsed(), 6),
            "intermediate_rows": self._intermediate,
            "groups": self._groups,
            "output_rows": self._output,
            "approx_bytes": self._bytes,
            "truncated": self.truncated,
        }


def approx_row_bytes(width: int) -> int:
    """单行结果的近似字节占用（保守估计，随列数线性增长）。"""
    return 64 + 40 * max(width, 1)
