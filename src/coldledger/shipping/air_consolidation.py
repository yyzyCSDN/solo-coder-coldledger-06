"""空运主分单航次域：按航段(leg)建立主单(MAWB)/分单(HAWB)关系。

业务约束：
1. 每个航段有独立的主单编组；同一分单在不同航段可以挂到不同主单上（转运/续程）。
2. 拆并货以分单件货为最小单位：一件货在一个航段只能出现一次，且不得跨主单重复。
3. 分单毛重/体积必须等于其各件货、各主单分摊之和（容差内）；主单汇总必须等于挂载分单之和。
4. 主单、分单、海关材料按航段汇总；缺少必备文件直接阻断，禁止只写半份单据。
5. 相邻航段在同一转运点衔接，必须满足最短转机时间(MCT)与海关查验缓冲。

所有校验产出 Finding(级别/类别/对象/原因)。commit 采用全有或全无语义：
任何 error 都抛 ManifestRejection 并保留全部原因，已提交清单不可变，只能换 revision 重提。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Iterable, Mapping, Sequence

# ---------------------------------------------------------------------------
# 基础值对象
# ---------------------------------------------------------------------------

ZERO = Decimal("0")
# 空运体积重换算：1 CBM = 167 kg（即 6000 cm3/kg 的常用等效值）
VOLUME_WEIGHT_FACTOR = Decimal("167")
DEFAULT_TOLERANCE_KG = Decimal("0.1")

ERROR = "error"
WARNING = "warning"


@dataclass(frozen=True)
class Piece:
    """分单下的一件货，拆并货的最小单位。"""
    piece_id: str
    gross_kg: Decimal
    volume_m3: Decimal
    marks: str = ""


@dataclass(frozen=True)
class HouseWaybill:
    """分单(HAWB)：发货人的一票货，件货集合在跨航段拆分时保持身份不变。"""
    house_no: str
    origin: str
    destination: str
    pieces: tuple[Piece, ...]
    declared_gross_kg: Decimal
    declared_volume_m3: Decimal

    @property
    def actual_gross_kg(self) -> Decimal:
        return sum((p.gross_kg for p in self.pieces), ZERO)

    @property
    def actual_volume_m3(self) -> Decimal:
        return sum((p.volume_m3 for p in self.pieces), ZERO)

    def piece(self, piece_id: str) -> Piece | None:
        for p in self.pieces:
            if p.piece_id == piece_id:
                return p
        return None


@dataclass(frozen=True)
class MasterWaybill:
    """主单(MAWB)：承运人单据，按航段与航班级挂。"""
    master_no: str
    carrier: str
    flight_no: str
    declared_gross_kg: Decimal
    declared_volume_m3: Decimal


@dataclass(frozen=True)
class FlightLeg:
    """航段：from_airport 起飞、to_airport 到达；相邻航段在 to==from 的机场构成转运衔接。"""
    leg_seq: int
    from_airport: str
    to_airport: str
    depart_at: datetime
    arrive_at: datetime


@dataclass(frozen=True)
class Allocation:
    """一件货在某个航段上挂到某张主单；weight_kg/volume_m3 为该件货实际过磅量。"""
    house_no: str
    master_no: str
    piece_id: str
    weight_kg: Decimal
    volume_m3: Decimal


@dataclass(frozen=True)
class CustomsPacket:
    """海关材料包：在某个航段（出境/中转/入境）清关使用。"""
    leg_seq: int
    documents: frozenset[str]
    cleared_at: datetime | None = None


@dataclass(frozen=True)
class DocRequirement:
    """必备文件规则。scope: house 每张分单 | master 每张主单 | customs 每航段海关包。"""
    scope: str
    document: str
    leg_seq: int | None = None          # None 表示每个航段都要
    customs_location: bool = False      # True 表示只在有海关包的转运点强制


@dataclass(frozen=True)
class TransferPoint:
    """转运截点策略：相邻航段之间的最短转机时间与海关缓冲。"""
    airport: str
    minimum_connect: timedelta
    customs_buffer: timedelta = timedelta()
    customs_required: bool = False


@dataclass(frozen=True)
class Finding:
    severity: str
    category: str          # structure | weight | volume | document | customs | timing
    subject: str
    reason: str


@dataclass(frozen=True)
class Manifest:
    """一个航段的已提交主分单清单（不可变）。"""
    leg_seq: int
    revision: int
    allocations: tuple[Allocation, ...]
    house_docs: Mapping[str, frozenset[str]] = field(default_factory=dict)
    master_docs: Mapping[str, frozenset[str]] = field(default_factory=dict)
    customs: CustomsPacket | None = None


@dataclass(frozen=True)
class ManifestDraft:
    """待提交的航段清单草案。"""
    leg_seq: int
    expected_revision: int
    masters: tuple[MasterWaybill, ...]
    allocations: tuple[Allocation, ...]
    house_docs: Mapping[str, frozenset[str]] = field(default_factory=dict)
    master_docs: Mapping[str, frozenset[str]] = field(default_factory=dict)
    customs: CustomsPacket | None = None


class ManifestRejection(Exception):
    """阻断提交：保留全部原因，调用方不得落半份单据。"""

    def __init__(self, leg_seq: int, findings: Sequence[Finding]):
        self.leg_seq = leg_seq
        self.findings: tuple[Finding, ...] = tuple(findings)
        reasons = "; ".join(f"[{f.category}] {f.subject}: {f.reason}" for f in findings)
        super().__init__(f"leg {leg_seq} manifest rejected: {reasons}")


# ---------------------------------------------------------------------------
# 汇总视图
# ---------------------------------------------------------------------------

def chargeable_weight(gross_kg: Decimal, volume_m3: Decimal) -> Decimal:
    """计费重 = max(实重, 体积重)。"""
    volume_kg = (volume_m3 * VOLUME_WEIGHT_FACTOR).quantize(Decimal("0.001"))
    return max(gross_kg, volume_kg)


def _sum(xs: Iterable[Decimal]) -> Decimal:
    return sum(xs, ZERO)


def house_totals(allocations: Sequence[Allocation]) -> dict[str, dict[str, Decimal]]:
    out: dict[str, dict[str, Decimal]] = {}
    for a in allocations:
        row = out.setdefault(a.house_no, {"gross": ZERO, "volume": ZERO})
        row["gross"] += a.weight_kg
        row["volume"] += a.volume_m3
    return out


def master_totals(allocations: Sequence[Allocation]) -> dict[str, dict[str, Decimal]]:
    out: dict[str, dict[str, Decimal]] = {}
    for a in allocations:
        row = out.setdefault(a.master_no, {"gross": ZERO, "volume": ZERO})
        row["gross"] += a.weight_kg
        row["volume"] += a.volume_m3
    return out


def houses_per_master(allocations: Sequence[Allocation]) -> dict[str, frozenset[str]]:
    out: dict[str, set[str]] = {}
    for a in allocations:
        out.setdefault(a.master_no, set()).add(a.house_no)
    return {m: frozenset(hs) for m, hs in out.items()}


# ---------------------------------------------------------------------------
# 校验
# ---------------------------------------------------------------------------

def _close(a: Decimal, b: Decimal, tol: Decimal) -> bool:
    return abs(a - b) <= tol


def validate_draft(
    draft: ManifestDraft,
    houses: Sequence[HouseWaybill],
    doc_requirements: Sequence[DocRequirement] = (),
    tolerance_kg: Decimal = DEFAULT_TOLERANCE_KG,
) -> tuple[Finding, ...]:
    """对单个航段草案做结构/重量体积/文件校验，返回全部发现（不抛异常）。"""
    findings: list[Finding] = []
    house_index = {h.house_no: h for h in houses}
    master_index = {m.master_no: m for m in draft.masters}

    def err(category, subject, reason):
        findings.append(Finding(ERROR, category, subject, reason))

    def warn(category, subject, reason):
        findings.append(Finding(WARNING, category, subject, reason))

    # 1. 结构：引用完整、件货不重不漏、主单不挂载空单
    seen: set[tuple[str, str]] = set()
    active_houses: set[str] = set()
    for a in draft.allocations:
        h = house_index.get(a.house_no)
        if h is None:
            err("structure", a.house_no, "分单不存在")
            continue
        if a.master_no not in master_index:
            err("structure", a.master_no, f"分单 {a.house_no} 件货 {a.piece_id} 挂到不存在的主单")
            continue
        if h.piece(a.piece_id) is None:
            err("structure", f"{a.house_no}/{a.piece_id}", "件货不属于该分单")
            continue
        key = (a.house_no, a.piece_id)
        if key in seen:
            err("structure", f"{a.house_no}/{a.piece_id}", "同一件货在本航段重复配载")
        seen.add(key)
        active_houses.add(a.house_no)

    for m in draft.masters:
        mounted = {a for a in draft.allocations if a.master_no == m.master_no}
        if not mounted:
            err("structure", m.master_no, "主单未挂载任何分单，禁止空主单随航班")

    # 2. 件货过磅量必须等于件货登记量；分单汇总守恒（拆并货不失真）
    for h_no in sorted(active_houses):
        h = house_index[h_no]
        rows = [a for a in draft.allocations if a.house_no == h_no]
        by_piece = {a.piece_id: a for a in rows}
        for p in h.pieces:
            a = by_piece.get(p.piece_id)
            if a is None:
                continue  # 该件货不在本航段（续程/前程），不算缺失
            if not _close(a.weight_kg, p.gross_kg, tolerance_kg):
                err("weight", f"{h_no}/{p.piece_id}",
                    f"过磅 {a.weight_kg}kg 与登记 {p.gross_kg}kg 不一致")
            if not _close(a.volume_m3, p.volume_m3, Decimal("0.001")):
                err("volume", f"{h_no}/{p.piece_id}",
                    f"体积 {a.volume_m3}m³ 与登记 {p.volume_m3}m³ 不一致")
        gross = _sum(a.weight_kg for a in rows)
        volume = _sum(a.volume_m3 for a in rows)
        mounted_pieces = frozenset(by_piece)
        expected_gross = _sum(p.gross_kg for p in h.pieces if p.piece_id in mounted_pieces)
        expected_volume = _sum(p.volume_m3 for p in h.pieces if p.piece_id in mounted_pieces)
        if not _close(gross, expected_gross, tolerance_kg):
            err("weight", h_no,
                f"分单本航段汇总 {gross}kg 与挂载件货登记 {expected_gross}kg 不符")
        if not _close(volume, expected_volume, Decimal("0.001")):
            err("volume", h_no,
                f"分单本航段体积 {volume}m³ 与挂载件货登记 {expected_volume}m³ 不符")
        # 件货全部在同一航段时，还要与分单全程申报值一致（防止拆并货后申报失真）
        if mounted_pieces == frozenset(p.piece_id for p in h.pieces):
            if not _close(gross, h.declared_gross_kg, tolerance_kg):
                err("weight", h_no,
                    f"分单汇总 {gross}kg 与申报毛重 {h.declared_gross_kg}kg 不符")
            if not _close(volume, h.declared_volume_m3, Decimal("0.001")):
                err("volume", h_no,
                    f"分单体积汇总 {volume}m³ 与申报 {h.declared_volume_m3}m³ 不符")

    # 3. 主单汇总必须等于挂载分单之和，且与主单申报一致
    mt = master_totals(draft.allocations)
    for m in draft.masters:
        totals = mt.get(m.master_no, {"gross": ZERO, "volume": ZERO})
        if not _close(totals["gross"], m.declared_gross_kg, tolerance_kg):
            err("weight", m.master_no,
                f"主单挂载汇总 {totals['gross']}kg 与主单申报 {m.declared_gross_kg}kg 不符")
        if not _close(totals["volume"], m.declared_volume_m3, Decimal("0.001")):
            err("volume", m.master_no,
                f"主单体积汇总 {totals['volume']}m³ 与申报 {m.declared_volume_m3}m³ 不符")

    # 4. 文件：分单/主单/海关包按航段逐项点齐
    for req in doc_requirements:
        if req.leg_seq is not None and req.leg_seq != draft.leg_seq:
            continue
        if req.scope == "house":
            for h_no in sorted(active_houses):
                provided = draft.house_docs.get(h_no, frozenset())
                if req.document not in provided:
                    err("document", h_no, f"缺少分单文件 {req.document}")
        elif req.scope == "master":
            for m in draft.masters:
                provided = draft.master_docs.get(m.master_no, frozenset())
                if req.document not in provided:
                    err("document", m.master_no, f"缺少主单文件 {req.document}")
        elif req.scope == "customs":
            packet = draft.customs
            if packet is None or req.document not in packet.documents:
                err("customs", f"leg {draft.leg_seq}",
                    f"海关材料缺少 {req.document}")
        else:
            warn("document", req.document, f"未知文件规则 scope={req.scope}")

    # 5. 海关包必须挂在当前航段；有放行时间则提醒放行晚于出发的时序问题在 timing 校验
    if draft.customs is not None and draft.customs.leg_seq != draft.leg_seq:
        err("customs", f"leg {draft.leg_seq}", "海关包航段号与清单不符")

    return tuple(findings)


def check_connection(
    inbound: FlightLeg,
    outbound: FlightLeg,
    points: Mapping[str, TransferPoint],
) -> Finding | None:
    """校验相邻航段在转运点的衔接时间，含 MCT 与海关缓冲。"""
    if inbound.leg_seq + 1 != outbound.leg_seq:
        return Finding(ERROR, "timing",
                       f"leg {inbound.leg_seq}->{outbound.leg_seq}", "航段不连续")
    if inbound.to_airport != outbound.from_airport:
        return Finding(ERROR, "timing", f"{inbound.to_airport}/{outbound.from_airport}",
                       f"转运点不一致：{inbound.to_airport} -> {outbound.from_airport}")
    point = points.get(inbound.to_airport)
    if point is None:
        return Finding(WARNING, "timing", inbound.to_airport, "未配置转运截点策略")
    gap = (outbound.depart_at.astimezone(timezone.utc)
           - inbound.arrive_at.astimezone(timezone.utc))
    needed = point.minimum_connect + (point.customs_buffer if point.customs_required else timedelta())
    if gap < needed:
        return Finding(ERROR, "timing", inbound.to_airport,
                       f"衔接 {gap} 短于 MCT+海关 {needed}")
    return None


def check_customs_timing(
    legs: Sequence[FlightLeg], packets: Sequence[CustomsPacket]
) -> tuple[Finding, ...]:
    """海关放行必须早于所在航段出发；未放行是 error（不能先放飞）。"""
    findings: list[Finding] = []
    leg_index = {leg.leg_seq: leg for leg in legs}
    for packet in packets:
        leg = leg_index.get(packet.leg_seq)
        if leg is None:
            findings.append(Finding(ERROR, "customs", f"leg {packet.leg_seq}", "海关包挂到不存在的航段"))
            continue
        if packet.cleared_at is None:
            findings.append(Finding(ERROR, "customs", f"leg {packet.leg_seq}", "海关尚未放行"))
        elif leg.depart_at is not None and packet.cleared_at.astimezone(timezone.utc) > leg.depart_at.astimezone(timezone.utc):
            findings.append(Finding(ERROR, "customs", f"leg {packet.leg_seq}",
                                    "放行时间晚于航段出发时间"))
    return tuple(findings)


def check_house_continuity(
    houses: Sequence[HouseWaybill], drafts: Sequence[ManifestDraft],
) -> tuple[Finding, ...]:
    """跨航段守恒：一张分单的每件货在整条链路上恰好配载一次，且重量体积不丢不重。"""
    findings: list[Finding] = []
    drafts_by_leg = {d.leg_seq: d for d in drafts}
    for h in houses:
        mounts: dict[str, list[tuple[int, Allocation]]] = {}
        for leg_seq, draft in sorted(drafts_by_leg.items()):
            for a in draft.allocations:
                if a.house_no == h.house_no:
                    mounts.setdefault(a.piece_id, []).append((leg_seq, a))
        for p in h.pieces:
            rows = mounts.get(p.piece_id, [])
            if not rows:
                findings.append(Finding(ERROR, "weight", f"{h.house_no}/{p.piece_id}",
                                        "件货在整条航段链路上缺失"))
            elif len(rows) > 1:
                legs = ",".join(str(leg) for leg, _ in rows)
                findings.append(Finding(ERROR, "structure", f"{h.house_no}/{p.piece_id}",
                                        f"件货在多个航段重复配载 (leg {legs})"))
            else:
                _, a = rows[0]
                if not _close(a.weight_kg, p.gross_kg, DEFAULT_TOLERANCE_KG):
                    findings.append(Finding(ERROR, "weight", f"{h.house_no}/{p.piece_id}",
                                            f"跨段重量 {a.weight_kg}kg != 登记 {p.gross_kg}kg"))
    return tuple(findings)


def blocking(findings: Sequence[Finding]) -> bool:
    return any(f.severity == ERROR for f in findings)


# ---------------------------------------------------------------------------
# 全有或全无的提交
# ---------------------------------------------------------------------------

@dataclass
class ConsolidationLedger:
    """各航段已提交清单的账本；提交是原子操作，失败不产生任何部分写入。"""
    manifests: dict[int, Manifest] = field(default_factory=dict)
    rejections: list[ManifestRejection] = field(default_factory=list)

    def revision(self, leg_seq: int) -> int:
        current = self.manifests.get(leg_seq)
        return current.revision if current else 0

    def commit(
        self,
        draft: ManifestDraft,
        houses: Sequence[HouseWaybill],
        doc_requirements: Sequence[DocRequirement] = (),
        tolerance_kg: Decimal = DEFAULT_TOLERANCE_KG,
    ) -> Manifest:
        findings = list(validate_draft(draft, houses, doc_requirements, tolerance_kg))
        if blocking(findings):
            rejection = ManifestRejection(draft.leg_seq, findings)
            self.rejections.append(rejection)
            raise rejection
        manifest = Manifest(
            leg_seq=draft.leg_seq,
            revision=draft.expected_revision + 1,
            allocations=draft.allocations,
            house_docs=dict(draft.house_docs),
            master_docs=dict(draft.master_docs),
            customs=draft.customs,
        )
        self.manifests[draft.leg_seq] = manifest
        return manifest

    def commit_chain(
        self,
        drafts: Sequence[ManifestDraft],
        houses: Sequence[HouseWaybill],
        legs: Sequence[FlightLeg],
        points: Mapping[str, TransferPoint],
        packets: Sequence[CustomsPacket] = (),
        doc_requirements: Sequence[DocRequirement] = (),
        tolerance_kg: Decimal = DEFAULT_TOLERANCE_KG,
    ) -> tuple[Manifest, ...]:
        """整链提交：先全部草案校验 + 航段衔接 + 海关时序，全部通过才一次落账。"""
        all_findings: list[Finding] = []
        for draft in drafts:
            current = self.manifests.get(draft.leg_seq)
            expected = current.revision if current else 0
            if draft.expected_revision != expected:
                all_findings.append(Finding(ERROR, "structure", f"leg {draft.leg_seq}",
                                            f"乐观锁冲突：期望 revision {expected}"))
            all_findings.extend(validate_draft(draft, houses, doc_requirements, tolerance_kg))

        ordered = sorted(legs, key=lambda x: x.leg_seq)
        for prev, nxt in zip(ordered, ordered[1:]):
            f = check_connection(prev, nxt, points)
            if f is not None:
                all_findings.append(f)
        all_findings.extend(check_customs_timing(ordered, packets))
        # 草案覆盖全部传入航段时，按完整行程校验拆并货跨段守恒
        draft_legs = frozenset(d.leg_seq for d in drafts)
        chain_legs = frozenset(l.leg_seq for l in ordered)
        if draft_legs == chain_legs:
            all_findings.extend(check_house_continuity(houses, drafts))

        if blocking(all_findings):
            rejection = ManifestRejection(-1, all_findings)
            self.rejections.append(rejection)
            raise rejection

        committed: list[Manifest] = []
        for draft in drafts:
            manifest = Manifest(
                leg_seq=draft.leg_seq,
                revision=draft.expected_revision + 1,
                allocations=draft.allocations,
                house_docs=dict(draft.house_docs),
                master_docs=dict(draft.master_docs),
                customs=draft.customs,
            )
            self.manifests[draft.leg_seq] = manifest
            committed.append(manifest)
        return tuple(committed)


def split_house(
    house: HouseWaybill, master_no_first: str, master_no_second: str,
    first_piece_ids: frozenset[str],
) -> tuple[tuple[Allocation, ...], tuple[Allocation, ...]]:
    """工具：把一票分单按件货拆到两张主单上（两段续程），重量体积直接取自件货，不做估算。

    返回 (首段配载, 次段配载)，调用方分别放进对应航段的 ManifestDraft。
    """
    first: list[Allocation] = []
    second: list[Allocation] = []
    for p in house.pieces:
        row = Allocation(house.house_no,
                         master_no_first if p.piece_id in first_piece_ids else master_no_second,
                         p.piece_id, p.gross_kg, p.volume_m3)
        (first if p.piece_id in first_piece_ids else second).append(row)
    return tuple(first), tuple(second)


def merge_allocations(*groups: Iterable[Allocation]) -> tuple[Allocation, ...]:
    """工具：把多票分单的配载并到同一航段主单（并货）。"""
    return tuple(a for group in groups for a in group)
