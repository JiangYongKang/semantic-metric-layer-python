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


class ServiceVersionTest(LoggingTestCase):
    def _client_with_history(self) -> TestClient:
        layer = MetricLayer(history_retention=1)
        return TestClient(create_app(layer))

    def test_versions_endpoint_and_historical_query(self) -> None:
        c = self._client_with_history()
        r = c.post("/datasets/t", json={
            "primary_key": ["id"],
            "records": [{"id": 1, "v": 1}],
        })
        self.assertEqual(r.status_code, 200, r.text)
        c.post("/measures", json={"name": "c", "dataset": "t", "agg": "count"})
        old = c.get("/meta").json()["model_version"]
        c.put("/datasets/t/data", json={
            "records": [{"id": 1, "v": 1}, {"id": 2, "v": 2}]
        })
        info = c.get("/versions").json()
        self.log_judgement("GET /versions", "当前=旧+1、历史含旧版、retention=1", info)
        self.assertEqual(info["current_version"], old + 1)
        self.assertEqual(info["retained_versions"], [old])
        self.assertEqual(info["oldest_retained_version"], old)
        self.assertEqual(info["history_retention"], 1)

        cur = c.post("/query", json={"measures": ["c"]}).json()
        hist = c.post("/query", json={"measures": ["c"], "at_version": old}).json()
        self.assertEqual(cur["rows"], [[2]])
        self.assertEqual(hist["rows"], [[1]])
        self.assertTrue(hist["historical"])
        self.assertEqual(hist["model_version"], old)
        self.assertEqual(hist["current_version"], old + 1)

    def test_version_error_http_status_mapping(self) -> None:
        c = self._client_with_history()
        c.post("/datasets/t", json={"records": [{"id": 1, "v": 1}]})
        c.post("/measures", json={"name": "c", "dataset": "t", "agg": "count"})
        # 再发一次更新，把第一版挤出 retention=1 的窗口
        c.put("/datasets/t/data", json={
            "records": [{"id": 1, "v": 1}, {"id": 2, "v": 2}]
        })
        c.put("/datasets/t/data", json={
            "records": [{"id": 1, "v": 1}, {"id": 2, "v": 2}, {"id": 3, "v": 3}]
        })
        ev = c.post("/query", json={"measures": ["c"], "at_version": 1})
        self.log_judgement("HTTP 回查已淘汰版本", "410 version_evicted",
                           f"{ev.status_code} {ev.json()['error']['code']}")
        self.assertEqual(ev.status_code, 410)
        self.assertEqual(ev.json()["error"]["code"], "version_evicted")
        nf = c.post("/query", json={"measures": ["c"], "at_version": 999})
        self.assertEqual(nf.status_code, 404)
        self.assertEqual(nf.json()["error"]["code"], "version_not_found")

    def test_set_retention_endpoint(self) -> None:
        c = self._client_with_history()
        c.post("/datasets/t", json={"records": [{"id": 1, "v": 1}]})
        c.post("/measures", json={"name": "c", "dataset": "t", "agg": "count"})
        c.put("/datasets/t/data", json={
            "records": [{"id": 1, "v": 1}, {"id": 2, "v": 2}]
        })
        r = c.post("/versions/retention", json={"retention": 0})
        body = r.json()
        self.log_judgement("retention 调为 0", "返回被淘汰版本且留存为空", body)
        self.assertEqual(body["history_retention"], 0)
        self.assertEqual(body["retained_versions"], [])
        self.assertTrue(body["evicted_versions"])
        bad = c.post("/versions/retention", json={"retention": -1})
        self.assertEqual(bad.status_code, 422)
        self.assertEqual(bad.json()["error"]["code"], "invalid_retention")


if __name__ == "__main__":
    unittest.main()
