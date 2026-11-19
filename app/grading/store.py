"""定等应付存储：结论只追加不改写，事件流留痕，全部写操作在一个事务内。"""
from __future__ import annotations
import json, os, sqlite3
from datetime import datetime, timezone
from pathlib import Path
from decimal import Decimal
from app.errors import DomainError

SCHEMA = """
CREATE TABLE IF NOT EXISTS contracts(
  contract_id TEXT PRIMARY KEY,
  grain TEXT NOT NULL,
  payload TEXT NOT NULL,
  fingerprint TEXT NOT NULL,
  created TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS bins(
  bin_id TEXT PRIMARY KEY,
  grain TEXT NOT NULL,
  capacity_kg TEXT,
  created TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS conclusions(
  conclusion_id TEXT NOT NULL,
  truck_id TEXT NOT NULL,
  version INTEGER NOT NULL,
  status TEXT NOT NULL,
  contract_id TEXT NOT NULL,
  bin_id TEXT NOT NULL,
  grain TEXT NOT NULL,
  assay_fingerprint TEXT NOT NULL,
  supersedes TEXT,
  payload TEXT NOT NULL,
  created TEXT NOT NULL,
  PRIMARY KEY(truck_id, version)
);
CREATE TABLE IF NOT EXISTS events(
  seq INTEGER PRIMARY KEY AUTOINCREMENT,
  event_type TEXT NOT NULL,
  truck_id TEXT,
  bin_id TEXT,
  payload TEXT NOT NULL,
  created TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_events_bin ON events(bin_id, seq);
CREATE INDEX IF NOT EXISTS idx_conclusions_bin ON conclusions(bin_id);
"""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _dumps(value) -> str:
    return json.dumps(value, ensure_ascii=False, default=_default)


def _default(obj):
    if isinstance(obj, Decimal):
        return str(obj)
    raise TypeError(f"不可序列化类型 {type(obj)}")


def _loads(text: str):
    return json.loads(text, parse_float=Decimal)


