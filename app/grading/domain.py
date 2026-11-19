"""定等应付领域：一份化验结论同时给出等级、折标量与应付。

设计原则
- 单车结论（truck conclusion）是最小不可拆分的可复核凭证，等级、折标量、应付与
  判定依据、指纹全部落在同一条记录里。
- 金额/重量全程 Decimal，舍入只在每车应付末端发生（HALF_UP 到分），未舍入值保留。
- 最差点定等与整车综合是两套并列口径，由契约在开秤前选定，结论里同时保存采信值
  与全部抽样点，仓里与农户的分歧因此可逐条复核。
- 已开应付不可改写：接受入仓的车锁单；同一份化验重试靠指纹幂等、不重复计价；
  待复验车复检会作废旧结论并生成新结论，留痕版本链。
本模块不做跨粮种混仓、税费与付款。
"""
from __future__ import annotations
from dataclasses import dataclass, field
from decimal import Decimal as D, ROUND_HALF_UP
from typing import Any
import hashlib, json
from app.errors import DomainError
from app.normalize import number

CENT = D("0.01")
GRADE_BASIS = ("worst_point", "composite")
# 复检后接受入仓、复检后仍退收、初次退收、初次接受
STATUS = ("accepted", "rejected", "pending_recheck", "superseded")


def _q(value: D) -> D:
    return value.quantize(D("0.000001"))


def assay_fingerprint(points: list[dict[str, Any]]) -> str:
    """同一份化验重试的判定指纹：抽样点序列规范化后取 SHA256。

    Decimal 归一化保证 "14" 与 "14.0"、键序不同都得到同一指纹，
    而抽样点顺序不同视为不同化验，不会被误判成重试。
    """
    canon = [
        {"moisture": format(D(str(p["moisture"])).normalize(), "f"),
         "impurity": format(D(str(p["impurity"])).normalize(), "f")}
        for p in points
    ]
    blob = json.dumps(canon, ensure_ascii=False, sort_keys=True)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


def contract_fingerprint(payload: dict[str, Any]) -> str:
    blob = json.dumps(payload, ensure_ascii=False, sort_keys=True, default=str)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


def conclusion_fingerprint(payload: dict[str, Any]) -> str:
    blob = json.dumps(payload, ensure_ascii=False, sort_keys=True, default=str)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()[:20]


@dataclass(frozen=True)
class Band:
    grade: str
    max_moisture: D
    max_impurity: D
    price: D


@dataclass(frozen=True)
class Contract:
    contract_id: str
    grain: str
    standard_moisture: D
    basis: str
    max_moisture: D          # 整车/单点水分收购上限，超过即退收
    max_impurity: D          # 整车/单点杂质收购上限
    bands: tuple[Band, ...]
    fingerprint: str

    def price_of(self, grade: str) -> D:
        for band in self.bands:
            if band.grade == grade:
                return band.price
        raise DomainError("GRADE", f"等级 {grade} 未在契约中定价")

    def grade_of(self, moisture: D, impurity: D) -> str | None:
        """按等内顺序取第一个同时满足水杂上限的等级；都不满足返回 None（退收）。"""
        for band in self.bands:
            if moisture <= band.max_moisture and impurity <= band.max_impurity:
                return band.grade
        return None


def build_contract(data: dict[str, Any]) -> Contract:
    contract_id = str(data.get("contract_id", "")).strip()
    grain = str(data.get("grain", "")).strip()
    if not contract_id:
        raise DomainError("MISSING", "缺少合同编号")
    if not grain:
        raise DomainError("MISSING", "缺少粮种")
    std = number(data.get("standard_moisture"))
    max_moisture = number(data.get("max_moisture"))
    max_impurity = number(data.get("max_impurity"))
    basis = str(data.get("basis", "worst_point")).strip()
    if basis not in GRADE_BASIS:
        raise DomainError("BASIS", "定等口径必须是 worst_point 或 composite")
    if not 0 <= std < 100 or not 0 <= max_moisture < 100 or not 0 <= max_impurity < 100:
        raise DomainError("RANGE", "水杂参数必须落在 [0,100)")
    if max_moisture <= std:
        raise DomainError("RANGE", "收购上限必须高于标准水分")
    raw_bands = data.get("bands")
    if not isinstance(raw_bands, list) or not raw_bands:
        raise DomainError("MISSING", "至少定义一个等级带")
    bands: list[Band] = []
    seen = set()
    for item in raw_bands:
        grade = str(item.get("grade", "")).strip()
        if not grade:
            raise DomainError("MISSING", "等级名称为空")
        if grade in seen:
            raise DomainError("BAND", f"等级 {grade} 重复")
        seen.add(grade)
        bm = number(item.get("max_moisture"))
        bi = number(item.get("max_impurity"))
        price = number(item.get("price"))
        if not 0 <= bm < 100 or not 0 <= bi < 100:
            raise DomainError("RANGE", "等级水杂阈值越界")
        if price < 0:
            raise DomainError("RANGE", "等级单价不能为负")
        bands.append(Band(grade, bm, bi, price))
    # 等内顺序即优先级：阈值更严的等级必须排在前面，杜绝同一指标命中两个等级。
    for prev, nxt in zip(bands, bands[1:]):
        if not (prev.max_moisture <= nxt.max_moisture and prev.max_impurity <= nxt.max_impurity):
            raise DomainError("BAND", "等级带必须按由严到宽排序，不得交叉倒挂")
        if prev.max_moisture == nxt.max_moisture and prev.max_impurity == nxt.max_impurity:
            raise DomainError("BAND", "相邻等级阈值完全相同，无法区分")
    fingerprint = contract_fingerprint(
        {"contract_id": contract_id, "grain": grain, "standard_moisture": str(std),
         "basis": basis, "max_moisture": str(max_moisture),
         "max_impurity": str(max_impurity),
         "bands": [{"grade": b.grade, "max_moisture": str(b.max_moisture),
                    "max_impurity": str(b.max_impurity), "price": str(b.price)} for b in bands]}
    )
    return Contract(contract_id, grain, std, basis, max_moisture, max_impurity,
                    tuple(bands), fingerprint)


