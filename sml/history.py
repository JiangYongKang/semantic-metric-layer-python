"""历史版本档案：保存已发布的不可变模型快照，并按保留上限稳定淘汰。

版本号语义：

- 版本号从 1 开始、连续、单调递增（每次成功变更 +1）；
- 当前版本始终保留；``retention`` 表示除当前版本外最多保留多少个历史版本，
  ``0`` 表示不保留任何历史版本，``None`` 表示不限制；
- 超过上限时淘汰**最旧**的版本（稳定、可预测的 FIFO 规则）。

回查时版本号的五种判定（严格区分，禁止用当前版本回退冒充）：

- == 当前版本：直接读当前；
- <= 当前版本且 >= 最旧可用版本号：在档案中命中；
- 曾经归档、后因上限被淘汰：``version_evicted``；
- 从未进入档案（如 retention=0 时发布的版本）/早于最早归档点：
  ``version_out_of_range``；
- > 当前版本或非正数（从未存在）：``version_not_found``。
"""

from __future__ import annotations

from .errors import RetentionConfigError
from .model import SemanticModel


def validate_retention(retention: object) -> int | None:
    """校验保留上限：None 表示不限；非负整数表示最多保留的历史版本数。"""
    if retention is None:
        return None
    if isinstance(retention, bool) or not isinstance(retention, int) or retention < 0:
        raise RetentionConfigError(
            f"历史版本保留上限必须是非负整数或 None，收到 {retention!r}",
            details={"retention": retention},
        )
    return retention


class VersionArchive:
    """历史模型快照档案：追加发布、FIFO 淘汰、按版本号回查。"""

    def __init__(self, retention: int | None = None) -> None:
        self._retention = validate_retention(retention)
        self._snapshots: dict[int, SemanticModel] = {}
        # 曾经成功归档过的最小版本号；None 表示从未有版本进入过档案。
        # 用于区分「从未进入保留范围」与「进入后被淘汰」。
        self._ever_archived_min: int | None = None

    def publish(self, model: SemanticModel) -> list[int]:
        """当前版本被新版本取代时归档；返回被淘汰的版本号列表。

        模型不可变且版本号单调，因此不允许重复归档同一版本。
        """
        if model.version in self._snapshots:
            raise RetentionConfigError(
                f"版本 {model.version} 已归档，禁止重复发布",
                details={"version": model.version},
            )
        self._snapshots[model.version] = model
        evicted = self._evict_to_limit()
        # 只把「淘汰后真正留存过」的版本计入最早归档点：
        # retention=0 时版本即归档即淘汰、从未可回查，不应算「曾保留后被淘汰」。
        if model.version in self._snapshots and self._ever_archived_min is None:
            self._ever_archived_min = model.version
        return evicted

    def get(self, version: int) -> SemanticModel | None:
        """取已归档的历史快照；不存在返回 None（是否可回查由上层判定）。"""
        return self._snapshots.get(version)

    def oldest_version(self) -> int | None:
        """档案中最旧的可回查历史版本号；档案为空返回 None。"""
        return min(self._snapshots) if self._snapshots else None

    def retained_versions(self) -> list[int]:
        """档案中现存历史版本号（升序）。"""
        return sorted(self._snapshots)

    def ever_archived_min(self) -> int | None:
        """曾经归档过的最小版本号；从未归档返回 None。"""
        return self._ever_archived_min

    def set_retention(self, retention: int | None) -> list[int]:
        """调整保留上限并立即应用淘汰；返回被淘汰的版本号列表。"""
        self._retention = validate_retention(retention)
        return self._evict_to_limit()

    @property
    def retention(self) -> int | None:
        return self._retention

    # ------------------------------------------------------------------
    def _evict_to_limit(self) -> list[int]:
        """超过上限时从最旧版本开始淘汰（FIFO），返回被淘汰版本号列表。"""
        evicted: list[int] = []
        if self._retention is None:
            return evicted
        while len(self._snapshots) > self._retention:
            oldest = min(self._snapshots)
            del self._snapshots[oldest]
            evicted.append(oldest)
        return evicted
