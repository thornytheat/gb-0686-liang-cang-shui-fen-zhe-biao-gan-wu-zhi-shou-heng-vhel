import pytest
from decimal import Decimal as D
from app.errors import DomainError
from app.grading.service import GradingService
from app.grading.store import GradingStore

BANDS = [
    {"grade": "一等", "max_moisture": "13.5", "max_impurity": "1.0", "price": "2.80"},
    {"grade": "二等", "max_moisture": "15.0", "max_impurity": "2.0", "price": "2.60"},
    {"grade": "三等", "max_moisture": "16.5", "max_impurity": "4.0", "price": "2.40"},
]
CONTRACT = dict(contract_id="HT1", grain="小麦", standard_moisture="12.5",
                basis="worst_point", max_moisture="18", max_impurity="6", bands=BANDS)


@pytest.fixture
def svc(tmp_path):
    service = GradingService(GradingStore(str(tmp_path)))
    service.open_contract(CONTRACT)
    service.register_bin({"bin_id": "仓A", "grain": "小麦", "capacity_kg": "5000"})
    return service


def arrive(svc, truck, points, net="1000", bin_id="仓A"):
    return svc.arrive({"contract_id": "HT1", "truck_id": truck, "bin_id": bin_id,
                       "net_kg": net, "points": points})


P_GOOD2 = [{"moisture": "14.0", "impurity": "1.5"}]
P_GOOD3 = [{"moisture": "16.0", "impurity": "3.0"}]
P_BAD = [{"moisture": "18.5", "impurity": "1.0"}]


def test_arrive_returns_one_reviewable_conclusion_with_grade_weight_amount(svc):
    out = arrive(svc, "C1", P_GOOD2)
    c = out["conclusion"]
    assert c["status"] == "accepted" and c["grade"] == "二等"
    assert D(c["amount"]) > 0 and D(c["standard_kg"]) > 0
    assert c["conclusion_id"] and len(c["points"]) == 1
    # 同时能看清进的是哪一仓、混仓后余量怎么变。
    assert out["bin"]["bin_id"] == "仓A"
    assert D(out["bin"]["remaining_kg"]) == D("4000")


def test_identical_assay_retry_is_idempotent_and_never_double_billed(svc):
    first = arrive(svc, "C1", P_GOOD2)["conclusion"]
    again = arrive(svc, "C1", P_GOOD2)
    assert again["mode"] == "idempotent_retry"
    assert again["conclusion"]["conclusion_id"] == first["conclusion_id"]
    batch = svc.batch_view("HT1")
    assert batch["summary"]["locked_payables"] == 1
    assert D(batch["summary"]["payable_total"]) == D(first["amount"])
    # 事件流里只有一次到车计价，没有第二次。
    types = [e["event_type"] for e in svc.ledger_view()["events"]]
    assert types.count("TRUCK_ARRIVE") == 1


def test_new_assay_on_locked_truck_is_rejected_not_silently_rewritten(svc):
    first = arrive(svc, "C1", P_GOOD2)["conclusion"]
    with pytest.raises(DomainError) as exc:
        arrive(svc, "C1", P_GOOD3)
    assert exc.value.code == "PAYABLE_LOCKED" and exc.value.status == 409
    stored = svc.store.latest_conclusion("C1")
    assert stored["amount"] == first["amount"]
    assert stored["conclusion_id"] == first["conclusion_id"]


def test_first_truck_opens_grade_then_invalid_and_keep_events_are_distinct(svc):
    first = arrive(svc, "C1", P_GOOD2)
    assert [h["event_type"] for h in first["bin"]["mix_history"]] == ["BIN_MIX_OPEN"]
    assert first["bin"]["grade_version"] == 1
    second = arrive(svc, "C2", P_GOOD3)
    assert [h["event_type"] for h in second["bin"]["mix_history"]] == [
        "BIN_MIX_OPEN", "BIN_MIX_INVALID"]
    assert second["bin"]["grade_version"] == 2
    # 再来一辆同等级车：等级维持，事件为 BIN_MIX，版本仍递增。
    third = arrive(svc, "C3", P_GOOD3)
    assert [h["event_type"] for h in third["bin"]["mix_history"]] == [
        "BIN_MIX_OPEN", "BIN_MIX_INVALID", "BIN_MIX"]
    assert third["bin"]["grade_version"] == 3


def test_later_truck_can_invalidate_bin_grade_and_history_records_it(svc):
    arrive(svc, "C1", P_GOOD2)
    bin_after_first = svc.bin_view("仓A")
    assert bin_after_first["grade"] == "二等"
    out = arrive(svc, "C2", P_GOOD3)
    # 后到三等车把整仓拉低到三等；原仓批定等失效并留痕。
    assert out["bin"]["grade"] == "三等"
    history = out["bin"]["mix_history"]
    invalid = [h for h in history if h["event_type"] == "BIN_MIX_INVALID"]
    assert invalid and invalid[-1]["payload"]["old_grade"] == "二等"
    assert invalid[-1]["payload"]["new_grade"] == "三等"
    assert out["bin"]["grade_version"] == 2
    # 第一车的应付没有被改写。
    assert D(svc.store.latest_conclusion("C1")["amount"]) > 0


