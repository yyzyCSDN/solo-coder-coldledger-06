"""重量/体积计量与汇总。

空运以计费重量(chargeable weight)结算：取实际毛重与体积重量的较大值。
体积重量按 IATA 标准 6000 cm³/kg 折算，并按 0.5kg 向上取整
（不足 0.5kg 的部分进到 0.5kg）。所有计算用 Decimal，避免浮点漂移
导致拆并货后"重量失真"。
"""
from __future__ import annotations
from dataclasses import dataclass
from decimal import Decimal, ROUND_HALF_UP
from typing import Iterable

VOLUME_FACTOR = Decimal("6000")          # cm^3 per kg（体积重量除数）
CHARGE_STEP = Decimal("0.5")             # 计费重量取整步长（kg）
ABS_TOLERANCE = Decimal("0.5")           # 主分单汇总绝对容差（kg）
REL_TOLERANCE = Decimal("0.005")         # 主分单汇总相对容差（0.5%）
ZERO = Decimal("0")


def _q(value) -> Decimal:
    return value if isinstance(value, Decimal) else Decimal(str(value))


def charge_round(weight: Decimal) -> Decimal:
    """向上按 0.5kg 取整（0.2 -> 0.5，0.6 -> 1.0，1.0 -> 1.0）。"""
    weight = _q(weight)
    if weight < 0:
        raise ValueError("negative weight")
    scaled = (weight / CHARGE_STEP).to_integral_value(rounding="ROUND_CEILING")
    return (scaled * CHARGE_STEP).quantize(CHARGE_STEP)


def volumetric_weight(volume_cm3, *, factor: Decimal = VOLUME_FACTOR) -> Decimal:
    """体积重量（kg），结果保留三位小数，不取整。"""
    volume = _q(volume_cm3)
    if volume < 0:
        raise ValueError("negative volume")
    return (volume / factor).quantize(Decimal("0.001"), rounding=ROUND_HALF_UP)


@dataclass(frozen=True)
class Weight:
    gross: Decimal                 # 实际毛重 kg
    chargeable: Decimal            # 计费重量 kg
    volumetric: Decimal = ZERO     # 体积重量 kg
    pieces: int = 0

    @staticmethod
    def measure(gross, volume_cm3=0, pieces=0, *, factor: Decimal = VOLUME_FACTOR) -> "Weight":
        gross = _q(gross)
        vol_w = volumetric_weight(volume_cm3, factor=factor)
        # 毛重保留测量值（最多三位小数），只有计费重量按 0.5kg 向上取整
        gross = gross.quantize(Decimal("0.001"))
        return Weight(gross, charge_round(max(gross, vol_w)), vol_w, pieces)

    def __post_init__(self):
        if self.gross < 0 or self.chargeable < 0 or self.volumetric < 0:
            raise ValueError("negative weight component")
        if self.pieces < 0:
            raise ValueError("negative pieces")
        if self.chargeable + Decimal("0.0001") < self.gross:
            raise ValueError("chargeable weight below gross weight")


def total(rows: Iterable[Weight]) -> Weight:
    """汇总多件/多分单重量。计费重量按汇总后的毛重/体积重重新取整，
    而不是把各分单计费重量直接相加（两者可能因取整相差半个步长）。"""
    rows = tuple(rows)
    gross = sum((r.gross for r in rows), ZERO).quantize(Decimal("0.001"))
    vol = sum((r.volumetric for r in rows), ZERO).quantize(Decimal("0.001"))
    pieces = sum(r.pieces for r in rows)
    return Weight(gross, charge_round(max(gross, vol)), vol, pieces)


def tolerance(master: Decimal) -> Decimal:
    """主分单重量允许偏差：max(0.5kg, 主单值 × 0.5%)。"""
    return max(ABS_TOLERANCE, (_q(master) * REL_TOLERANCE).quantize(CHARGE_STEP))


def weight_gap(master: Decimal, houses: Decimal) -> Decimal:
    """主单重量与分单汇总的正向偏差（>0 表示分单汇总不足）。"""
    return _q(master) - _q(houses)


def within_tolerance(master: Decimal, houses: Decimal) -> bool:
    gap = abs(weight_gap(master, houses))
    return gap <= tolerance(master)
