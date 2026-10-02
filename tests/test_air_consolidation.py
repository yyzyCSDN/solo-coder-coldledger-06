import unittest
from datetime import datetime, timezone, timedelta
from decimal import Decimal
from coldledger.shipping.air_consolidation import (
    Piece, HouseWaybill, MasterWaybill, FlightLeg, Allocation, CustomsPacket,
    DocRequirement, TransferPoint, ManifestDraft, ManifestRejection,
    ConsolidationLedger, validate_draft, check_connection, check_customs_timing,
    blocking, chargeable_weight, split_house, merge_allocations,
    house_totals, master_totals, ERROR,
)

UTC = timezone.utc
D = Decimal


def dt(y, m, d, h):
    return datetime(y, m, d, h, tzinfo=UTC)


PIECES_A = (
    Piece("A-1", D("100"), D("0.5")),
    Piece("A-2", D("80"), D("0.4")),
)
HOUSE_A = HouseWaybill("HAWB-A", "PVG", "FRA", PIECES_A, D("180"), D("0.9"))
HOUSE_B = HouseWaybill("HAWB-B", "PVG", "FRA",
                       (Piece("B-1", D("120"), D("0.6")),), D("120"), D("0.6"))

MASTER = MasterWaybill("MAWB-1", "LH", "LH729", D("300"), D("1.5"))

DOC_REQS = (
    DocRequirement("house", "commercial_invoice"),
    DocRequirement("house", "packing_list"),
    DocRequirement("master", "manifest"),
    DocRequirement("customs", "export_declaration", leg_seq=1),
)

HOUSE_DOCS = {
    "HAWB-A": frozenset({"commercial_invoice", "packing_list"}),
    "HAWB-B": frozenset({"commercial_invoice", "packing_list"}),
}
MASTER_DOCS = {"MAWB-1": frozenset({"manifest"})}


def alloc(a, master, pid, w, v):
    return Allocation(a, master, pid, D(w), D(v))


def good_draft_leg1():
    rows = (
        alloc("HAWB-A", "MAWB-1", "A-1", "100", "0.5"),
        alloc("HAWB-A", "MAWB-1", "A-2", "80", "0.4"),
        alloc("HAWB-B", "MAWB-1", "B-1", "120", "0.6"),
    )
    return ManifestDraft(1, 0, (MASTER,), rows, HOUSE_DOCS, MASTER_DOCS,
                         CustomsPacket(1, frozenset({"export_declaration"}), dt(2026, 1, 1, 6)))


