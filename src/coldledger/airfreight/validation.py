"""主分单关系、拆货、并货的校验规则。

所有规则返回 Finding 列表：
- ERROR  -> 阻断，操作整体不落账（只留 BLOCKED 尝试记录与原因）；
- WARN   -> 不阻断但留痕（如容差内的小重量差）。

校验全部针对"暂存账本"进行，通过后由 AirCargoBook 一次性提交，
因此任何一条 ERROR 都不会产生半份单据。
"""
from __future__ import annotations
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import TYPE_CHECKING

from . import weight as w
from .model import REQUIRED_DOCS

if TYPE_CHECKING:
    from .book import AirCargoBook

ERROR = "ERROR"
WARN = "WARN"
DEFAULT_MIN_CONNECTION_MINUTES = 120     # 医药空运最短转运衔接时间


@dataclass(frozen=True)
class Finding:
    level: str
    code: str
    message: str
    refs: tuple[str, ...] = ()


def err(code, message, *refs) -> Finding:
    return Finding(ERROR, code, message, tuple(refs))


def warn(code, message, *refs) -> Finding:
    return Finding(WARN, code, message, tuple(refs))


def blocked(findings) -> bool:
    return any(f.level == ERROR for f in findings)


def _utc(v: datetime) -> datetime:
    return v.astimezone(timezone.utc)


# ---------- 单票分单的航段衔接 ----------

def validate_route(book: "AirCargoBook", hawb, min_conn_minutes: int) -> list[Finding]:
    """分单航段必须存在、按起飞时间排序后首尾相接（目的地=下一段起运地），
    且中转衔接时间不小于最小缓冲。"""
    out: list[Finding] = []
    legs = []
    for leg_id in hawb.leg_ids:
        leg = book.leg(leg_id)
        if leg is None:
            out.append(err("LEG_UNKNOWN", f"航段 {leg_id} 不存在", hawb.hawb_id, leg_id))
            return out
        legs.append(leg)
    ordered = sorted(legs, key=lambda x: (_utc(x.depart), x.leg_id))
    if [x.leg_id for x in ordered] != list(hawb.leg_ids):
        out.append(err("LEG_ORDER", "分单航段顺序与起飞时间不一致", hawb.hawb_id))
    gap_min = timedelta(minutes=min_conn_minutes)
    for prev, nxt in zip(ordered, ordered[1:]):
        if prev.destination != nxt.origin:
            out.append(err("CONNECTION_MISMATCH",
                           f"中转点对不上：{prev.leg_id} 终到 {prev.destination}，"
                           f"{nxt.leg_id} 起运 {nxt.origin}",
                           hawb.hawb_id, prev.leg_id, nxt.leg_id))
            continue
        slack = _utc(nxt.depart) - _utc(prev.arrive)
        if slack < gap_min:
            hours = slack.total_seconds() / 3600
            out.append(err("CONNECTION_TOO_TIGHT",
                           f"转运衔接仅 {hours:.1f} 小时，短于最小 {min_conn_minutes/60:.1f} 小时",
                           hawb.hawb_id, prev.leg_id, nxt.leg_id))
    return out


# ---------- 单证 / 海关 / 截点 ----------

def validate_docs(book: "AirCargoBook", hawb, now: datetime) -> list[Finding]:
    out: list[Finding] = []
    now = _utc(now)
    required = REQUIRED_DOCS[hawb.regime]
    present = {d.doc_type for d in hawb.docs}
    for doc_type in sorted(required - present):
        out.append(err("DOC_MISSING", f"缺少必备单证 {doc_type}", hawb.hawb_id, doc_type))
    for doc in hawb.docs:
        if doc.doc_type in required and not doc.checked():
            out.append(err("DOC_UNCHECKED", f"单证 {doc.doc_type} 尚未完成审核",
                           hawb.hawb_id, doc.reference))
    if hawb.customs_ref is not None:
        decl = hawb.doc("CUSTOMS_DECLARATION")
        if decl is None or decl.reference != hawb.customs_ref:
            out.append(err("CUSTOMS_REF_MISMATCH",
                           f"分单报关号 {hawb.customs_ref} 与报关单证不一致", hawb.hawb_id))
    for leg_id in hawb.leg_ids:
        leg = book.leg(leg_id)
        if leg is None:
            continue
        if now > _utc(leg.cutoff("doc_ready")):
            out.append(err("DOC_CUTOFF", f"已过 {leg.flight_no} 单证截单时间",
                           hawb.hawb_id, leg_id))
        if now > _utc(leg.cutoff("customs")):
            out.append(err("CUSTOMS_CUTOFF", f"已过 {leg.flight_no} 海关放行截点",
                           hawb.hawb_id, leg_id))
        if now > _utc(leg.cutoff("ramp")):
            out.append(err("RAMP_CUTOFF", f"已过 {leg.flight_no} 货站收板截点",
                           hawb.hawb_id, leg_id))
    return out


