import pytest
from app.main import create_app

BANDS = [
    {"grade": "一等", "max_moisture": "13.5", "max_impurity": "1.0", "price": "2.80"},
    {"grade": "二等", "max_moisture": "15.0", "max_impurity": "2.0", "price": "2.60"},
    {"grade": "三等", "max_moisture": "16.5", "max_impurity": "4.0", "price": "2.40"},
]
CONTRACT = dict(contract_id="HT1", grain="小麦", standard_moisture="12.5",
                basis="worst_point", max_moisture="18", max_impurity="6", bands=BANDS)


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    return create_app().test_client()


def seed(client):
    client.post("/api/grading/contracts", json=CONTRACT)
    client.post("/api/grading/bins",
                json={"bin_id": "仓A", "grain": "小麦", "capacity_kg": "5000"})


def arrive(client, truck, points, net="1000"):
    return client.post("/api/grading/trucks/arrive",
                       json={"contract_id": "HT1", "truck_id": truck, "bin_id": "仓A",
                             "net_kg": net, "points": points})


def test_grading_page_renders(client):
    assert client.get("/grading").status_code == 200


def test_full_flow_through_http(client):
    seed(client)
    r = arrive(client, "C1", [{"moisture": "14.0", "impurity": "1.5"}])
    assert r.status_code == 200
    body = r.get_json()
    assert body["conclusion"]["grade"] == "二等"
    assert body["bin"]["remaining_kg"] == "4000.000000"

    # 同化验重试：幂等，不重复计价。
    again = arrive(client, "C1", [{"moisture": "14.0", "impurity": "1.5"}])
    assert again.get_json()["mode"] == "idempotent_retry"

    # 新化验撞锁单：409，金额不变。
    locked = arrive(client, "C1", [{"moisture": "16.0", "impurity": "3.0"}])
    assert locked.status_code == 409
    assert locked.get_json()["code"] == "PAYABLE_LOCKED"

    # 后到车拉低仓批等级。
    arrive(client, "C2", [{"moisture": "16.0", "impurity": "3.0"}])
    bin_view = client.get("/api/grading/bins/仓A").get_json()["bin"]
    assert bin_view["grade"] == "三等"
    assert any(h["event_type"] == "BIN_MIX_INVALID" for h in bin_view["mix_history"])

    # 待复验车与复检。
    bad = arrive(client, "C9", [{"moisture": "18.5", "impurity": "1.0"}]).get_json()
    assert bad["conclusion"]["status"] == "pending_recheck"
    pending = client.get("/api/grading/pending").get_json()
    assert pending["count"] == 1 and pending["trucks"][0]["truck_id"] == "C9"

    rc = client.post("/api/grading/trucks/recheck",
                     json={"contract_id": "HT1", "truck_id": "C9", "bin_id": "仓A",
                           "net_kg": "1000",
                           "points": [{"moisture": "14.0", "impurity": "1.5"}]})
    assert rc.status_code == 200
    new = rc.get_json()["conclusion"]
    assert new["version"] == 2 and new["status"] == "accepted"
    assert new["supersedes"] == bad["conclusion"]["conclusion_id"]

    # 批次视图：逐车结论、仓批等级、待复验同屏可查。
    batch = client.get("/api/grading/batches/HT1").get_json()
    trucks = {c["truck_id"]: c for c in batch["truck_conclusions"]}
    assert trucks["C1"]["amount"] == body["conclusion"]["amount"]
    assert batch["summary"]["locked_payables"] == 3
    assert batch["pending_recheck"] == []

    ledger = client.get("/api/grading/ledger").get_json()["events"]
    kinds = {e["event_type"] for e in ledger}
    assert {"TRUCK_ARRIVE", "BIN_MIX_INVALID", "CONCLUSION_SUPERSEDE",
            "TRUCK_RECHECK_PASS"} <= kinds


def test_cross_grain_rejected_over_http(client):
    seed(client)
    client.post("/api/grading/bins", json={"bin_id": "仓B", "grain": "玉米"})
    r = client.post("/api/grading/trucks/arrive",
                    json={"contract_id": "HT1", "truck_id": "C1", "bin_id": "仓B",
                          "net_kg": "1000", "points": [{"moisture": "14", "impurity": "1"}]})
    assert r.status_code == 422 and r.get_json()["code"] == "GRAIN_MISMATCH"


def test_missing_contract_returns_404(client):
    r = client.get("/api/grading/batches/NOPE")
    assert r.status_code == 404 and r.get_json()["code"] == "NOT_FOUND"


def test_invalid_basis_rejected(client):
    r = client.post("/api/grading/contracts", json={**CONTRACT, "basis": "guess"})
    assert r.status_code == 422 and r.get_json()["code"] == "BASIS"


def test_decimals_serialized_as_strings(client):
    seed(client)
    r = arrive(client, "C1", [{"moisture": "14.0", "impurity": "1.5"}]).get_json()
    c = r["conclusion"]
    assert isinstance(c["amount"], str) and isinstance(c["standard_kg"], str)
    assert isinstance(r["bin"]["remaining_kg"], str)


def test_existing_seven_modules_unaffected(client):
    for stage in range(1, 8):
        assert client.get(f"/api/m{stage}").status_code == 200
