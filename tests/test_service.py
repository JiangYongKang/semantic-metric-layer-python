"""HTTP 端到端测试：错误码映射、Decimal 字符串化、血缘、版本/缓存。"""

from __future__ import annotations

import sys
import unittest

from fastapi.testclient import TestClient

from sml.registry import MetricLayer
from sml.service import create_app
from tests._support import LoggingTestCase


def _client() -> TestClient:
    return TestClient(create_app(MetricLayer()))


class ServiceTest(LoggingTestCase):
    def _seed(self, c: TestClient) -> None:
        r = c.post("/datasets/orders", json={
            "primary_key": ["oid"],
            "records": [
                {"oid": 1, "cid": 10, "amount": "100.00"},
                {"oid": 2, "cid": 10, "amount": "0.10"},
                {"oid": 3, "cid": 20, "amount": "200.30"},
            ],
        })
        assert r.status_code == 200, r.text
        r = c.post("/datasets/customers", json={
            "primary_key": ["cid"],
            "records": [{"cid": 10, "city": "BJ"}, {"cid": 20, "city": "SH"}],
        })
        assert r.status_code == 200, r.text
        assert c.post("/relations", json={
            "name": "o_c", "left_dataset": "orders", "left_keys": ["cid"],
            "right_dataset": "customers", "right_keys": ["cid"],
            "cardinality": "many_to_one",
        }).status_code == 200
        assert c.post("/dimensions", json={
            "name": "city", "dataset": "customers", "field": "city"}).status_code == 200
        assert c.post("/measures", json={
            "name": "total", "dataset": "orders", "agg": "sum", "field": "amount"}
        ).status_code == 200
        assert c.post("/measures", json={
            "name": "cnt", "dataset": "orders", "agg": "count"}).status_code == 200
        assert c.post("/ratios", json={
            "name": "avg_amt", "dataset": "orders",
            "numerator": "total", "denominator": "cnt"}).status_code == 200

    def test_end_to_end_query(self) -> None:
        c = _client()
        self._seed(c)
        r = c.post("/query", json={
            "dimensions": ["city"], "measures": ["total", "cnt", "avg_amt"],
        })
        self.assertEqual(r.status_code, 200, r.text)
        body = r.json()
        self.log_judgement("HTTP 按 city 聚合", "BJ 总额字符串 100.10", body["rows"])
        bj = next(row for row in body["rows"] if row[0] == "BJ")
        self.assertEqual(bj[1], "100.10")  # Decimal 以字符串输出
        self.assertEqual(bj[3], "50.0500000000")
        self.assertEqual([col["output_column"] for col in body["lineage"]["columns"]],
                         body["columns"])
        ratio_col = next(x for x in body["lineage"]["columns"] if x["output_column"] == "avg_amt")
        self.assertEqual(ratio_col["kind"], "ratio")
        self.assertEqual(body["model_version"], c.get("/meta").json()["model_version"])

    def test_float_payload_rejected_with_code(self) -> None:
        c = _client()
        r = c.post("/datasets/t", json={"records": [{"id": 1, "v": 1.5}]})
        self.log_judgement("JSON 浮点 1.5", "422 type_conflict",
                           f"{r.status_code} {r.json()['error']['code']}")
        self.assertEqual(r.status_code, 422)
        self.assertEqual(r.json()["error"]["code"], "type_conflict")

    def test_missing_field_distinguishable(self) -> None:
        c = _client()
        r = c.post("/datasets/t", json={"records": [{"id": 1}, {"id": 2, "v": 1}]})
        self.assertEqual(r.status_code, 422)
        self.assertEqual(r.json()["error"]["code"], "field_missing")

    def test_drop_column_rejected_http(self) -> None:
        c = _client()
        self._seed(c)
        r = c.put("/datasets/orders/data", json={"records": [{"oid": 1}]})
        self.log_judgement("更新删除 amount/cid", "409 source_update_rejected",
                           f"{r.status_code} {r.json()['error']['code']}")
        self.assertEqual(r.status_code, 409)
        self.assertEqual(r.json()["error"]["code"], "source_update_rejected")
        # 旧数据仍可查询
        r2 = c.post("/query", json={"measures": ["cnt"]})
        self.assertEqual(r2.json()["rows"], [[3]])

    def test_resource_limit_rejected_and_no_pollution(self) -> None:
        c = _client()
        self._seed(c)
        r = c.post("/query", json={
            "dimensions": ["city"], "measures": ["cnt"],
            "budget": {"max_groups": 1},
        })
        self.log_judgement("max_groups=1", "429 resource_limit, rejected=true",
                           f"{r.status_code} {r.json()}")
        self.assertEqual(r.status_code, 429)
        self.assertEqual(r.json()["error"]["code"], "resource_limit")
        # 失败查询不写缓存：同一查询不带限制后仍正常
        r2 = c.post("/query", json={"dimensions": ["city"], "measures": ["cnt"]})
        self.assertEqual(r2.status_code, 200)
        self.assertEqual(len(r2.json()["rows"]), 2)

    def test_ambiguous_join_http(self) -> None:
        c = _client()
        self._seed(c)
        # 加 cities 并构造双路径
        c.put("/datasets/orders/data", json={"records": [
            {"oid": 1, "cid": 10, "amount": "1", "city": "BJ"},
            {"oid": 2, "cid": 10, "amount": "2", "city": "BJ"},
            {"oid": 3, "cid": 20, "amount": "3", "city": "SH"},
        ]})
        c.post("/datasets/cities", json={
            "primary_key": ["city"],
            "records": [{"city": "BJ", "r": "N"}, {"city": "SH", "r": "E"}],
        })
        c.post("/relations", json={
            "name": "c_city", "left_dataset": "customers", "left_keys": ["city"],
            "right_dataset": "cities", "right_keys": ["city"],
            "cardinality": "many_to_one"})
        c.post("/relations", json={
            "name": "o_city", "left_dataset": "orders", "left_keys": ["city"],
            "right_dataset": "cities", "right_keys": ["city"],
            "cardinality": "many_to_one"})
        c.post("/dimensions", json={"name": "r", "dataset": "cities", "field": "r"})
        r = c.post("/query", json={"dimensions": ["r"], "measures": ["cnt"]})
        self.log_judgement("orders 到 cities 双路径", "409 ambiguous_join",
                           f"{r.status_code} {r.json()['error']['code']}")
        self.assertEqual(r.status_code, 409)
        self.assertEqual(r.json()["error"]["code"], "ambiguous_join")

    def test_truncate_mode_marks_result(self) -> None:
        c = _client()
        self._seed(c)
        r = c.post("/query", json={
            "dimensions": ["city"], "measures": ["cnt"],
            "budget": {"max_output_rows": 1, "on_overflow": "truncate"},
        })
        body = r.json()
        self.log_judgement("max_output_rows=1 truncate", "1 行且 truncated=true",
                           f"{len(body['rows'])}, {body['truncated']}")
        self.assertEqual(len(body["rows"]), 1)
        self.assertTrue(body["truncated"])


if __name__ == "__main__":
    unittest.main()