# ---------- 按航段挂主单（主分单关系一次性建全链） ----------

def validate_assign_chain(book: "AirCargoBook", entries, now: datetime,
                          min_conn_minutes: int = DEFAULT_MIN_CONNECTION_MINUTES) -> list[Finding]:
    """entries: (hawb_id, mawb_id) 对。每个分单必须把其全部航段一次挂全，
    杜绝只挂半程的半份单据。"""
    out: list[Finding] = []
    per_house: dict[str, list[str]] = {}
    per_mawb: dict[str, list[str]] = {}
    for hawb_id, mawb_id in entries:
        per_house.setdefault(hawb_id, []).append(mawb_id)
        per_mawb.setdefault(mawb_id, []).append(hawb_id)

    for hawb_id, mawb_ids in per_house.items():
        hawb = book.hawb(hawb_id)
        if hawb is None:
            out.append(err("HAWB_UNKNOWN", f"分单 {hawb_id} 不存在", hawb_id))
            continue
        if hawb.state != "planned":
            out.append(err("HAWB_NOT_BOOKABLE",
                           f"分单状态为 {hawb.state}，不能再挂主单", hawb_id))
        if len(mawb_ids) != len(set(mawb_ids)):
            out.append(err("DOUBLE_ENTRY", "同一航段重复挂单", hawb_id))
        out.extend(validate_route(book, hawb, min_conn_minutes))
        out.extend(validate_docs(book, hawb, now))
        covered = set()
        for mawb_id in mawb_ids:
            mawb = book.mawb(mawb_id)
            if mawb is None:
                out.append(err("MAWB_UNKNOWN", f"主单 {mawb_id} 不存在", hawb_id, mawb_id))
                continue
            covered.add(mawb.leg_id)
            if mawb.state != "open":
                out.append(err("MAWB_CLOSED",
                               f"主单 {mawb_id} 状态为 {mawb.state}，不能追加分单",
                               hawb_id, mawb_id))
            if book.is_assigned(hawb_id, mawb_id):
                out.append(err("HAWB_ALREADY_ASSIGNED", "分单已挂在该主单下",
                               hawb_id, mawb_id))
        missing = set(hawb.leg_ids) - covered
        if missing:
            out.append(err("HAWB_CHAIN_INCOMPLETE",
                           f"分单还有 {sorted(missing)} 航段未挂主单，禁止先挂半程",
                           hawb_id, *sorted(missing)))

    for mawb_id, hawb_ids in per_mawb.items():
        mawb = book.mawb(mawb_id)
        if mawb is None:
            continue
        houses = [book.hawb(h) for h in hawb_ids if book.hawb(h) is not None]
        regimes = {h.regime for h in houses}
        if len(regimes) > 1:
            out.append(err("REGIME_MIX", "同一主单下不能混挂一般贸易与保税中货",
                           mawb_id, *sorted(regimes)))
        if "bonded" in regimes and not mawb.customs_office:
            out.append(err("BONDED_OFFICE", "保税中转主单必须指定主管海关", mawb_id))
        all_houses = list(dict.fromkeys(book.hawbs_on(mawb_id) + hawb_ids))
        resolved = [book.hawb(h) for h in all_houses]
        resolved = [h for h in resolved if h is not None]
        if resolved:
            out.extend(_check_mawb_totals(mawb, resolved))
    return out


def _check_mawb_totals(mawb, houses) -> list[Finding]:
    out: list[Finding] = []
    agg = w.total(h.weight for h in houses)
    for field_name, declared, got, tol in (
        ("毛重", mawb.weight.gross, agg.gross, w.tolerance(mawb.weight.gross)),
        ("计费重量", mawb.weight.chargeable, agg.chargeable,
         w.tolerance(mawb.weight.chargeable)),
    ):
        gap = declared - got
        if abs(gap) > tol:
            out.append(err("WEIGHT_MISMATCH",
                           f"{field_name}对不上：主单 {declared}kg，分单合计 {got}kg，"
                           f"差 {gap}kg 超出容差 ±{tol}kg",
                           mawb.mawb_id))
        elif gap != 0:
            out.append(warn("WEIGHT_TOLERANCE",
                            f"{field_name}差 {gap}kg，在容差 ±{tol}kg 内", mawb.mawb_id))
    if mawb.weight.pieces != agg.pieces:
        out.append(err("PIECES_MISMATCH",
                       f"件数对不上：主单 {mawb.weight.pieces} 件，分单合计 {agg.pieces} 件",
                       mawb.mawb_id))
    return out


# ---------- 拆货 ----------

@dataclass(frozen=True)
class SplitChild:
    hawb_id: str
    weight: w.Weight
    docs: tuple = ()
    customs_ref: str | None = None
    shipper: str | None = None
    consignee: str | None = None


