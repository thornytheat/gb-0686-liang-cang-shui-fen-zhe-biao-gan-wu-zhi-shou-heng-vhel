import pytest
from decimal import Decimal as D
from app.errors import DomainError
from app.grading import domain
from app.grading.domain import (
    build_contract, parse_points, assay_fingerprint, evaluate_truck, mix_bin,
    standard_weight,
)

BANDS = [
    {"grade": "一等", "max_moisture": "13.5", "max_impurity": "1.0", "price": "2.80"},
    {"grade": "二等", "max_moisture": "15.0", "max_impurity": "2.0", "price": "2.60"},
    {"grade": "三等", "max_moisture": "16.5", "max_impurity": "4.0", "price": "2.40"},
]
CONTRACT = dict(contract_id="HT1", grain="小麦", standard_moisture="12.5",
                basis="worst_point", max_moisture="18", max_impurity="6", bands=BANDS)


def contract(basis="worst_point", **over):
    return build_contract({**CONTRACT, "basis": basis, **over})


def pts(*pairs):
    return [{"moisture": D(str(m)), "impurity": D(str(i))} for m, i in pairs]


def test_worst_point_uses_highest_of_each_attribute():
    c = contract()
    points = pts((14.8, 1.2), (16.9, 3.4), (14.2, 0.9))
    out = domain.decide_values(c, points)
    assert out["moisture"] == D("16.900000")
    assert out["impurity"] == D("3.400000")
    # 最差水分 16.9 落不进任何等级带 → 待复验，而不是按平均放水。
    conclusion = evaluate_truck(c, "C1", "仓A", D("1000"), points, assay_fingerprint(points))
    assert conclusion.status == "pending_recheck"
    assert conclusion.grade is None and conclusion.amount == D("0.00")


def test_composite_uses_arithmetic_mean_and_can_grade_same_truck():
    c = contract("composite")
    points = pts((14.8, 1.2), (16.9, 3.4), (14.2, 0.9))
    out = domain.decide_values(c, points)
    assert out["moisture"] == D("15.300000")
    assert out["impurity"] == D("1.833333")
    conclusion = evaluate_truck(c, "C1", "仓A", D("1000"), points, assay_fingerprint(points))
    assert conclusion.status == "accepted"
    assert conclusion.grade == "三等"


def test_grade_grade_amount_share_one_conclusion_and_round_only_at_end():
    c = contract()
    points = pts((14.0, 1.5))
    conclusion = evaluate_truck(c, "C1", "仓A", D("1000"), points, assay_fingerprint(points))
    assert conclusion.grade == "二等"
    # 净粮→干物质→折标，与等级/金额在同一对象内，可一并复核。
    expect = standard_weight(D("1000"), conclusion.decided_moisture,
                             conclusion.decided_impurity, c.standard_moisture)
    assert conclusion.standard_kg == expect["standard_kg"]
    assert conclusion.unrounded_amount == conclusion.standard_kg * D("2.60")
    assert conclusion.amount == conclusion.unrounded_amount.quantize(D("0.01"))
    assert conclusion.conclusion_id


def test_over_limit_moisture_is_pending_recheck_not_accepted():
    c = contract()
    points = pts((18.5, 1))
    conclusion = evaluate_truck(c, "C1", "仓A", D("800"), points, assay_fingerprint(points))
    assert conclusion.status == "pending_recheck"
    assert conclusion.reject_code == "LIMIT"
    assert conclusion.standard_kg > 0 and conclusion.amount == D("0.00")


def test_same_points_give_same_fingerprint_even_if_key_order_or_text_differs():
    a = parse_points([{"moisture": "14", "impurity": "1.5"}])
    b = parse_points([{"impurity": "1.50", "moisture": "14.0"}])
    assert assay_fingerprint(a) == assay_fingerprint(b)


def test_band_order_must_be_strict_nested():
    bad = [BANDS[0], {**BANDS[1], "max_moisture": "13.0"}, BANDS[2]]
    with pytest.raises(DomainError) as exc:
        build_contract({**CONTRACT, "bands": bad})
    assert exc.value.code == "BAND"


def test_duplicate_band_threshold_rejected():
    bad = [BANDS[0], {**BANDS[1], "max_moisture": "13.5", "max_impurity": "1.0"}, BANDS[2]]
    with pytest.raises(DomainError) as exc:
        build_contract({**CONTRACT, "bands": bad})
    assert exc.value.code == "BAND"


def test_mix_bin_weighted_regrade_and_remaining():
    c = contract()
    t1 = {"net_kg": D("1000"), "clean_kg": D("980"), "standard_kg": D("960"),
          "moisture": D("14.0"), "impurity": D("1.5"), "grade": "二等", "_contract": c}
    t2 = {"net_kg": D("1000"), "clean_kg": D("970"), "standard_kg": D("930"),
          "moisture": D("16.2"), "impurity": D("3.5"), "grade": "三等", "_contract": c}
    view = mix_bin("仓A", "小麦", D("60000"), [t1, t2])
    assert view["moisture"] == D("15.100000")
    assert view["impurity"] == D("2.500000")
    assert view["grade"] == "三等"
    assert view["worst_truck_grade"] == "三等"
    assert view["net_kg"] == D("2000")
    assert view["remaining_kg"] == D("58000")
    assert view["standard_kg"] == D("1890")


def test_mix_bin_drops_below_all_bands_returns_none_grade():
    c = contract()
    t = {"net_kg": D("1000"), "clean_kg": D("900"), "standard_kg": D("900"),
         "moisture": D("17.5"), "impurity": D("5"), "grade": "三等", "_contract": c}
    view = mix_bin("仓A", "小麦", None, [t])
    assert view["grade"] is None and view["remaining_kg"] is None
