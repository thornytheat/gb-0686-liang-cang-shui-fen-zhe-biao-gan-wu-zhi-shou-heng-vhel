"""定等应付 HTTP 接口：契约、仓、到车/复验、批次与仓批查询、事件流水。"""
from flask import Blueprint, request, jsonify, render_template
from app.errors import DomainError
from app.grading.service import GradingService

bp = Blueprint("grading", __name__)
service = GradingService()


def _body() -> dict:
    data = request.get_json(silent=True)
    if not isinstance(data, dict):
        raise DomainError("BODY", "请求必须是对象")
    return data


@bp.get("/grading")
def page():
    return render_template("grading.html")


@bp.post("/api/grading/contracts")
def open_contract():
    return jsonify(service.open_contract(_body()))


@bp.get("/api/grading/contracts/<contract_id>")
def get_contract(contract_id):
    return jsonify({"contract": service.contract_view(contract_id)})


@bp.post("/api/grading/bins")
def register_bin():
    return jsonify(service.register_bin(_body()))


@bp.get("/api/grading/bins")
def list_bins():
    return jsonify({"bins": service.store.list_bins()})


@bp.get("/api/grading/bins/<bin_id>")
def get_bin(bin_id):
    return jsonify({"bin": service.bin_view(bin_id)})


@bp.post("/api/grading/trucks/arrive")
def arrive():
    return jsonify(service.arrive(_body()))


@bp.post("/api/grading/trucks/recheck")
def recheck():
    return jsonify(service.recheck(_body()))


@bp.get("/api/grading/batches/<contract_id>")
def batch(contract_id):
    return jsonify(service.batch_view(contract_id))


@bp.get("/api/grading/pending")
def pending():
    return jsonify(service.pending_view())


@bp.get("/api/grading/ledger")
def ledger():
    return jsonify(service.ledger_view())
