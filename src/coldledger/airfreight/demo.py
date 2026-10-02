"""空运主分单场景演示（无外部依赖，可直接 python -m coldledger.airfreight.demo）。

场景一：PVG -> FRA -> ORD 保税中转，两份分单按两个航段各挂各的主单，
        汇总重量体积、校验单证与转运衔接，成功生成两段舱单。
场景二：三种典型对不上（缺海关单证、过了单证截点、主分单重量差超限、
        只挂半程），全部 BLOCKED，账本保持原样且原因全部留痕。
场景三：先拆后并，重量/体积/件数严格守恒，失败的拆货尝试不留半票。
"""
from __future__ import annotations
from datetime import datetime, timedelta, timezone
from decimal import Decimal

from .book import AirCargoBook, COMMITTED, BLOCKED
from .model import AirLeg, Doc, Hawb, Mawb
from .weight import Weight
from . import dossier

UTC = timezone.utc
NOW = datetime(2026, 10, 2, 6, 0, tzinfo=UTC)


def docs(general=True, customs_ref="CUS-1", *, checked=True):
    at = NOW - timedelta(days=1) if checked else None
    if general:
        return (Doc("HOUSE_AWB", "HA", at),
                Doc("COMMERCIAL_INVOICE", "INV", at),
                Doc("PACKING_LIST", "PL", at),
                Doc("CUSTOMS_DECLARATION", customs_ref, at))
    return (Doc("HOUSE_AWB", "HA", at),
            Doc("COMMERCIAL_INVOICE", "INV", at),
            Doc("PACKING_LIST", "PL", at),
            Doc("BONDED_MANIFEST", "BM", at),
            Doc("CUSTOMS_TRANSIT_DECLARATION", "CTD", at))


def bonded_book():
    book = AirCargoBook()
    l1 = AirLeg("PVG-FRA", "CA", "CA931", "PVG", "FRA",
                datetime(2026, 10, 2, 13, 0, tzinfo=UTC),
                datetime(2026, 10, 2, 19, 30, tzinfo=UTC))
    l2 = AirLeg("FRA-ORD", "UA", "UA945", "FRA", "ORD",
                datetime(2026, 10, 3, 0, 30, tzinfo=UTC),
                datetime(2026, 10, 3, 4, 0, tzinfo=UTC))   # FRA 中转 5h，满足最短 2h
    book.add_leg(l1)
    book.add_leg(l2)
    h1 = Hawb("H-100", "bonded", Weight.measure(120, 240_000, 40),
              ("PVG-FRA", "FRA-ORD"), docs(False),
              shipper="上海生物制剂厂", consignee="Chicago Pharma Co")
    h2 = Hawb("H-101", "bonded", Weight.measure(78, 90_000, 22),
              ("PVG-FRA", "FRA-ORD"), tuple(
                  Doc(d.doc_type, d.reference + "-2", d.checked_at,
                      d.inherited_from) for d in docs(False)),
              shipper="苏州诊断公司", consignee="Midwest Labs")
    book.add_hawb(h1)
    book.add_hawb(h2)
    totals = Weight.measure(198, 330_000, 62)
    book.add_mawb(Mawb("112-98765671", "PVG-FRA", totals, customs_office="上海浦东机场海关"))
    book.add_mawb(Mawb("016-44556621", "FRA-ORD", totals, customs_office="Frankfurt Customs"))
    return book


def scenario_one():
    print("\n=== 场景一：保税中转两段挂单成功 ===")
    book = bonded_book()
    rec = book.assign_chain(
        [("H-100", "112-98765671"), ("H-100", "016-44556621"),
         ("H-101", "112-98765671"), ("H-101", "016-44556621")], NOW)
    print(f"尝试 {rec.attempt_id} {rec.operation} -> {rec.status}")
    print("生成舱单:", ", ".join(rec.manifests))
    totals = book.mawb_totals("112-98765671")
    print(f"主单 112-98765671 分单汇总: 毛重 {totals.gross}kg / "
          f"体积重 {totals.volumetric}kg / 计费重 {totals.chargeable}kg / {totals.pieces} 件")
    print("H-100 航段衔接:", dossier.connection_view(book, "H-100"))
    return book