def parse_points(value: Any) -> list[dict[str, D]]:
    if not isinstance(value, list) or not value:
        raise DomainError("SAMPLE", "至少需要一个抽样点")
    points: list[dict[str, D]] = []
    for index, item in enumerate(value, 1):
        if not isinstance(item, dict):
            raise DomainError("SAMPLE", f"抽样点 {index} 结构错误")
        moisture = number(item.get("moisture"))
        impurity = number(item.get("impurity"))
        if not 0 <= moisture < 100 or not 0 <= impurity < 100:
            raise DomainError("RANGE", f"抽样点 {index} 水杂越界")
        points.append({"moisture": moisture, "impurity": impurity})
    return points


def decide_values(contract: Contract, points: list[dict[str, D]]) -> dict[str, Any]:
    """按契约口径从抽样点得到用于定等的采信水杂与逐点依据。"""
    if contract.basis == "worst_point":
        moisture = max(p["moisture"] for p in points)
        impurity = max(p["impurity"] for p in points)
        basis_detail = "逐点取最高水分与最高杂质（最差点口径）"
    else:
        total = len(points)
        moisture = sum((p["moisture"] for p in points), D(0)) / D(total)
        impurity = sum((p["impurity"] for p in points), D(0)) / D(total)
        basis_detail = f"{total} 个抽样点算术平均（整车综合口径）"
    return {"moisture": _q(moisture), "impurity": _q(impurity), "basis_detail": basis_detail}


def standard_weight(net_kg: D, moisture: D, impurity: D, standard_moisture: D) -> dict[str, D]:
    """杂质先扣，再按干物质守恒折标：净粮→干物质→折标重。"""
    clean_kg = net_kg * (1 - impurity / D(100))
    dry_kg = clean_kg * (1 - moisture / D(100))
    standard_kg = dry_kg / (1 - standard_moisture / D(100))
    return {"clean_kg": _q(clean_kg), "dry_kg": _q(dry_kg), "standard_kg": _q(standard_kg)}


@dataclass
class Conclusion:
    truck_id: str
    contract_id: str
    grain: str
    bin_id: str
    net_kg: D
    basis: str
    points: list[dict[str, D]]
    assay_fingerprint: str
    decided_moisture: D
    decided_impurity: D
    basis_detail: str
    grade: str | None
    price: D | None
    clean_kg: D
    dry_kg: D
    standard_kg: D
    amount: D
    unrounded_amount: D
    status: str
    reject_code: str | None
    reject_reason: str | None
    version: int = 1
    supersedes: str | None = None
    conclusion_id: str = ""
    created: str = ""

    def fingerprint_payload(self) -> dict[str, Any]:
        return {
            "truck_id": self.truck_id, "contract_id": self.contract_id, "bin_id": self.bin_id,
            "net_kg": str(self.net_kg), "basis": self.basis,
            "assay_fingerprint": self.assay_fingerprint,
            "moisture": str(self.decided_moisture), "impurity": str(self.decided_impurity),
            "grade": self.grade, "price": None if self.price is None else str(self.price),
            "standard_kg": str(self.standard_kg), "amount": str(self.amount),
            "status": self.status, "version": self.version, "supersedes": self.supersedes,
        }