class GradingStore:
    def __init__(self, data_dir: str | None = None):
        path = Path(data_dir or os.environ.get("DATA_DIR", "data"))
        path.mkdir(parents=True, exist_ok=True)
        self.path = path / "grading.sqlite"

    def connect(self) -> sqlite3.Connection:
        db = sqlite3.connect(self.path, timeout=15)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA foreign_keys=ON")
        db.executescript(SCHEMA)
        return db

    # ---------- 契约与仓 ----------
    def save_contract(self, contract) -> bool:
        with self.connect() as db:
            row = db.execute("SELECT 1 FROM contracts WHERE contract_id=?",
                             (contract.contract_id,)).fetchone()
            if row:
                return False
            payload = _dumps({
                "contract_id": contract.contract_id, "grain": contract.grain,
                "standard_moisture": contract.standard_moisture, "basis": contract.basis,
                "max_moisture": contract.max_moisture, "max_impurity": contract.max_impurity,
                "bands": [{"grade": b.grade, "max_moisture": b.max_moisture,
                           "max_impurity": b.max_impurity, "price": b.price}
                          for b in contract.bands],
            })
            db.execute("INSERT INTO contracts VALUES(?,?,?,?,?)",
                       (contract.contract_id, contract.grain, payload,
                        contract.fingerprint, _now()))
            db.execute("INSERT INTO events(event_type,payload,created) VALUES(?,?,?)",
                       ("CONTRACT_OPEN", payload, _now()))
            return True

    def get_contract_row(self, contract_id: str) -> dict:
        with self.connect() as db:
            row = db.execute("SELECT * FROM contracts WHERE contract_id=?",
                             (contract_id,)).fetchone()
        if row is None:
            raise DomainError("NOT_FOUND", f"合同 {contract_id} 不存在", 404)
        data = _loads(row["payload"])
        data["fingerprint"] = row["fingerprint"]
        return data

    def save_bin(self, bin_id: str, grain: str, capacity_kg) -> bool:
        with self.connect() as db:
            row = db.execute("SELECT grain FROM bins WHERE bin_id=?", (bin_id,)).fetchone()
            if row:
                return False
            db.execute("INSERT INTO bins VALUES(?,?,?,?)",
                       (bin_id, grain, None if capacity_kg is None else str(capacity_kg), _now()))
            db.execute("INSERT INTO events(event_type,bin_id,payload,created) VALUES(?,?,?,?)",
                       ("BIN_REGISTER", bin_id,
                        _dumps({"bin_id": bin_id, "grain": grain, "capacity_kg": capacity_kg}),
                        _now()))
            return True

    def get_bin(self, bin_id: str) -> sqlite3.Row:
        with self.connect() as db:
            row = db.execute("SELECT * FROM bins WHERE bin_id=?", (bin_id,)).fetchone()
        if row is None:
            raise DomainError("NOT_FOUND", f"仓 {bin_id} 未登记", 404)
        return row

    def list_bins(self) -> list[dict]:
        with self.connect() as db:
            rows = db.execute("SELECT * FROM bins ORDER BY bin_id").fetchall()
        return [dict(r) for r in rows]

    # ---------- 车结论（追加） ----------
    def latest_conclusion(self, truck_id: str) -> dict | None:
        with self.connect() as db:
            row = db.execute(
                "SELECT * FROM conclusions WHERE truck_id=? ORDER BY version DESC LIMIT 1",
                (truck_id,)).fetchone()
        if row is None:
            return None
        data = _loads(row["payload"])
        data["conclusion_id"] = row["conclusion_id"]
        data["version"] = row["version"]
        data["created"] = row["created"]
        return data

    def append_conclusion(self, conclusion, event_type: str, event_payload: dict) -> dict:
        with self.connect() as db:
            created = _now()
            db.execute(
                "INSERT INTO conclusions(conclusion_id,truck_id,version,status,contract_id,"
                "bin_id,grain,assay_fingerprint,supersedes,payload,created) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                (conclusion.conclusion_id, conclusion.truck_id, conclusion.version,
                 conclusion.status, conclusion.contract_id, conclusion.bin_id,
                 conclusion.grain, conclusion.assay_fingerprint, conclusion.supersedes,
                 _dumps(_conclusion_dict(conclusion)), created))
            db.execute("INSERT INTO events(event_type,truck_id,bin_id,payload,created) "
                       "VALUES(?,?,?,?,?)",
                       (event_type, conclusion.truck_id, conclusion.bin_id,
                        _dumps(event_payload), created))
        saved = self.latest_conclusion(conclusion.truck_id)
        saved["created"] = created
        return saved

    def list_trucks(self, contract_id: str | None = None) -> list[dict]:
        with self.connect() as db:
            if contract_id:
                rows = db.execute(
                    "SELECT * FROM conclusions ORDER BY contract_id, truck_id, version",
                    ).fetchall()
                rows = [r for r in rows if r["contract_id"] == contract_id]
            else:
                rows = db.execute("SELECT * FROM conclusions ORDER BY truck_id, version").fetchall()
        out = []
        for row in rows:
            data = _loads(row["payload"])
            data["conclusion_id"] = row["conclusion_id"]
            data["version"] = row["version"]
            data["created"] = row["created"]
            out.append(data)
        return out

    def active_trucks_in_bin(self, bin_id: str) -> list[dict]:
        """当前真正占仓的车：最新结论为 accepted。待复验/退收不占仓。"""
        with self.connect() as db:
            rows = db.execute(
                "SELECT * FROM conclusions c WHERE bin_id=? AND version=("
                "SELECT MAX(version) FROM conclusions WHERE truck_id=c.truck_id) "
                "AND status='accepted' ORDER BY c.truck_id", (bin_id,)).fetchall()
        return [_loads(r["payload"]) for r in rows]

    # ---------- 事件 / 历史 ----------
    def record_event(self, event_type: str, payload: dict,
                     truck_id: str | None = None, bin_id: str | None = None) -> None:
        with self.connect() as db:
            db.execute("INSERT INTO events(event_type,truck_id,bin_id,payload,created) "
                       "VALUES(?,?,?,?,?)",
                       (event_type, truck_id, bin_id, _dumps(payload), _now()))

    def bin_events(self, bin_id: str) -> list[dict]:
        with self.connect() as db:
            rows = db.execute(
                "SELECT * FROM events WHERE bin_id=? ORDER BY seq", (bin_id,)).fetchall()
        return [{"seq": r["seq"], "event_type": r["event_type"],
                 "created": r["created"], "payload": _loads(r["payload"])} for r in rows]

    def events(self, limit: int = 200) -> list[dict]:
        with self.connect() as db:
            rows = db.execute("SELECT * FROM events ORDER BY seq DESC LIMIT ?", (limit,)).fetchall()
        return [{"seq": r["seq"], "event_type": r["event_type"], "truck_id": r["truck_id"],
                 "bin_id": r["bin_id"], "created": r["created"],
                 "payload": _loads(r["payload"])} for r in rows]

    def pending_recheck(self) -> list[dict]:
        with self.connect() as db:
            rows = db.execute(
                "SELECT * FROM conclusions WHERE status='pending_recheck' AND version=("
                "SELECT MAX(version) FROM conclusions c2 WHERE c2.truck_id=conclusions.truck_id) "
                "ORDER BY truck_id").fetchall()
        out = []
        for row in rows:
            data = _loads(row["payload"])
            data["conclusion_id"] = row["conclusion_id"]
            data["version"] = row["version"]
            data["created"] = row["created"]
            out.append(data)
        return out

    def mix_history(self, bin_id: str) -> list[dict]:
        with self.connect() as db:
            rows = db.execute(
                "SELECT * FROM events WHERE bin_id=? AND event_type IN "
                "('BIN_MIX','BIN_MIX_OPEN','BIN_MIX_INVALID') ORDER BY seq",
                (bin_id,)).fetchall()
        return [{"seq": r["seq"], "event_type": r["event_type"], "created": r["created"],
                 "payload": _loads(r["payload"])} for r in rows]


def _conclusion_dict(c) -> dict:
    return {
        "truck_id": c.truck_id, "contract_id": c.contract_id, "grain": c.grain,
        "bin_id": c.bin_id, "net_kg": c.net_kg, "basis": c.basis, "points": c.points,
        "assay_fingerprint": c.assay_fingerprint, "moisture": c.decided_moisture,
        "impurity": c.decided_impurity, "basis_detail": c.basis_detail, "grade": c.grade,
        "price": c.price, "clean_kg": c.clean_kg, "dry_kg": c.dry_kg,
        "standard_kg": c.standard_kg, "amount": c.amount,
        "unrounded_amount": c.unrounded_amount, "status": c.status,
        "reject_code": c.reject_code, "reject_reason": c.reject_reason,
        "supersedes": c.supersedes,
    }
