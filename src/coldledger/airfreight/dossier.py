"""主分单单证包(dossier)序列化与原子落盘。

一票主单的单证包汇总：主分单关系、分单单证清单、重量体积汇总、
航段衔接时间、校验发现、舱单行。先写临时文件再 rename，避免在磁盘
上留下写了一半的 JSON。
"""
from __future__ import annotations
import json
import os
import tempfile
from datetime import datetime

from .book import AirCargoBook
from . import weight as w


def _dt(v: datetime | None) -> str | None:
    return v.astimezone().isoformat() if v else None


def weight_dict(x: w.Weight) -> dict:
    return {"gross_kg": str(x.gross), "volumetric_kg": str(x.volumetric),
            "chargeable_kg": str(x.chargeable), "pieces": x.pieces}


def finding_dict(f) -> dict:
    return {"level": f.level, "code": f.code, "message": f.message,
            "refs": list(f.refs)}


def leg_dict(leg) -> dict:
    return {"leg_id": leg.leg_id, "carrier": leg.carrier, "flight_no": leg.flight_no,
            "origin": leg.origin, "destination": leg.destination,
            "depart_utc": _dt(leg.depart), "arrive_utc": _dt(leg.arrive),
            "cutoffs_minutes": dict(leg.cutoffs_minutes)}


def hawb_dict(h) -> dict:
    return {"hawb_id": h.hawb_id, "regime": h.regime, "state": h.state,
            "legs": list(h.leg_ids), "customs_ref": h.customs_ref,
            "shipper": h.shipper, "consignee": h.consignee,
            "derived_from": list(h.derived_from),
            "weight": weight_dict(h.weight),
            "docs": [{"type": d.doc_type, "reference": d.reference,
                      "checked_at": _dt(d.checked_at),
                      "inherited_from": d.inherited_from} for d in h.docs]}


def connection_view(book: AirCargoBook, hawb_id: str) -> list[dict]:
    """该分单相邻航段的衔接视图：中转点、到达、起飞、缓冲小时数。"""
    h = book.hawb(hawb_id)
    out = []
    if h is None:
        return out
    for a_id, b_id in zip(h.leg_ids, h.leg_ids[1:]):
        a, b = book.leg(a_id), book.leg(b_id)
        slack = (b.depart - a.arrive).total_seconds() / 3600
        out.append({"transfer_at": a.destination,
                    "inbound_arrive_utc": _dt(a.arrive),
                    "outbound_depart_utc": _dt(b.depart),
                    "slack_hours": round(slack, 2),
                    "matched": a.destination == b.origin})
    return out


def mawb_dossier(book: AirCargoBook, mawb_id: str) -> dict:
    mawb = book.mawb(mawb_id)
    if mawb is None:
        raise KeyError(mawb_id)
    house_ids = book.hawbs_on(mawb_id)
    houses = [book.hawb(h) for h in house_ids]
    totals = w.total(h.weight for h in houses) if houses else None
    return {
        "mawb": {"mawb_id": mawb.mawb_id, "leg_id": mawb.leg_id,
                 "state": mawb.state, "customs_office": mawb.customs_office,
                 "declared": weight_dict(mawb.weight)},
        "leg": leg_dict(book.leg(mawb.leg_id)),
        "houses": [hawb_dict(h) for h in houses],
        "totals": weight_dict(totals) if totals else None,
        "declared_vs_total": (
            {"gross_delta_kg": str(mawb.weight.gross - totals.gross),
             "chargeable_delta_kg": str(mawb.weight.chargeable - totals.chargeable),
             "pieces_delta": mawb.weight.pieces - totals.pieces}
            if totals else None),
        "connections": {h: connection_view(book, h) for h in house_ids},
        "manifests": sorted(mid for mid, m in book.manifests.items()
                            if m.mawb_id == mawb_id),
    }


def attempt_dict(rec) -> dict:
    return {"attempt_id": rec.attempt_id, "operation": rec.operation,
            "status": rec.status, "actor": rec.actor, "at": _dt(rec.at),
            "manifests": list(rec.manifests),
            "payload": _jsonable(rec.payload),
            "findings": [finding_dict(f) for f in rec.findings]}


def _jsonable(value):
    if isinstance(value, (list, tuple)):
        return [_jsonable(v) for v in value]
    if isinstance(value, dict):
        return {k: _jsonable(v) for k, v in value.items()}
    if isinstance(value, datetime):
        return _dt(value)
    return value


def book_snapshot(book: AirCargoBook) -> dict:
    return {
        "legs": [leg_dict(book.leg(k)) for k in sorted(book._state.legs)],
        "mawbs": [mawb_dossier(book, k) for k in sorted(book._state.mawbs)],
        "attempts": [attempt_dict(a) for a in book.attempts],
    }


def export_atomic(path: str, payload: dict) -> str:
    """同目录临时文件 + os.replace 原子替换，杜绝半截文件。"""
    directory = os.path.dirname(os.path.abspath(path))
    os.makedirs(directory, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=".dossier-", suffix=".tmp", dir=directory)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(payload, fh, ensure_ascii=False, indent=2, sort_keys=True)
            fh.write("\n")
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
    return path