def validate_split(book: "AirCargoBook", parent_id: str, children, now: datetime) -> list[Finding]:
    out: list[Finding] = []
    parent = book.hawb(parent_id)
    if parent is None:
        return [err("HAWB_UNKNOWN", f"分单 {parent_id} 不存在", parent_id)]
    if parent.state != "planned":
        out.append(err("HAWB_NOT_SPLITTABLE",
                       f"分单状态为 {parent.state}：已挂单/已并单的货物不能直接拆，"
                       f"须先撤回再拆，避免舱单失真", parent_id))
    if len(children) < 2:
        out.append(err("SPLIT_TOO_FEW", "拆货至少要拆成两票", parent_id))
    ids = [c.hawb_id for c in children]
    if len(set(ids)) != len(ids):
        out.append(err("CHILD_ID_DUP", "拆出的分单号重复", parent_id))
    for child in children:
        if book.hawb(child.hawb_id) is not None:
            out.append(err("CHILD_EXISTS", f"分单 {child.hawb_id} 已存在", child.hawb_id))
    if out:
        return out

    agg = w.total(c.weight for c in children)
    for label, a, b in (
        ("毛重", agg.gross, parent.weight.gross),
        ("体积重量", agg.volumetric, parent.weight.volumetric),
    ):
        if a != b:
            out.append(err("SPLIT_WEIGHT_GAP",
                           f"拆货{label}不守恒：合计 {a}kg != 原票 {b}kg", parent_id))
    if agg.pieces != parent.weight.pieces:
        out.append(err("SPLIT_PIECES_GAP",
                       f"拆货件数不守恒：合计 {agg.pieces} != 原票 {parent.weight.pieces}",
                       parent_id))

    required = REQUIRED_DOCS[parent.regime]
    for child in children:
        stub = type(parent)(child.hawb_id, parent.regime, child.weight, parent.leg_ids,
                            tuple(child.docs), "planned", child.customs_ref,
                            child.shipper, child.consignee)
        out.extend(validate_docs(book, stub, now))
        present = {d.doc_type for d in child.docs}
        missing = required - present
        if missing:
            out.append(err("SPLIT_DOC_GAP",
                           f"拆出分单缺单证 {sorted(missing)}，不能让子票带着文件缺口出账",
                           child.hawb_id))
    return out


# ---------- 并货 ----------

def validate_merge(book: "AirCargoBook", parent_ids, new_id: str, now: datetime,
                   *, shipper: str | None = None, consignee: str | None = None) -> list[Finding]:
    out: list[Finding] = []
    if len(parent_ids) < 2:
        out.append(err("MERGE_TOO_FEW", "并货至少要两票"))
    if len(set(parent_ids)) != len(parent_ids):
        out.append(err("MERGE_DUP", "并货清单中有重复分单"))
    if book.hawb(new_id) is not None:
        out.append(err("CHILD_EXISTS", f"分单 {new_id} 已存在", new_id))
    parents = []
    for pid in parent_ids:
        h = book.hawb(pid)
        if h is None:
            out.append(err("HAWB_UNKNOWN", f"分单 {pid} 不存在", pid))
            continue
        if h.state != "planned":
            out.append(err("HAWB_NOT_MERGEABLE",
                           f"分单 {pid} 状态为 {h.state}，不能参与并货", pid))
        parents.append(h)
    if out or len(parents) < 2:
        return out
    regimes = {h.regime for h in parents}
    if len(regimes) > 1:
        out.append(err("REGIME_MIX", "一般贸易与保税中货不能并成一票", *sorted(regimes)))
    legs = {h.leg_ids for h in parents}
    if len(legs) > 1:
        out.append(err("MERGE_ROUTE", "参与并货的分单航段链不一致，不能并单"))
    customs_refs = {h.customs_ref for h in parents if h.customs_ref}
    new_customs_ref = next(iter(customs_refs)) if len(customs_refs) == 1 else None
    merged_weight = w.total(h.weight for h in parents)
    merged_docs = _merge_docs(parents, new_id, now)
    stub = type(parents[0])(new_id, next(iter(regimes)), merged_weight,
                            parents[0].leg_ids, tuple(merged_docs), "planned",
                            new_customs_ref, shipper, consignee)
    out.extend(validate_route(book, stub, DEFAULT_MIN_CONNECTION_MINUTES))
    out.extend(validate_docs(book, stub, now))
    return out


def _merge_docs(parents, new_id: str, now: datetime):
    now = _utc(now)
    seen, merged = set(), []
    from .model import Doc
    merged.append(Doc("HOUSE_AWB", new_id, now,
                      inherited_from=",".join(h.hawb_id for h in parents)))
    for h in parents:
        for doc in h.docs:
            if doc.doc_type == "HOUSE_AWB":
                continue
            key = (doc.doc_type, doc.reference)
            if key in seen:
                continue
            seen.add(key)
            checked = doc.checked_at or now          # 并票时重新过审核
            merged.append(type(doc)(doc.doc_type, doc.reference, checked,
                                    inherited_from=h.hawb_id))
    return merged