def test_pending_recheck_truck_recheck_pass_supersedes_and_enters_bin(svc):
    bad = arrive(svc, "C9", P_BAD)["conclusion"]
    assert bad["status"] == "pending_recheck"
    assert svc.pending_view()["count"] == 1
    out = svc.recheck({"contract_id": "HT1", "truck_id": "C9", "bin_id": "仓A",
                       "net_kg": "1000", "points": P_GOOD2})
    new = out["conclusion"]
    assert new["status"] == "accepted" and new["version"] == 2
    assert new["supersedes"] == bad["conclusion_id"]
    # 旧结论仍可查，状态链完整；新结论才占仓。
    rows = svc.store.list_trucks("HT1")
    versions = {r["version"]: r for r in rows if r["truck_id"] == "C9"}
    assert versions[1]["status"] == "pending_recheck"
    assert svc.pending_view()["count"] == 0
    assert D(out["bin"]["remaining_kg"]) == D("4000")


def test_recheck_with_same_assay_is_idempotent_no_new_version(svc):
    arrive(svc, "C9", P_BAD)
    again = svc.recheck({"contract_id": "HT1", "truck_id": "C9", "bin_id": "仓A",
                         "net_kg": "1000", "points": P_BAD})
    assert again["mode"] == "idempotent_retry"
    assert svc.store.latest_conclusion("C9")["version"] == 1


def test_recheck_reject_closes_truck_and_keeps_it_out_of_bin(svc):
    arrive(svc, "C9", P_BAD)
    out = svc.recheck({"contract_id": "HT1", "truck_id": "C9", "bin_id": "仓A",
                       "net_kg": "1000",
                       "points": [{"moisture": "19.2", "impurity": "2"}]})
    assert out["conclusion"]["status"] == "rejected"
    # 已结案车再提交化验被拒绝。
    with pytest.raises(DomainError) as exc:
        svc.recheck({"contract_id": "HT1", "truck_id": "C9", "bin_id": "仓A",
                     "net_kg": "1000", "points": P_GOOD2})
    assert exc.value.code == "TRUCK_CLOSED"
    assert svc.bin_view("仓A")["truck_count"] == 0


def test_recheck_cannot_change_bin_or_net_weight(svc):
    arrive(svc, "C9", P_BAD)
    with pytest.raises(DomainError) as exc:
        svc.recheck({"contract_id": "HT1", "truck_id": "C9", "bin_id": "仓A",
                     "net_kg": "1200", "points": P_GOOD2})
    assert exc.value.code == "RECHECK_SCOPE"


def test_cross_grain_bin_is_refused(svc):
    svc.register_bin({"bin_id": "仓B", "grain": "玉米", "capacity_kg": "9000"})
    with pytest.raises(DomainError) as exc:
        svc.arrive({"contract_id": "HT1", "truck_id": "C1", "bin_id": "仓B",
                    "net_kg": "1000", "points": P_GOOD2})
    assert exc.value.code == "GRAIN_MISMATCH"


def test_bin_capacity_is_enforced_with_remaining_visibility(svc):
    arrive(svc, "C1", P_GOOD2, net="4500")
    with pytest.raises(DomainError) as exc:
        arrive(svc, "C2", P_GOOD2, net="1000")
    assert exc.value.code == "BIN_FULL"
    # 待复验车不占仓，不消耗余量。
    arrive(svc, "C3", P_BAD, net="1000")
    assert D(svc.bin_view("仓A")["remaining_kg"]) == D("500")


def test_batch_view_totals_only_accepted_and_keeps_rounding_tail(svc):
    arrive(svc, "C1", P_GOOD2)
    arrive(svc, "C2", P_GOOD3)
    arrive(svc, "C9", P_BAD)
    view = svc.batch_view("HT1")
    assert view["summary"]["truck_total"] == 3
    assert view["summary"]["accepted"] == 2
    assert view["pending_recheck"] == ["C9"]
    amounts = [D(c["amount"]) for c in view["truck_conclusions"] if c["status"] == "accepted"]
    assert D(view["summary"]["payable_total"]) == sum(amounts, D(0))
    assert D(view["summary"]["payable_total"]) != D(view["summary"]["payable_unrounded"])
    assert len(view["bins"]) == 1


def test_recheck_pass_respects_bin_capacity(svc):
    # 先占满 5000kg 容量（5 车二等）。
    for i in range(5):
        arrive(svc, f"L{i}", P_GOOD2)
    arrive(svc, "C9", P_BAD)
    with pytest.raises(DomainError) as exc:
        svc.recheck({"contract_id": "HT1", "truck_id": "C9", "bin_id": "仓A",
                     "net_kg": "1000", "points": P_GOOD2})
    assert exc.value.code == "BIN_FULL"
    # 复验失败不留半条结论，车仍在待复验队列。
    assert svc.store.latest_conclusion("C9")["status"] == "pending_recheck"
    assert svc.pending_view()["count"] == 1


def test_contract_reopening_is_idempotent_and_band_validation(svc):
    again = svc.open_contract(CONTRACT)
    assert again["created"] is False
    bad = {**CONTRACT, "max_moisture": "10"}
    with pytest.raises(DomainError):
        svc.open_contract(bad)


def test_conclusion_fingerprint_changes_when_assay_changes(svc):
    first = arrive(svc, "C9", P_BAD)["conclusion"]
    new = svc.recheck({"contract_id": "HT1", "truck_id": "C9", "bin_id": "仓A",
                       "net_kg": "1000", "points": P_GOOD2})["conclusion"]
    assert first["assay_fingerprint"] != new["assay_fingerprint"]
    assert first["conclusion_id"] != new["conclusion_id"]
