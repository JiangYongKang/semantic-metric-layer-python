"""关联关系：键不唯一、方向歧义、多对多必须拒绝；多对一不得产生行数膨胀。"""
import pytest

from sml.errors import (AmbiguousJoinError, NonUniqueJoinKeyError,
                        TypeConflictError, UnknownFieldError)
from sml.model import Agg, Join, Metric, SemanticModel
from tests.conftest import log_case


class TestJoinValidation:
    def test_many_to_one_accepted(self, sales_registry):
        m = SemanticModel(sales_registry)
        m.add_join(Join("o2c", "orders", "cid", "customers", "cid"))
        log_case("多对一关联", "orders.cid(重复) -> customers.cid(唯一)",
                 "一侧键唯一即合法", "接受")

    def test_both_sides_non_unique_rejected(self, registry):
        registry.register("a", [{"k": 1}, {"k": 1}])
        registry.register("b", [{"k": 1}, {"k": 1}])
        m = SemanticModel(registry)
        with pytest.raises(NonUniqueJoinKeyError) as ei:
            m.add_join(Join("j", "a", "k", "b", "k"))
        log_case("多对多", "两侧键均重复", "会产生行数膨胀", ei.value.code)

    def test_reversed_direction_rejected(self, registry):
        registry.register("a", [{"k": 1}, {"k": 2}])       # 唯一
        registry.register("b", [{"k": 1}, {"k": 1}])       # 不唯一
        m = SemanticModel(registry)
        with pytest.raises(AmbiguousJoinError) as ei:
            m.add_join(Join("j", "a", "k", "b", "k"))
        log_case("方向歧义", "声明的一侧 b 键不唯一而多侧 a 唯一",
                 "方向可能写反，拒绝而非静默取其一", ei.value.code)

    def test_key_type_mismatch_rejected(self, registry):
        registry.register("a", [{"k": 1}])
        registry.register("b", [{"k": "1"}])
        m = SemanticModel(registry)
        with pytest.raises(TypeConflictError) as ei:
            m.add_join(Join("j", "a", "k", "b", "k"))
        log_case("键类型冲突", "int vs string", "类型必须一致", ei.value.code)

    def test_missing_key_rejected(self, sales_registry):
        m = SemanticModel(sales_registry)
        with pytest.raises(UnknownFieldError) as ei:
            m.add_join(Join("j", "orders", "nope", "customers", "cid"))
        log_case("键不存在", "orders.nope", "字段必须存在", ei.value.code)

    def test_null_key_on_one_side_rejected(self, registry):
        registry.register("a", [{"k": 1}])
        registry.register("b", [{"k": 1, "v": "x"}, {"k": None, "v": "y"}])
        m = SemanticModel(registry)
        with pytest.raises(AmbiguousJoinError) as ei:
            m.add_join(Join("j", "a", "k", "b", "k"))
        log_case("一侧键含空", "b.k 有 None", "匹配语义不确定", ei.value.code)


class TestJoinExecution:
    def test_no_row_inflation(self, sales_registry, model):
        """多对一关联后行数必须等于事实表行数（4），不得膨胀。"""
        from sml.engine import Query, QueryEngine
        eng = QueryEngine(sales_registry, model)
        r = eng.run(Query(metrics=("n_orders",), dimensions=("customers.city",)))
        total = sum(row["n_orders"] for row in r.rows)
        log_case("关联不膨胀", "4 行 orders 关联 2 行 customers",
                 "各组计数之和 == 事实表行数", total)
        assert total == 4

    def test_unmatched_left_join_gives_null_group(self, sales_registry, model):
        from sml.engine import Query, QueryEngine
        eng = QueryEngine(sales_registry, model)
        r = eng.run(Query(metrics=("total_amt",), dimensions=("customers.city",)))
        null_groups = [row for row in r.rows if row["customers.city"] is None]
        log_case("左连接未匹配", "orders.cid=99 无对应客户",
                 "未匹配行进 NULL 组而非丢失或膨胀", null_groups)
        assert len(null_groups) == 1
        assert str(null_groups[0]["total_amt"]) == "1.0"

    def test_join_broken_by_later_update_rejected_at_query(self, sales_registry, model):
        """关联定义后数据被更新成一侧键不唯一：查询时必须显式拒绝。"""
        from sml.engine import Query, QueryEngine
        sales_registry.update("customers", [{"cid": 10, "city": "hz"},
                                            {"cid": 10, "city": "sh"}])
        eng = QueryEngine(sales_registry, model)
        with pytest.raises(NonUniqueJoinKeyError) as ei:
            eng.run(Query(metrics=("total_amt",), dimensions=("customers.city",)))
        log_case("更新后键不再唯一", "customers 出现重复 cid=10",
                 "执行时基于快照复核唯一性", ei.value.code)