def scenario_two():
    print("\n=== 场景二：对不上，全部阻断，不写半份单据 ===")
    book = bonded_book()

    # 1) 分单缺海关单证
    bad = Hawb("H-BAD", "bonded", Weight.measure(50, 30_000, 10),
               ("PVG-FRA", "FRA-ORD"),
               (Doc("HOUSE_AWB", "HA", NOW), Doc("PACKING_LIST", "PL", NOW)))
    book.add_hawb(bad)
    rec1 = book.assign_chain([("H-BAD", "112-98765671"), ("H-BAD", "016-44556621")], NOW)
    print(f"[缺单证] {rec1.status}: " + "; ".join(f.code for f in rec1.findings))

    # 2) 主分单重量严重失真（主单报 300kg，分单 198kg）
    heavy = Mawb("112-00000002", "PVG-FRA", Weight.measure(300, 0, 62),
                 customs_office="上海浦东机场海关")
    book.add_mawb(heavy)
    rec2 = book.assign_chain([("H-100", "112-00000002")], NOW)
    print(f"[重量失真+半程] {rec2.status}: " + "; ".join(f.code for f in rec2.findings))

    # 3) 过了截单时间
    late = NOW + timedelta(days=2)      # 航班已截单
    rec3 = book.assign_chain(
        [("H-101", "112-98765671"), ("H-101", "016-44556621")], late)
    print(f"[过截点] {rec3.status}: " + "; ".join(f.code for f in rec3.findings))

    # 账本原样：没有任何分单 booked、没有任何舱单
    print("阻断后 H-101 状态:", book.hawb("H-101").state,
          "| 已生成舱单数:", len(book.manifests),
          "| 挂单关系数:", book.assignment_count())
    return book


def scenario_three():
    print("\n=== 场景三：拆货/并货守恒，失败尝试不留半票 ===")
    book = bonded_book()
    from .validation import SplitChild
    children = (
        SplitChild("H-100-A", Weight.measure(70, 150_000, 25), docs(False)),
        SplitChild("H-100-B", Weight.measure(50, 90_000, 15),
                   tuple(Doc(d.doc_type, d.reference + "-B", d.checked_at)
                         for d in docs(False))),
    )
    rec = book.split("H-100", children, NOW)
    print(f"拆货 -> {rec.status}；H-100={book.hawb('H-100').state}，"
          f"H-100-A 计费重 {book.hawb('H-100-A').weight.chargeable}kg，"
          f"血缘 {book.hawb('H-100-A').derived_from}")

    # 拆货不守恒（70+60=130 != 120）
    bad_children = (
        SplitChild("H-BAD-A", Weight.measure(70, 150_000, 25), docs(False)),
        SplitChild("H-BAD-B", Weight.measure(60, 180_000, 17), docs(False)),
    )
    rec_bad = book.split("H-101", bad_children, NOW)
    print(f"失真拆货 -> {rec_bad.status}: " + "; ".join(
        f.code for f in rec_bad.findings))
    print("H-101 仍是:", book.hawb("H-101").state, "（未产生 H-BAD-A/H-BAD-B）:",
          book.hawb("H-BAD-A") is None, book.hawb("H-BAD-B") is None)

    # 并回两票：H-100-A + H-101 -> H-200（航段一致、同为保税）
    rec_m = book.merge(("H-100-A", "H-101"), "H-200", NOW,
                       consignee="Chicago Consolidated")
    print(f"并货 -> {rec_m.status}；新票 H-200 毛重 "
          f"{book.hawb('H-200').weight.gross}kg / 件数 "
          f"{book.hawb('H-200').weight.pieces}，父票状态: "
          f"{book.hawb('H-100-A').state}, {book.hawb('H-101').state}")
    return book


def main():
    b1 = scenario_one()
    scenario_two()
    scenario_three()
    print("\n全部尝试记录:")
    for book in (b1,):
        for a in book.attempts:
            print(" ", a.attempt_id, a.operation, a.status)


if __name__ == "__main__":
    main()
