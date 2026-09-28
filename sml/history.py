"""历史版本保留与稳定淘汰。

每次成功发布后，**被取代的旧模型**作为不可变快照进入历史栈；历史栈只增不
改写，淘汰规则固定为 FIFO（版本最旧者先淘汰），保证同一系列变更在任何机器
上的淘汰结果一致、可解释。

容量边界：

- ``max_history`` 仅限制「历史版本」数量，当前最新版本永远保留；
- ``max_history=0`` 表示不保留任何历史版本（旧版本在下一版本发布时立即淘汰）；
- 调小容量会立即按 FIFO 淘汰多余的旧版本。

版本号自 1 起连续（每次成功发布 +1，失败不升版），因此对于
``1 <= v < 当前版本`` 且不在保留栈中的版本，可以明确判定为「已淘汰」，
绝不需要、也不允许用当前版本顶替。
"""

from __future__ import annotations


class VersionHistory:
    """不可变模型快照的 FIFO 历史栈（不含当前版本）。"""

    def __init__(self, max_history: int = 10) -> None:
        if not isinstance(max_history, int) or isinstance(max_history, bool):
            raise TypeError("max_history 必须是非负整数")
        if max_history < 0:
            raise ValueError("max_history 不能为负数")
        self._max_history = max_history
        # 按版本升序保留的历史快照；同一时刻一个版本至多一条
        self._past: list = []  # list[SemanticModel]

    # ------------------------------------------------------------------
    @property
    def limit(self) -> int:
        return self._max_history

    @property
    def retained_versions(self) -> tuple[int, ...]:
        """仍可回查的历史版本号（升序，不含当前版本）。"""
        return tuple(m.version for m in self._past)

    def oldest_version(self) -> int | None:
        """保留栈中最旧的版本号；栈空为 None。"""
        return self._past[0].version if self._past else None

    def __len__(self) -> int:
        return len(self._past)

    def set_limit(self, max_history: int) -> tuple[int, ...]:
        """调整保留容量并立即按 FIFO 淘汰；返回本次淘汰的版本号。"""
        if not isinstance(max_history, int) or isinstance(max_history, bool):
            raise TypeError("max_history 必须是非负整数")
        if max_history < 0:
            raise ValueError("max_history 不能为负数")
        self._max_history = max_history
        return self._evict()

    # ------------------------------------------------------------------
    def get(self, version: int):
        """取回历史快照；不在保留栈中返回 None（当前版本也不在其中）。"""
        for m in self._past:
            if m.version == version:
                return m
        return None

    def record(self, model) -> tuple[int, ...]:
        """把被取代的旧模型快照压入历史栈并执行容量淘汰。

        返回本次因容量限制被淘汰的版本号（升序）。历史栈只在成功发布后
        写入；版本号由注册中心保证单调递增，这里只防御性拒绝重复版本。
        """
        if any(m.version == model.version for m in self._past):
            raise AssertionError(f"版本 {model.version} 已在历史栈中")
        self._past.append(model)
        return self._evict()

    def _evict(self) -> tuple[int, ...]:
        """超出容量时从最旧端稳定淘汰；返回淘汰版本号。"""
        evicted: list[int] = []
        while len(self._past) > self._max_history:
            evicted.append(self._past.pop(0).version)
        return tuple(evicted)
