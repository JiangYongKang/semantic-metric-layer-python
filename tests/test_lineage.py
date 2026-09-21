"""列级血缘：每个输出列都可追溯到来源字段与口径，且与结果一致。"""
from sml.engine import Query, QueryEngine
from tests.conftest import log_case


def test_every_output_column_has_lineage(sales_registry, model):
    eng = QueryEngine(sales_registry, model)
    r = eng.run(Query(metrics=("total_amt", "avg_qty", "amt_per_order"),
                      dimensions=("customers.city",)))
    lineage_cols = {l.output_column for l in r.lineage}
    log_case("血缘完整", r.columns, "每个输出列都有血缘记录", lineage_cols)
    assert lineage_cols == set(r.columns)


def test_lineage_sources_match_result(sales_registry, model):
    eng = QueryEngine(sales_registry, model)
    r = eng.run(Query(metrics=("total_amt",), dimensions=("customers.city",)))
    by_col = {l.output_column: l for l in r.lineage}
    log_case("血缘内容", "sum 指标 + 维度",
             "指标列指向 orders.amt 与 sum 口径；维度列指向 customers.city",
             {k: v.to_dict() for k, v in by_col.items()})
    assert by_col["total_amt"].source_fields == ("orders.amt",)
    assert by_col["total_amt"].expression == "sum(orders.amt)"
    assert by_col["customers.city"].source_fields == ("customers.city",)
    assert by_col["customers.city"].kind == "dimension"


def test_ratio_lineage_expands_transitive_sources(sales_registry, model):
    eng = QueryEngine(sales_registry, model)
    r = eng.run(Query(metrics=("amt_per_order",)))
    lin = r.lineage[0]
    log_case("比率血缘", "amt_per_order = total_amt / n_orders",
             "血缘需展开到原始字段 orders.amt", lin.to_dict())
    assert "orders.amt" in lin.source_fields
    assert lin.expression == "ratio(total_amt / n_orders)"


def test_lineage_consistent_with_versions(sales_registry, model):
    """血缘随结果一起返回，且携带数据版本，可追溯重算。"""
    eng = QueryEngine(sales_registry, model)
    r = eng.run(Query(metrics=("total_amt",)))
    sales_registry.update("orders", [{"oid": 9, "cid": 10, "amt": 100.0, "qty": 1}])
    r2 = eng.run(Query(metrics=("total_amt",)))
    log_case("血缘与版本", "更新前后同一查询",
             "版本号不同 -> 结果与血缘可区分",
             (r.dataset_versions, r2.dataset_versions))
    assert r.dataset_versions["orders"] == 1
    assert r2.dataset_versions["orders"] == 2
    assert r.rows != r2.rows
