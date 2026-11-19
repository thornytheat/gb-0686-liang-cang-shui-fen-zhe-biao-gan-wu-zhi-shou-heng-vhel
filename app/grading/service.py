"""定等应付应用服务：把车化验编排成不可改写的结论与仓批演化。"""
from __future__ import annotations
from decimal import Decimal as D
from app.errors import DomainError
from app.normalize import number, serial
from app.grading import domain
from app.grading.domain import (
    build_contract, parse_points, assay_fingerprint, evaluate_truck, mix_bin,
)
from app.grading.store import GradingStore

STATUS_TEXT = {
    "accepted": "已入仓锁单",
    "rejected": "退收",
    "pending_recheck": "待复验",
    "superseded": "已被新版本作废",
}


class GradingService:
    def __init__(self, store: GradingStore | None = None):
        self.store = store or GradingStore()

    # ---------- 契约与仓 ----------
    def open_contract(self, data: dict) -> dict:
        contract = build_contract(data)
        created = self.store.save_contract(contract)
        return {"created": created, "contract": self.contract_view(contract.contract_id)}

    def contract_view(self, contract_id: str) -> dict:
        row = self.store.get_contract_row(contract_id)
        return serial(row)

    def register_bin(self, data: dict) -> dict:
        bin_id = str(data.get("bin_id", "")).strip()
        grain = str(data.get("grain", "")).strip()
        if not bin_id or not grain:
            raise DomainError("MISSING", "仓号与粮种不能为空")
        capacity = data.get("capacity_kg")
        capacity_kg = None if capacity in (None, "") else number(capacity)
        if capacity_kg is not None and capacity_kg <= 0:
            raise DomainError("RANGE", "仓容量必须为正数")
        existing = None
        try:
            existing = self.store.get_bin(bin_id)
        except DomainError as exc:
            if exc.status != 404:
                raise
        if existing is not None:
            if existing["grain"] != grain:
                raise DomainError("GRAIN_MISMATCH", "该仓已登记其他粮种，禁止跨粮种混用")
            return {"created": False, "bin": serial(dict(existing))}
        self.store.save_bin(bin_id, grain, capacity_kg)
        return {"created": True, "bin": serial(dict(self.store.get_bin(bin_id)))}

    def _load_contract(self, contract_id: str):
        return build_contract(self.store.get_contract_row(contract_id))

    # ---------- 到车 ----------
    def _parse_arrival(self, data: dict) -> dict:
        for key in ("contract_id", "truck_id", "bin_id"):
            if not str(data.get(key, "")).strip():
                raise DomainError("MISSING", f"缺少 {key}")
        contract = self._load_contract(str(data["contract_id"]).strip())
        bin_row = self.store.get_bin(str(data["bin_id"]).strip())
        if bin_row["grain"] != contract.grain:
            raise DomainError("GRAIN_MISMATCH",
                              f"仓 {bin_row['bin_id']} 为 {bin_row['grain']}，"
                              f"与合同粮种 {contract.grain} 不一致，禁止跨粮种混仓")
        net_kg = number(data.get("net_kg"))
        points = parse_points(data.get("points"))
        return {"contract": contract, "bin_row": bin_row, "net_kg": net_kg,
                "points": points, "truck_id": str(data["truck_id"]).strip(),
                "bin_id": str(data["bin_id"]).strip(),
                "assay_fp": assay_fingerprint(points)}

    def arrive(self, data: dict) -> dict:
        parsed = self._parse_arrival(data)
        contract, bin_row = parsed["contract"], parsed["bin_row"]
        prior = self.store.latest_conclusion(parsed["truck_id"])
        if prior is not None:
            # 同一份化验重试：原样返回既有结论，绝不重复计价。
            if prior["assay_fingerprint"] == parsed["assay_fp"]:
                return {"mode": "idempotent_retry", "conclusion": serial(prior),
                        "bin": self.bin_view(prior["bin_id"])}
            if prior["status"] == "accepted":
                # 已开应付不得因新化验悄悄改写。
                raise DomainError(
                    "PAYABLE_LOCKED",
                    f"车 {parsed['truck_id']} 已开应付 {prior['amount']} 元并锁单，"
                    "新化验不能改写原结论；如确需更正须走红冲调整流程", 409)
            if prior["status"] == "rejected":
                raise DomainError("TRUCK_CLOSED", "该车已退收结案，不能再提交化验", 409)
            if prior["status"] == "pending_recheck":
                raise DomainError("NEED_RECHECK",
                                  "该车正在待复验队列，请调用复验接口提交新化验", 409)
        # 先判定：待复验/退收车不占仓，只有接受入仓的车才受容量约束。
        conclusion = evaluate_truck(
            contract, parsed["truck_id"], parsed["bin_id"], parsed["net_kg"],
            parsed["points"], parsed["assay_fp"])
        if conclusion.status == "accepted":
            self._check_capacity(bin_row, parsed["net_kg"], parsed["bin_id"])
        saved = self.store.append_conclusion(
            conclusion, "TRUCK_ARRIVE",
            {"truck_id": conclusion.truck_id, "assay_fingerprint": conclusion.assay_fingerprint,
             "status": conclusion.status, "grade": conclusion.grade,
             "amount": conclusion.amount})
        bin_view = self._after_conclusion(saved, contract)
        return {"mode": "arrival", "conclusion": serial(saved), "bin": bin_view}

    def recheck(self, data: dict) -> dict:
        parsed = self._parse_arrival(data)
        contract = parsed["contract"]
        prior = self.store.latest_conclusion(parsed["truck_id"])
        if prior is None:
            raise DomainError("NOT_FOUND", "该车没有初次化验结论，无法复验", 404)
        if prior["status"] == "accepted":
            raise DomainError("PAYABLE_LOCKED",
                              "该车已入仓锁单，复验只能对待退收车辆发起", 409)
        if prior["status"] == "rejected":
            raise DomainError("TRUCK_CLOSED", "该车已退收结案", 409)
        if prior["assay_fingerprint"] == parsed["assay_fp"]:
            # 同一份化验重复点复验：幂等返回，不新增版本、不重复计价。
            return {"mode": "idempotent_retry", "conclusion": serial(prior),
                    "bin": self.bin_view(prior["bin_id"])}
        if parsed["bin_id"] != prior["bin_id"] or str(parsed["net_kg"]) != str(prior["net_kg"]):
            raise DomainError("RECHECK_SCOPE", "复验只能更换化验，不得改仓或改净重")
        bin_row = parsed["bin_row"]
        conclusion = evaluate_truck(
            contract, parsed["truck_id"], parsed["bin_id"], parsed["net_kg"],
            parsed["points"], parsed["assay_fp"],
            version=prior["version"] + 1, supersedes=prior["conclusion_id"],
            prior_status=prior["status"])
        # 复验通过才会真正入仓，入仓前同样要过容量闸；失败则不留半条记录。
        if conclusion.status == "accepted":
            self._check_capacity(bin_row, parsed["net_kg"], parsed["bin_id"])
        # 旧结论置为 superseded：通过追加作废事件留痕，原记录本身不更新不删除。
        self.store.record_event(
            "CONCLUSION_SUPERSEDE",
            {"truck_id": conclusion.truck_id,
             "old_conclusion_id": prior["conclusion_id"],
             "new_conclusion_id": conclusion.conclusion_id,
             "old_status": prior["status"], "new_status": conclusion.status},
            truck_id=conclusion.truck_id, bin_id=conclusion.bin_id)
        saved = self.store.append_conclusion(
            conclusion,
            "TRUCK_RECHECK_PASS" if conclusion.status == "accepted" else "TRUCK_RECHECK_REJECT",
            {"truck_id": conclusion.truck_id,
             "assay_fingerprint": conclusion.assay_fingerprint,
             "supersedes": conclusion.supersedes, "status": conclusion.status,
             "grade": conclusion.grade, "amount": conclusion.amount})
        bin_view = self._after_conclusion(saved, contract)
        return {"mode": "recheck", "conclusion": serial(saved), "bin": bin_view}

    def _check_capacity(self, bin_row, net_kg: D, bin_id: str) -> None:
        if bin_row["capacity_kg"] is None:
            return
        capacity = D(bin_row["capacity_kg"])
        occupied = sum((D(str(t["net_kg"])) for t in self.store.active_trucks_in_bin(bin_id)), D(0))
        if occupied + net_kg > capacity:
            raise DomainError("BIN_FULL",
                              f"仓 {bin_id} 余量 {capacity - occupied}kg，装不下本车 {net_kg}kg")

    def _after_conclusion(self, saved: dict, contract) -> dict:
        """接受入仓才参与混仓；混仓后重算仓批等级并记录失效/生效版本。"""
        if saved["status"] != "accepted":
            return self.bin_view(saved["bin_id"])
        bin_row = self.store.get_bin(saved["bin_id"])
        # 先在“不含本车”的状态下取旧快照：不读事件流，避免 grade_version 自污染。
        prior_trucks = [t for t in self.store.active_trucks_in_bin(saved["bin_id"])
                        if t["truck_id"] != saved["truck_id"]]
        capacity = None if bin_row["capacity_kg"] is None else D(bin_row["capacity_kg"])
        before = mix_bin(saved["bin_id"], bin_row["grain"], capacity,
                         [{**t, "_contract": contract} for t in prior_trucks])
        after = self._recompute_bin(saved["bin_id"], contract, trigger=saved["truck_id"])
        is_first_mix = not prior_trucks
        invalidated = (not is_first_mix) and before["grade"] != after["grade"]
        event_type = "BIN_MIX_OPEN" if is_first_mix else (
            "BIN_MIX_INVALID" if invalidated else "BIN_MIX")
        self.store.record_event(
            event_type,
            {"bin_id": after["bin_id"], "truck_id": saved["truck_id"],
             "old_grade": before["grade"], "new_grade": after["grade"],
             "old_remaining_kg": before["remaining_kg"],
             "new_remaining_kg": after["remaining_kg"],
             "bin_grade_version": after["grade_version"],
             "note": "首车建立仓批定等" if is_first_mix else (
                     "后到车次拉低仓批等级，原仓批定等失效"
                     if invalidated else "仓批等级维持")},
            truck_id=saved["truck_id"], bin_id=after["bin_id"])
        # 返回完整仓视图（含混仓历史），保证页面能看清每一次余量与等级变化。
        return self.bin_view(saved["bin_id"])

    def _recompute_bin(self, bin_id: str, contract, trigger: str | None = None) -> dict:
        bin_row = self.store.get_bin(bin_id)
        trucks = self.store.active_trucks_in_bin(bin_id)
        attached = [{**t, "_contract": contract} for t in trucks]
        capacity = None if bin_row["capacity_kg"] is None else D(bin_row["capacity_kg"])
        snapshot = mix_bin(bin_id, bin_row["grain"], capacity, attached)
        # 版本号即该仓已落库的混仓事件数：本车事件尚未写入，此刻正是它的版本号。
        snapshot["grade_version"] = len(self.store.mix_history(bin_id))
        snapshot["trigger_truck_id"] = trigger
        snapshot["truck_ids"] = [t["truck_id"] for t in trucks]
        return serial(snapshot)

    # ---------- 查询 ----------
    def bin_view(self, bin_id: str) -> dict:
        bin_row = self.store.get_bin(bin_id)
        contract_ids = {r["contract_id"] for r in self.store.list_trucks()
                        if r["bin_id"] == bin_id}
        if len(contract_ids) > 1:
            raise DomainError("GRAIN_MISMATCH", "仓内出现多个合同，数据异常")
        contract = self._load_contract(next(iter(contract_ids))) if contract_ids else None
        if contract is None:
            capacity = None if bin_row["capacity_kg"] is None else D(bin_row["capacity_kg"])
            snapshot = mix_bin(bin_id, bin_row["grain"], capacity, [])
            snapshot["grade_version"] = 0
            snapshot["truck_ids"] = []
            return serial(snapshot)
        view = self._recompute_bin(bin_id, contract, trigger=None)
        view["mix_history"] = self.store.mix_history(bin_id)
        return view

    def batch_view(self, contract_id: str) -> dict:
        self.get_contract_or_404(contract_id)
        trucks = [t for t in self.store.list_trucks(contract_id)]
        latest = [t for t in trucks if t["version"] == max(
            x["version"] for x in trucks if x["truck_id"] == t["truck_id"])]
        accepted = [t for t in latest if t["status"] == "accepted"]
        total_amount = sum((D(str(t["amount"])) for t in accepted), D(0))
        total_unrounded = sum((D(str(t["unrounded_amount"])) for t in accepted), D(0))
        total_standard = sum((D(str(t["standard_kg"])) for t in accepted), D(0))
        bins = sorted({t["bin_id"] for t in latest})
        return serial({
            "contract_id": contract_id,
            "truck_conclusions": trucks,
            "bins": [self.bin_view(b) for b in bins],
            "pending_recheck": [t["truck_id"] for t in latest
                                if t["status"] == "pending_recheck"],
            "summary": {
                "truck_total": len({t["truck_id"] for t in trucks}),
                "accepted": len(accepted),
                "locked_payables": len(accepted),
                "standard_kg": total_standard,
                "payable_total": total_amount,
                "payable_unrounded": total_unrounded,
                "rounding_tail": total_amount - total_unrounded,
            },
        })

    def get_contract_or_404(self, contract_id: str) -> dict:
        return self.store.get_contract_row(contract_id)

    def pending_view(self) -> dict:
        rows = self.store.pending_recheck()
        return {"count": len(rows), "trucks": serial(rows)}

    def ledger_view(self) -> dict:
        return {"events": self.store.events()}