def evaluate_truck(contract: Contract, truck_id: str, bin_id: str, net_kg: D,
                   points: list[dict[str, D]], assay_fp: str, *,
                   version: int = 1, supersedes: str | None = None,
                   prior_status: str | None = None) -> Conclusion:
    """同一份输入产出唯一的车结论：等级、折标量、应付来自同一次判定。"""
    if not truck_id or not bin_id:
        raise DomainError("MISSING", "车号与仓号不能为空")
    if net_kg <= 0:
        raise DomainError("RANGE", "净重必须为正数")
    decision = decide_values(contract, points)
    moisture, impurity = decision["moisture"], decision["impurity"]
    weights = standard_weight(net_kg, moisture, impurity, contract.standard_moisture)
    over = moisture > contract.max_moisture or impurity > contract.max_impurity
    grade = None if over else contract.grade_of(moisture, impurity)
    reject_code = reject_reason = None
    price = None
    amount = D("0.00")
    unrounded = D("0")
    if over:
        # 超收购上限优先于“等外”：先告诉结算员是哪条硬线被突破。
        reject_code = "LIMIT"
        if moisture > contract.max_moisture and impurity > contract.max_impurity:
            reject_reason = "采信水分与杂质均超收购上限"
        elif moisture > contract.max_moisture:
            reject_reason = f"采信水分 {moisture}% 超过上限 {contract.max_moisture}%"
        else:
            reject_reason = f"采信杂质 {impurity}% 超过上限 {contract.max_impurity}%"
        status = "pending_recheck" if prior_status is None else "rejected"
    elif grade is None:
        reject_code = "GRADE"
        reject_reason = ("水杂未超收购上限，但未落入任何等内等级带；"
                         "请核对契约等级带设置或安排复验")
        status = "pending_recheck" if prior_status is None else "rejected"
    else:
        price = contract.price_of(grade)
        unrounded = weights["standard_kg"] * price
        # 未舍入值保留全精度，舍入只发生在应付末端（到分，HALF_UP）。
        amount = unrounded.quantize(CENT, rounding=ROUND_HALF_UP)
        status = "accepted"
    conclusion = Conclusion(
        truck_id=truck_id, contract_id=contract.contract_id, grain=contract.grain,
        bin_id=bin_id, net_kg=net_kg, basis=contract.basis, points=points,
        assay_fingerprint=assay_fp, decided_moisture=moisture, decided_impurity=impurity,
        basis_detail=decision["basis_detail"], grade=grade, price=price,
        clean_kg=weights["clean_kg"], dry_kg=weights["dry_kg"],
        standard_kg=weights["standard_kg"], amount=amount, unrounded_amount=_q(unrounded),
        status=status, reject_code=reject_code, reject_reason=reject_reason,
        version=version, supersedes=supersedes,
    )
    conclusion.unrounded_amount = unrounded
    conclusion.conclusion_id = conclusion_fingerprint(conclusion.fingerprint_payload())
    return conclusion


def mix_bin(bin_id: str, grain: str, capacity_kg: D | None,
            trucks: list[dict[str, Any]]) -> dict[str, Any]:
    """仓批按净粮量加权得到混仓水杂，再用同一契约口径重算仓批等级。

    trucks 元素需含 net_kg/clean_kg/moisture/impurity/grade/contract 指纹字段。
    仓批余量 = 容量 - 在仓净重；无容量登记时余量留空。
    """
    net = sum((D(str(t["net_kg"])) for t in trucks), D(0))
    clean = sum((D(str(t["clean_kg"])) for t in trucks), D(0))
    standard = sum((D(str(t["standard_kg"])) for t in trucks), D(0))
    if not trucks:
        mixed_moisture = mixed_impurity = D("0")
        grade, worst_grade = None, None
    else:
        mixed_moisture = sum(
            (D(str(t["moisture"])) * D(str(t["net_kg"])) for t in trucks), D(0)) / net
        mixed_impurity = sum(
            (D(str(t["impurity"])) * D(str(t["net_kg"])) for t in trucks), D(0)) / net
        mixed_moisture, mixed_impurity = _q(mixed_moisture), _q(mixed_impurity)
        contract: Contract = trucks[0]["_contract"]
        grade = contract.grade_of(mixed_moisture, mixed_impurity)
        if mixed_moisture > contract.max_moisture or mixed_impurity > contract.max_impurity:
            grade = None
        worst_grade = _worst_grade(contract, [t["grade"] for t in trucks])
    remaining = None if capacity_kg is None else _q(capacity_kg - net)
    return {
        "bin_id": bin_id, "grain": grain, "truck_count": len(trucks),
        "net_kg": _q(net), "clean_kg": _q(clean), "standard_kg": _q(standard),
        "moisture": mixed_moisture, "impurity": mixed_impurity,
        "grade": grade, "worst_truck_grade": worst_grade,
        "capacity_kg": None if capacity_kg is None else _q(capacity_kg),
        "remaining_kg": remaining,
    }


def _worst_grade(contract: Contract, grades: list[str | None]) -> str | None:
    """等级带越靠后越差；None（等外）视为最差。"""
    order = {band.grade: index for index, band in enumerate(contract.bands)}
    result: str | None = None
    result_rank = -1
    for grade in grades:
        if grade is None:
            return None
        if order[grade] > result_rank:
            result, result_rank = grade, order[grade]
    return result
