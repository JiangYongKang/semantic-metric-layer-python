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

    # ------------------------------------------------------------------
    # 历史版本回查
    # ------------------------------------------------------------------
    def test_historical_query_http_returns_then_snapshot(self) -> None:
        c = _client()
        self._seed(c)
        past = c.get("/versions").json()["current_version"]
        # 更新 orders 数据（BJ 金额变化、行数变化）
        r = c.put("/datasets/orders/data", json={"records": [
            {"oid": 1, "cid": 10, "region": "east", "amount": "999.00", "qty": 2},
            {"oid": 2, "cid": 10, "region": "east", "amount": "0.10", "qty": 1},
            {"oid": 3, "cid": 20, "region": "west", "amount": "200.30", "qty": 3},
            {"oid": 4, "cid": 20, "region": "west", "amount": "5.00", "qty": 1},
        ]})
        self.assertEqual(r.status_code, 200, r.text)
        listing = c.get("/versions").json()
        self.assertIn(past, listing["history_versions"])
        self.assertEqual(listing["max_history"], 10)

        cur = c.post("/query", json={"dimensions": ["city"], "measures": ["total", "cnt"]})
        old = c.post("/query", json={
            "dimensions": ["city"], "measures": ["total", "cnt"],
            "at_version": past,
        })
        self.assertEqual(old.status_code, 200, old.text)
        ob = old.json()
        self.log_judgement("HTTP 回查历史版本",
                           "model_version=历史版本、historical=true、BJ=100.10",
                           f"mv={ob['model_version']}, cur={ob['current_version']}")
        self.assertEqual(ob["model_version"], past)
        self.assertEqual(ob["current_version"], listing["current_version"])
        self.assertTrue(ob["historical"])
        bj = next(row for row in ob["rows"] if row[0] == "BJ")
        self.assertEqual(bj[1], "100.10")
        # 当前版本 BJ 已是 999.10，明确不同
        cur_bj = next(row for row in cur.json()["rows"] if row[0] == "BJ")
        self.assertEqual(cur_bj[1], "999.10")

    def test_version_error_codes_http(self) -> None:
        c = _client()
        self._seed(c)
        # 从未存在 -> 404
        r1 = c.post("/query", json={"measures": ["cnt"], "at_version": 9999})
        self.assertEqual(r1.status_code, 404)
        self.assertEqual(r1.json()["error"]["code"], "version_never_existed")
        # 超范围 -> 400
        r0 = c.post("/query", json={"measures": ["cnt"], "at_version": 0})
        self.assertEqual(r0.status_code, 400)
        self.assertEqual(r0.json()["error"]["code"], "version_out_of_range")
        self.log_judgement("HTTP 版本不存在/超范围",
                           "404 version_never_existed / 400 version_out_of_range",
                           f"{r1.status_code}/{r0.status_code}")

    def test_version_evicted_http_410(self) -> None:
        from fastapi.testclient import TestClient

        c = TestClient(create_app(MetricLayer(max_history=1)))
        c.post("/datasets/t", json={"primary_key": ["id"],
                                    "records": [{"id": 1, "v": "1"}]})
        c.post("/measures", json={"name": "c", "dataset": "t", "agg": "count"})
        c.post("/measures", json={"name": "s", "dataset": "t",
                                  "agg": "sum", "field": "v"})
        c.post("/dimensions", json={"name": "g", "dataset": "t", "field": "v"})
        # 容量 1：最早的版本已淘汰
        r = c.post("/query", json={"measures": ["c"], "at_version": 1})
        self.log_judgement("HTTP 回查已淘汰版本",
                           "410 version_evicted，不回退当前",
                           f"{r.status_code} {r.json()['error']['code']}")
        self.assertEqual(r.status_code, 410)
        self.assertEqual(r.json()["error"]["code"], "version_evicted")

    def test_set_history_limit_http(self) -> None:
        c = _client()
        self._seed(c)
        r = c.post("/history/limit", json={"max_history": 0})
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(r.json()["max_history"], 0)
        listing = c.get("/versions").json()
        self.assertEqual(listing["history_versions"], [])
        # 非法值 -> 400
        bad = c.post("/history/limit", json={"max_history": -1})
        self.assertEqual(bad.status_code, 400)
        self.assertEqual(bad.json()["error"]["code"], "version_out_of_range")
        self.log_judgement("HTTP 调整保留容量", "成功置 0；非法值 400", "ok")


if __name__ == "__main__":
    unittest.main()
