"""数据集间关联关系定义与校验。

严格防膨胀/防歧义规则：

- 只允许 many_to_one、one_to_one、one_to_many 三种基数；
  many_to_many 直接拒绝（关联键两侧都可能重复 -> 行数膨胀不可控）。
- many_to_one / one_to_one 要求「one 侧」键在数据中唯一且非空，
  违反即 ``non_unique_key``（不得靠运行时去重兜底）。
- 同两个数据集之间只允许一条关系（防止方向/字段歧义）。
- 路径解析：查询所跨数据集之间必须存在**唯一**连接路径；
  存在多条候选路径即 ``ambiguous_join``，绝不静默选一条。
- 沿路径只允许在「many 侧」连续前进（星型语义），一旦需要从 one 侧
  扇出到第二个 many 侧，判定为方向歧义并拒绝（隐性膨胀来源）。
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

from .errors import (
    AmbiguousJoinError,
    InvalidCaliberError,
    NonUniqueKeyError,
    RelationError,
)


class Cardinality(str, Enum):
    MANY_TO_ONE = "many_to_one"
    ONE_TO_MANY = "one_to_many"
    ONE_TO_ONE = "one_to_one"


@dataclass(frozen=True)
class Relation:
    """两个数据集之间的一条有向关联。

    基数总是以 ``left`` 相对 ``right`` 描述：
    many_to_one 表示 left 多行 -> right 一行。
    """

    name: str
    left_dataset: str
    left_keys: tuple[str, ...]
    right_dataset: str
    right_keys: tuple[str, ...]
    cardinality: Cardinality

    def other(self, dataset: str) -> str:
        if dataset == self.left_dataset:
            return self.right_dataset
        if dataset == self.right_dataset:
            return self.left_dataset
        raise RelationError(f"关系 '{self.name}' 不涉及数据集 '{dataset}'")

    def keys_of(self, dataset: str) -> tuple[str, ...]:
        if dataset == self.left_dataset:
            return self.left_keys
        return self.right_keys

    def many_side(self) -> str:
        """返回 many 侧数据集名；one_to_one 返回 None。"""
        if self.cardinality is Cardinality.MANY_TO_ONE:
            return self.left_dataset
        if self.cardinality is Cardinality.ONE_TO_MANY:
            return self.right_dataset
        return None  # type: ignore[return-value]

    def one_side(self) -> str | None:
        m = self.many_side()
        if m is None:
            return None
        return self.right_dataset if m == self.left_dataset else self.left_dataset


class RelationGraph:
    """关联图：登记关系、校验键唯一性、解析无歧义连接路径。"""

    def __init__(self, datasets: "object") -> None:  # DatasetRegistry
        self._datasets = datasets
        self._relations: dict[str, Relation] = {}

    def add(self, relation: Relation) -> None:
        r = relation
        if r.name in self._relations:
            raise RelationError(
                f"关联 '{r.name}' 已存在", details={"relation": r.name}
            )
        if r.left_dataset == r.right_dataset:
            raise RelationError(
                f"关联 '{r.name}' 不能自引用数据集 '{r.left_dataset}'",
                details={"relation": r.name},
            )
        for ds_name, keys in (
            (r.left_dataset, r.left_keys),
            (r.right_dataset, r.right_keys),
        ):
            ds = self._datasets.get(ds_name)
            cols = set(ds.schema.field_map)
            missing = [k for k in keys if k not in cols]
            if missing:
                raise InvalidCaliberError(
                    f"关联 '{r.name}' 的键 {missing} 在数据集 '{ds_name}' 中不存在",
                    details={"relation": r.name, "dataset": ds_name, "missing_keys": missing},
                )
            if len(keys) == 0 or len(set(keys)) != len(keys):
                raise RelationError(
                    f"关联 '{r.name}' 在 '{ds_name}' 上的键为空或有重复",
                    details={"relation": r.name, "dataset": ds_name, "keys": list(keys)},
                )
        if len(r.left_keys) != len(r.right_keys):
            raise RelationError(
                f"关联 '{r.name}' 两侧键数量不一致: {len(r.left_keys)} != {len(r.right_keys)}",
                details={"relation": r.name},
            )
        # 同两数据集只允许一条关系
        for ex in self._relations.values():
            pair = {ex.left_dataset, ex.right_dataset}
            if pair == {r.left_dataset, r.right_dataset}:
                raise AmbiguousJoinError(
                    f"数据集 '{r.left_dataset}' 与 '{r.right_dataset}' 之间已存在关联 "
                    f"'{ex.name}'，不得再定义 '{r.name}'（关联方向歧义）",
                    details={
                        "datasets": [r.left_dataset, r.right_dataset],
                        "existing_relation": ex.name,
                        "new_relation": r.name,
                    },
                )

        # 数据层面校验 one 侧键唯一非空
        if r.cardinality is Cardinality.ONE_TO_ONE:
            self._assert_unique(r, r.left_dataset, r.left_keys)
            self._assert_unique(r, r.right_dataset, r.right_keys)
        else:
            one = r.one_side()
            assert one is not None
            self._assert_unique(r, one, r.keys_of(one))

        self._relations[r.name] = r

    def _assert_unique(
        self, r: Relation, ds_name: str, keys: tuple[str, ...]
    ) -> None:
        ds = self._datasets.get(ds_name)
        seen: set[tuple] = set()
        for idx, rec in enumerate(ds.records, start=1):
            vals = tuple(rec.get(k) for k in keys)
            if any(v is None for v in vals):
                raise NonUniqueKeyError(
                    f"关联 '{r.name}' 的 '{ds_name}' 侧键 {list(keys)} 在第 {idx} 行为空，"
                    "空键不能保证关联唯一性",
                    details={
                        "relation": r.name,
                        "dataset": ds_name,
                        "keys": list(keys),
                        "row_index": idx,
                        "reason": "null_key",
                    },
                )
            if vals in seen:
                raise NonUniqueKeyError(
                    f"关联 '{r.name}' 的 '{ds_name}' 侧键 {list(keys)} 出现重复值 "
                    f"{[str(v) for v in vals]!r}，关联会造成行数膨胀",
                    details={
                        "relation": r.name,
                        "dataset": ds_name,
                        "keys": list(keys),
                        "duplicate_value": [str(v) for v in vals],
                        "reason": "duplicate_key",
                    },
                )
            seen.add(vals)

    def get(self, name: str) -> Relation:
        if name not in self._relations:
            raise RelationError(f"关联 '{name}' 不存在", details={"relation": name})
        return self._relations[name]

    def names(self) -> list[str]:
        return sorted(self._relations)

    def resolve_path(self, source: str, target: str) -> list[Relation]:
        """解析 source -> target 的唯一连接路径。

        多条候选路径 -> ``ambiguous_join``；
        路径要求以 source 为 many 端、沿途只做 many->one 上行（或 one_to_one），
        任何 one->many 扇出都判定方向歧义。
        """
        if source == target:
            return []
        # 定向 BFS：只沿「从 many 侧离开 / 经 one_to_one」的边
        # prev[node] = (from_node, relation)
        prev: dict[str, tuple[str, Relation]] = {}
        prev[source] = None  # type: ignore[assignment]
        frontier = [source]
        while frontier:
            nxt: list[str] = []
            for node in frontier:
                for r in self._relations.values():
                    if node not in (r.left_dataset, r.right_dataset):
                        continue
                    other = r.other(node)
                    if other in prev:
                        continue
                    # 方向校验：node 必须是 many 侧，或关系为 one_to_one
                    if r.cardinality is Cardinality.ONE_TO_ONE:
                        allowed = True
                    elif r.many_side() == node:
                        allowed = True
                    else:
                        # 需要从 one 侧向 many 侧扇出 -> 拒绝
                        raise AmbiguousJoinError(
                            f"从 '{source}' 到 '{target}' 的关联需要在关系 '{r.name}' 上"
                            f"从 one 侧 '{node}' 扇出到 many 侧 '{other}'，"
                            "会产生隐性行数膨胀，拒绝该关联方向",
                            details={
                                "relation": r.name,
                                "one_side": node,
                                "many_side": other,
                                "source": source,
                                "target": target,
                            },
                        )
                    prev[other] = (node, r)
                    nxt.append(other)
            frontier = nxt

        if target not in prev:
            raise AmbiguousJoinError(
                f"数据集 '{source}' 与 '{target}' 之间不存在关联路径",
                details={"source": source, "target": target, "reason": "no_path"},
            )

        # 还原路径
        path: list[Relation] = []
        node = target
        while node != source:
            parent, rel = prev[node]
            path.append(rel)
            node = parent
        path.reverse()

        # 唯一性复核：无向图中再用普通 BFS 检查是否存在第二条不同路径
        if self._has_alternative_path(source, target, path):
            raise AmbiguousJoinError(
                f"数据集 '{source}' 与 '{target}' 之间存在多条关联路径，"
                "连接口径不唯一，拒绝静默选择",
                details={"source": source, "target": target, "reason": "multiple_paths"},
            )
        return path

    def _has_alternative_path(
        self, source: str, target: str, chosen: list[Relation]
    ) -> bool:
        """无向枚举简单路径，若超过 1 条返回 True。图很小，DFS 即可。"""
        adj: dict[str, list[str]] = {}
        rels_between: dict[frozenset[str], list[str]] = {}
        for r in self._relations.values():
            adj.setdefault(r.left_dataset, []).append(r.right_dataset)
            adj.setdefault(r.right_dataset, []).append(r.left_dataset)
            rels_between.setdefault(frozenset({r.left_dataset, r.right_dataset}), []).append(
                r.name
            )
        count = 0

        def dfs(node: str, visited: set[str]) -> None:
            nonlocal count
            if node == target:
                count += 1
                return
            for n2 in adj.get(node, []):
                if n2 in visited:
                    continue
                visited.add(n2)
                dfs(n2, visited)
                visited.remove(n2)
                if count > 1:
                    return

        dfs(source, {source})
        return count > 1

    def snapshot(self) -> "RelationGraph":
        g = RelationGraph(self._datasets)
        g._relations = dict(self._relations)
        return g