class AirConsolidationTests(unittest.TestCase):
    def test_chargeable_weight(self):
        self.assertEqual(chargeable_weight(D("100"), D("1")), D("167.000"))
        self.assertEqual(chargeable_weight(D("200"), D("1")), D("200"))

    def test_happy_merge_commits_and_aggregates(self):
        ledger = ConsolidationLedger()
        m = ledger.commit(good_draft_leg1(), [HOUSE_A, HOUSE_B], DOC_REQS)
        self.assertEqual(m.revision, 1)
        self.assertEqual(master_totals(m.allocations)["MAWB-1"]["gross"], D("300"))
        self.assertEqual(house_totals(m.allocations)["HAWB-A"]["volume"], D("0.9"))

    def test_weight_distortion_after_repack_is_rejected(self):
        # 并货后过磅重量被改成 290（丢了 10kg），主单申报却仍是 300
        draft = good_draft_leg1()
        bad_rows = (
            alloc("HAWB-A", "MAWB-1", "A-1", "100", "0.5"),
            alloc("HAWB-A", "MAWB-1", "A-2", "70", "0.4"),
            alloc("HAWB-B", "MAWB-1", "B-1", "120", "0.6"),
        )
        draft = ManifestDraft(1, 0, (MASTER,), bad_rows, HOUSE_DOCS, MASTER_DOCS,
                              draft.customs)
        ledger = ConsolidationLedger()
        with self.assertRaises(ManifestRejection) as cm:
            ledger.commit(draft, [HOUSE_A, HOUSE_B], DOC_REQS)
        reasons = cm.exception.findings
        cats = {(f.subject, f.category) for f in reasons}
        self.assertIn(("HAWB-A", "weight"), cats)
        self.assertIn(("MAWB-1", "weight"), cats)
        self.assertNotIn(1, ledger.manifests)  # 半份单据不得落账
        self.assertEqual(len(ledger.rejections), 1)

    def test_duplicate_piece_in_same_leg_is_error(self):
        rows = (
            alloc("HAWB-A", "MAWB-1", "A-1", "100", "0.5"),
            alloc("HAWB-A", "MAWB-1", "A-1", "100", "0.5"),  # 同件重复
            alloc("HAWB-B", "MAWB-1", "B-1", "120", "0.6"),
        )
        master = MasterWaybill("MAWB-1", "LH", "LH729", D("320"), D("1.6"))
        draft = ManifestDraft(1, 0, (master,), rows, HOUSE_DOCS, MASTER_DOCS, None)
        findings = validate_draft(draft, [HOUSE_A, HOUSE_B])
        self.assertTrue(any(f.category == "structure" and "重复" in f.reason for f in findings))

    def test_missing_customs_document_blocks_commit(self):
        draft = good_draft_leg1()
        draft = ManifestDraft(1, 0, draft.masters, draft.allocations,
                              HOUSE_DOCS, MASTER_DOCS,
                              CustomsPacket(1, frozenset(), dt(2026, 1, 1, 6)))
        ledger = ConsolidationLedger()
        with self.assertRaises(ManifestRejection) as cm:
            ledger.commit(draft, [HOUSE_A, HOUSE_B], DOC_REQS)
        self.assertTrue(any(f.category == "customs" for f in cm.exception.findings))
        self.assertEqual(ledger.manifests, {})

    def test_empty_master_is_rejected(self):
        draft = ManifestDraft(1, 0, (MASTER,), (), {}, {}, None)
        findings = validate_draft(draft, [HOUSE_A, HOUSE_B])
        self.assertTrue(any(f.subject == "MAWB-1" and f.category == "structure"
                            for f in findings))

    def test_split_house_conserves_weight_across_legs(self):
        # HAWB-A 拆到两段：A-1 随 MAWB-1（PVG-FRA），A-2 随 MAWB-2（FRA-ORD）
        first, second = split_house(HOUSE_A, "MAWB-1", "MAWB-2", frozenset({"A-1"}))
        self.assertEqual(sum((a.weight_kg for a in first + second), D("0")), D("180"))
        m1 = MasterWaybill("MAWB-1", "LH", "LH729", D("100"), D("0.5"))
        m2 = MasterWaybill("MAWB-2", "LH", "LH400", D("80"), D("0.4"))
        d1 = ManifestDraft(1, 0, (m1,), first,
                           {"HAWB-A": frozenset({"commercial_invoice", "packing_list"})},
                           {"MAWB-1": frozenset({"manifest"})},
                           CustomsPacket(1, frozenset({"export_declaration"}), dt(2026, 1, 1, 6)))
        d2 = ManifestDraft(2, 0, (m2,), second,
                           {"HAWB-A": frozenset({"commercial_invoice", "packing_list"})},
                           {"MAWB-2": frozenset({"manifest"})},
                           CustomsPacket(2, frozenset({"import_declaration"}), dt(2026, 1, 2, 10)))
        f1 = validate_draft(d1, [HOUSE_A], DOC_REQS)
        f2 = validate_draft(d2, [HOUSE_A])  # 第二段不要求出口报关
        self.assertFalse(blocking(f1 + f2), [str(x) for x in f1 + f2])

    def test_mct_transfer_connection(self):
        leg1 = FlightLeg(1, "PVG", "FRA", dt(2026, 1, 1, 8), dt(2026, 1, 1, 14))
        leg2_tight = FlightLeg(2, "FRA", "ORD", dt(2026, 1, 1, 15), dt(2026, 1, 1, 19))
        leg2_ok = FlightLeg(2, "FRA", "ORD", dt(2026, 1, 1, 20), dt(2026, 1, 2, 0))
        point = TransferPoint("FRA", timedelta(hours=3), timedelta(hours=2),
                              customs_required=True)
        # 14:00 到、15:00 飞：只有 1h < 5h
        f = check_connection(leg1, leg2_tight, {"FRA": point})
        self.assertEqual(f.severity, ERROR)
        # 14:00 到、20:00 飞：6h >= 5h
        self.assertIsNone(check_connection(leg1, leg2_ok, {"FRA": point}))

    def test_customs_must_clear_before_departure(self):
        leg = FlightLeg(1, "PVG", "FRA", dt(2026, 1, 1, 8), dt(2026, 1, 1, 14))
        late = (CustomsPacket(1, frozenset({"x"}), dt(2026, 1, 1, 10)),)
        uncleared = (CustomsPacket(1, frozenset({"x"}), None),)
        self.assertTrue(blocking(check_customs_timing([leg], late)))
        self.assertTrue(blocking(check_customs_timing([leg], uncleared)))

    def test_chain_commit_is_atomic(self):
        # 第一段合法，第二段重量失真 -> 整链拒绝，两段都不能落账
        first, second = split_house(HOUSE_A, "MAWB-1", "MAWB-2", frozenset({"A-1"}))
        m1 = MasterWaybill("MAWB-1", "LH", "LH729", D("100"), D("0.5"))
        m2 = MasterWaybill("MAWB-2", "LH", "LH400", D("99"), D("0.4"))  # 应为 80
        d1 = ManifestDraft(1, 0, (m1,), first,
                           {"HAWB-A": frozenset({"commercial_invoice", "packing_list"})},
                           {"MAWB-1": frozenset({"manifest"})},
                           CustomsPacket(1, frozenset(), dt(2026, 1, 1, 6)))
        d2 = ManifestDraft(2, 0, (m2,), second,
                           {"HAWB-A": frozenset({"commercial_invoice", "packing_list"})},
                           {"MAWB-2": frozenset({"manifest"})},
                           CustomsPacket(2, frozenset(), dt(2026, 1, 2, 10)))
        legs = [
            FlightLeg(1, "PVG", "FRA", dt(2026, 1, 1, 8), dt(2026, 1, 1, 14)),
            FlightLeg(2, "FRA", "ORD", dt(2026, 1, 1, 20), dt(2026, 1, 2, 0)),
        ]
        points = {"FRA": TransferPoint("FRA", timedelta(hours=3))}
        ledger = ConsolidationLedger()
        with self.assertRaises(ManifestRejection):
            ledger.commit_chain([d1, d2], [HOUSE_A], legs, points)
        self.assertEqual(ledger.manifests, {})

    def test_chain_commit_success_and_optimistic_lock(self):
        first, second = split_house(HOUSE_A, "MAWB-1", "MAWB-2", frozenset({"A-1"}))
        m1 = MasterWaybill("MAWB-1", "LH", "LH729", D("100"), D("0.5"))
        m2 = MasterWaybill("MAWB-2", "LH", "LH400", D("80"), D("0.4"))
        packets = [
            CustomsPacket(1, frozenset({"export_declaration"}), dt(2026, 1, 1, 6)),
            CustomsPacket(2, frozenset({"import_declaration"}), dt(2026, 1, 1, 18)),
        ]
        d1 = ManifestDraft(1, 0, (m1,), first,
                           {"HAWB-A": frozenset({"commercial_invoice", "packing_list"})},
                           {"MAWB-1": frozenset({"manifest"})}, packets[0])
        d2 = ManifestDraft(2, 0, (m2,), second,
                           {"HAWB-A": frozenset({"commercial_invoice", "packing_list"})},
                           {"MAWB-2": frozenset({"manifest"})}, packets[1])
        legs = [
            FlightLeg(1, "PVG", "FRA", dt(2026, 1, 1, 8), dt(2026, 1, 1, 14)),
            FlightLeg(2, "FRA", "ORD", dt(2026, 1, 1, 20), dt(2026, 1, 2, 0)),
        ]
        points = {"FRA": TransferPoint("FRA", timedelta(hours=3))}
        ledger = ConsolidationLedger()
        committed = ledger.commit_chain([d1, d2], [HOUSE_A], legs, points, packets)
        self.assertEqual([c.leg_seq for c in committed], [1, 2])

        # 用过期 revision 重提第一段 -> 乐观锁冲突
        stale = ManifestDraft(1, 0, (m1,), first, d1.house_docs, d1.master_docs, packets[0])
        with self.assertRaises(ManifestRejection) as cm:
            ledger.commit_chain([stale], [HOUSE_A], legs[:1], points)
        self.assertTrue(any("乐观锁" in f.reason for f in cm.exception.findings))

    def test_merge_helper(self):
        rows = merge_allocations(
            [alloc("HAWB-A", "MAWB-9", "A-1", "100", "0.5")],
            [alloc("HAWB-B", "MAWB-9", "B-1", "120", "0.6")],
        )
        self.assertEqual(len(rows), 2)


if __name__ == "__main__":
    unittest.main()
