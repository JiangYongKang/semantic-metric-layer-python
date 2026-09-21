"""本地数据源：内存记录与 CSV 文件。

数据源只负责承载原始记录，不做结构判断；结构推断在 dataset 层完成。
CSV 单元格经 :func:`sml.types.parse_scalar` 做确定性解析，
因此 CSV 与内存记录走完全相同的类型规则。
"""

from __future__ import annotations

import csv as _csv
from dataclasses import dataclass, field

from .types import parse_scalar


@dataclass
class LocalSource:
    """一个本地数据集的原始载体。

    ``records`` 为 ``dict[列名, 值]`` 列表，值为 None/int/Decimal/str/bool。
    """

    name: str
    records: list[dict] = field(default_factory=list)

    @classmethod
    def from_csv(cls, name: str, path: str, *, encoding: str = "utf-8") -> "LocalSource":
        """读取带表头的 CSV；表头重名或空表头直接拒绝。"""
        with open(path, newline="", encoding=encoding) as fh:
            reader = _csv.reader(fh)
            try:
                header = next(reader)
            except StopIteration:
                # 完全空文件：没有任何列，交给注册阶段按结构不完整拒绝
                return cls(name=name, records=[])
            names = [h.strip() for h in header]
            if any(n == "" for n in names):
                from .errors import IncompleteStructureError

                raise IncompleteStructureError(
                    f"数据源 '{name}' 的 CSV 表头存在空列名",
                    details={"source": name, "header": names},
                )
            if len(set(names)) != len(names):
                from .errors import IncompleteStructureError

                dupes = sorted({n for n in names if names.count(n) > 1})
                raise IncompleteStructureError(
                    f"数据源 '{name}' 的 CSV 表头存在重名列: {dupes}",
                    details={"source": name, "duplicates": dupes},
                )
            records: list[dict] = []
            for line_no, row in enumerate(reader, start=2):
                if not row or all(c.strip() == "" for c in row):
                    continue  # 纯空行跳过（不计为全空记录）
                if len(row) != len(names):
                    from .errors import IncompleteStructureError

                    raise IncompleteStructureError(
                        f"数据源 '{name}' 的 CSV 第 {line_no} 行列数 {len(row)} 与表头 {len(names)} 不一致",
                        details={
                            "source": name,
                            "line": line_no,
                            "expected_columns": len(names),
                            "actual_columns": len(row),
                        },
                    )
                records.append(
                    {n: parse_scalar(v, field=n) for n, v in zip(names, row)}
                )
        return cls(name=name, records=records)

    def snapshot(self) -> list[dict]:
        """返回当前记录的浅拷贝快照（值本身为不可变标量）。"""
        return [dict(r) for r in self.records]


def normalize_text_records(records: list[dict]) -> list[dict]:
    """把「文本载体」记录规范化为原生标量记录。

    适用于 CSV 行与 JSON API 行两种入站形态：字符串单元格经
    :func:`sml.types.parse_scalar` 确定性解析；Python float 一律拒绝
    （二进制浮点不得进入精确数值体系，金额请用字符串或整数传递）。
    """
    out: list[dict] = []
    for rec in records:
        row: dict = {}
        for k, v in rec.items():
            if isinstance(v, float):
                from .errors import TypeConflictError

                raise TypeConflictError(
                    f"字段 '{k}' 收到 JSON 浮点值 {v!r}：请改用字符串（如 '10.50'）或整数",
                    details={"field": k, "value": repr(v)},
                )
            row[k] = parse_scalar(v, field=k) if isinstance(v, str) else v
        out.append(row)
    return out
