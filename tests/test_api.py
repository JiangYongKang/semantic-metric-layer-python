"""HTTP 服务层：注册 -> 建模 -> 查询 -> 错误分类。"""
from fastapi.testclient import TestClient

from main import app
from tests.conftest import log_case

client = TestClient(app, raise_server_exceptions=False)


def test_end_to_end_flow():
    assert client.post("/datasets/orders", json={"rows": [
        {"oid": 1, "cid": 10, "amt": 0.1},
        {"oid": 2, "cid": 10, "amt": 0.2},
    ]}).status_code == 200
    assert client.post("/datasets/customers", json={"rows": [
        {"cid": 10, "city": "hz"}]}).status_code == 200
    r = client.post("/model/joins", json={
        "name": "o2c", "left_dataset": "orders", "left_key": "cid",
        "right_dataset": "customers", "right_key": "cid"})
    assert r.status_code == 200
    r = client.post("/model/metrics", json={
        "name": "total", "dataset": "orders", "field": "amt", "agg": "sum"})
    assert r.status_code == 200
    r = client.post("/query", json={
        "metrics": ["total"], "dimensions": ["customers.city"]})
    body = r.json()
    log_case("端到端查询", "sum(orders.amt) by customers.city",
             "0.1+0.2 精确等于 0.3，血缘齐全", body)
    assert r.status_code == 200
    assert body["rows"] == [{"customers.city": "hz", "total": "0.3"}]
    assert {l["output_column"] for l in body["lineage"]} == set(body["columns"])
    assert body["dataset_versions"]["orders"] == 1


def test_error_classification_over_http():
    r = client.post("/datasets/bad", json={"rows": [{"a": 1}, {"a": "x"}]})
    log_case("HTTP 错误分类", "类型冲突的数据集", "422 + TYPE_CONFLICT", r.json())
    assert r.status_code == 422
    assert r.json()["code"] == "TYPE_CONFLICT"
    r = client.post("/query", json={"metrics": ["nonexistent"]})
    assert r.status_code == 422
    assert r.json()["code"] == "UNKNOWN_FIELD"
