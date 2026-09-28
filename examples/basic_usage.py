"""端到端可运行示例：本地数据集 -> 口径 -> 关联 -> 查询 -> 血缘。

运行：uv run python examples/basic_usage.py
"""

from __future__ import annotations

import json
from decimal import Decimal

from sml.caliber import AggKind, DimensionSpec, MeasureSpec, RatioSpec
from sml.engine import Filter, Query
from sml.limits import LimitAction, ResourceBudget
from sml.registry import MetricLayer
from sml.relations import Cardinality, Relation


def main() -> None:
    layer = MetricLayer()

    # 1) 注册本地数据集
    #    Python 内存注册要求数值就是数值（int/Decimal）；
    #    字符串仅在 CSV/HTTP 文本载体上经确定性解析变为数值。
    layer.register_dataset(
        "orders",
        [
            {"oid": 1, "cid": 10, "region": "east", "amount": Decimal("100.10"), "qty": 2},
            {"oid": 2, "cid": 10, "region": "east", "amount": Decimal("0.20"), "qty": None},
            {"oid": 3, "cid": 20, "region": "west", "amount": Decimal("200.30"), "qty": 3},
            {"oid": 4, "cid": 99, "region": None, "amount": None, "qty": 5},
        ],
        primary_key=("oid",),
    )
    layer.register_dataset(
        "customers",
        [{"cid": 10, "tier": "gold"}, {"cid": 20, "tier": "silver"}],
        primary_key=("cid",),
    )

    # 2) 关联：orders(*cid) -> customers(cid)，one 侧键唯一在注册期校验
    layer.add_relation(
        Relation("o_c", "orders", ("cid",), "customers", ("cid",),
                 Cardinality.MANY_TO_ONE)
    )

    # 3) 维度 / 指标 / 比率口径
    layer.add_dimension(DimensionSpec("region", "orders", "region"))
    layer.add_dimension(DimensionSpec("tier", "customers", "tier"))
    layer.add_measure(MeasureSpec("total_amount", "orders", AggKind.SUM, "amount"))
    layer.add_measure(MeasureSpec("orders_cnt", "orders", AggKind.COUNT))
    layer.add_measure(MeasureSpec("avg_amount", "orders", AggKind.AVG, "amount"))
    layer.add_ratio(RatioSpec("avg_order_value", "orders",
                              "total_amount", "orders_cnt"))

    print("model_version =", layer.model_version())

    # 4) 查询：按 region 分组 + 过滤
    result = layer.query(
        Query(
            dimensions=("region",),
            measures=("total_amount", "orders_cnt", "avg_amount", "avg_order_value"),
            filters=(Filter("region", ("east",), negate=True),),  # west + NULL 桶
        ),
        budget=ResourceBudget(on_overflow=LimitAction.REJECT),
    )
    print("columns      =", result.columns)
    for row in result.rows:
        print("row          =", tuple(format(v, "f") if isinstance(v, Decimal) else v for v in row))
    print("truncated    =", result.truncated)
    print("stats        =", result.resource_stats)
    print("lineage      =", json.dumps(result.lineage.to_dict(), ensure_ascii=False, indent=2))

    # 5) 跨表维度（沿唯一 many->one 路径）
    result2 = layer.query(
        Query(dimensions=("tier",), measures=("total_amount", "orders_cnt"))
    )
    for row in result2.rows:
        print("by tier row  =", tuple(format(v, "f") if isinstance(v, Decimal) else v for v in row))

    # 6) 历史版本回查：记录当前版本，改数据后回看，结果与当时逐字节一致
    version_then = result.model_version
    layer.replace_dataset_data(
        "orders",
        [
            {"oid": 1, "cid": 10, "region": "east", "amount": Decimal("999.00"), "qty": 2},
            {"oid": 2, "cid": 10, "region": "east", "amount": Decimal("1.00"), "qty": None},
            {"oid": 3, "cid": 20, "region": "west", "amount": Decimal("1.00"), "qty": 3},
            {"oid": 4, "cid": 99, "region": None, "amount": None, "qty": 5},
        ],
    )
    replay = layer.query(
        Query(
            dimensions=("region",),
            measures=("total_amount", "orders_cnt", "avg_amount", "avg_order_value"),
            filters=(Filter("region", ("east",), negate=True),),
        ),
        at_version=version_then,
    )
    print("replay ver   =", replay.model_version,
          "current =", layer.model_version(),
          "historical =", replay.resource_stats.get("historical"))
    for row in replay.rows:
        print("replay row   =", tuple(format(v, "f") if isinstance(v, Decimal) else v for v in row))
    print("replay == 当时结果:", replay.rows == result.rows,
          "血缘一致:", replay.lineage.to_dict() == result.lineage.to_dict())


if __name__ == "__main__":
    main()
