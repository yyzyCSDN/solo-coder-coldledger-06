"""空运航段、单证、主单(MAWB)与分单(HAWB)领域模型。"""
from __future__ import annotations
from dataclasses import dataclass, field, replace
from datetime import datetime
from decimal import Decimal
from typing import Mapping

from . import weight as w

REGIMES = ("general", "bonded")          # 一般贸易 / 保税中转
HAWB_STATES = ("planned", "booked", "split", "merged", "cancelled")
MAWB_STATES = ("open", "manifested", "departed", "closed")
DEFAULT_CUTOFFS = {
    "doc_ready": 180,     # 单证截单：起飞前分钟
    "customs": 240,       # 海关放行截点
    "ramp": 120,          # 货站收板截点
}


@dataclass(frozen=True)
class AirLeg:
    """航段（一次承运航段；中转即多段衔接）。"""
    leg_id: str
    carrier: str
    flight_no: str
    origin: str
    destination: str
    depart: datetime                # UTC（core.clock.utc 转换后传入）
    arrive: datetime
    cutoffs_minutes: Mapping[str, int] = field(default_factory=lambda: dict(DEFAULT_CUTOFFS))

    def __post_init__(self):
        if self.depart >= self.arrive:
            raise ValueError("arrive before depart")
        if not self.origin or not self.destination or self.origin == self.destination:
            raise ValueError("invalid leg routing")

    def cutoff(self, name: str) -> datetime:
        from datetime import timedelta
        return self.depart - timedelta(minutes=int(self.cutoffs_minutes[name]))


@dataclass(frozen=True)
class Doc:
    """随附单证。checked_at 为空表示尚未过单证审核，不能用于截单放行。"""
    doc_type: str                   # AWB / COMMERCIAL_INVOICE / PACKING_LIST / CUSTOMS_DECLARATION / ...
    reference: str
    checked_at: datetime | None = None
    inherited_from: str | None = None   # 拆货时继承自哪个父分单

    def checked(self) -> bool:
        return self.checked_at is not None


# 不同监管模式下每票分单必备的单证
REQUIRED_DOCS = {
    "general": frozenset({
        "HOUSE_AWB", "COMMERCIAL_INVOICE", "PACKING_LIST", "CUSTOMS_DECLARATION",
    }),
    "bonded": frozenset({
        "HOUSE_AWB", "COMMERCIAL_INVOICE", "PACKING_LIST",
        "BONDED_MANIFEST", "CUSTOMS_TRANSIT_DECLARATION",
    }),
}


@dataclass(frozen=True)
class Hawb:
    hawb_id: str
    regime: str
    weight: w.Weight
    leg_ids: tuple[str, ...]               # 该票实际承运的连续航段
    docs: tuple[Doc, ...] = ()
    state: str = "planned"
    customs_ref: str | None = None         # 报关单号（须与 CUSTOMS_DECLARATION 一致）
    shipper: str | None = None
    consignee: str | None = None
    derived_from: tuple[str, ...] = ()     # 拆/并货来源分单（血缘留痕）

    def __post_init__(self):
        if self.regime not in REGIMES:
            raise ValueError("unknown customs regime")
        if self.state not in HAWB_STATES:
            raise ValueError("unknown hawb state")
        if not self.leg_ids or len(set(self.leg_ids)) != len(self.leg_ids):
            raise ValueError("hawb legs must be a non-empty unique sequence")

    def doc(self, doc_type: str) -> Doc | None:
        return next((d for d in self.docs if d.doc_type == doc_type), None)

    def has_doc(self, doc_type: str) -> bool:
        return self.doc(doc_type) is not None


@dataclass(frozen=True)
class Mawb:
    mawb_id: str
    leg_id: str                            # 主单按航段签发：一主单对应一航段
    weight: w.Weight                       # 主单申报重量（航段汇总口径）
    state: str = "open"
    customs_office: str | None = None

    def __post_init__(self):
        if self.state not in MAWB_STATES:
            raise ValueError("unknown mawb state")


@dataclass(frozen=True)
class ManifestLine:
    mawb_id: str
    leg_id: str
    hawb_id: str
    gross: Decimal
    chargeable: Decimal
    pieces: int


@dataclass(frozen=True)
class Manifest:
    """主单舱单：只有挂单成功后才生成。"""
    manifest_id: str
    mawb_id: str
    leg_id: str
    lines: tuple[ManifestLine, ...]
    weight: w.Weight
    created_at: datetime
