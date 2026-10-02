import unittest
from datetime import datetime, timezone, timedelta
from decimal import Decimal

from coldledger.airfreight.book import AirCargoBook, COMMITTED, BLOCKED
from coldledger.airfreight.model import AirLeg, Doc, Hawb, Mawb
from coldledger.airfreight.weight import Weight, charge_round, volumetric_weight, total
from coldledger.airfreight.validation import SplitChild
from coldledger.airfreight import dossier

UTC = timezone.utc
NOW = datetime(2026, 10, 2, 6, 0, tzinfo=UTC)


def bonded_docs(tag=""):
    at = NOW - timedelta(days=1)
    return (Doc("HOUSE_AWB", "HA" + tag, at),
            Doc("COMMERCIAL_INVOICE", "INV" + tag, at),
            Doc("PACKING_LIST", "PL" + tag, at),
            Doc("BONDED_MANIFEST", "BM" + tag, at),
            Doc("CUSTOMS_TRANSIT_DECLARATION", "CTD" + tag, at))


def make_book():
    book = AirCargoBook()
    book.add_leg(AirLeg("L1", "CA", "CA1", "PVG", "FRA",
                        datetime(2026, 10, 2, 13, tzinfo=UTC),
                        datetime(2026, 10, 2, 19, 30, tzinfo=UTC)))
    book.add_leg(AirLeg("L2", "UA", "UA2", "FRA", "ORD",
                        datetime(2026, 10, 3, 0, 30, tzinfo=UTC),
                        datetime(2026, 10, 3, 4, tzinfo=UTC)))
    book.add_hawb(Hawb("H1", "bonded", Weight.measure(120, 240_000, 40),
                       ("L1", "L2"), bonded_docs()))
    book.add_hawb(Hawb("H2", "bonded", Weight.measure(80, 120_000, 22),
                       ("L1", "L2"), bonded_docs("2")))
    w = Weight.measure(200, 360_000, 62)
    book.add_mawb(Mawb("M1", "L1", w, customs_office="上海海关"))
    book.add_mawb(Mawb("M2", "L2", w, customs_office="FRA Customs"))
    return book


class WeightTests(unittest.TestCase):
    def test_charge_round_half_kg_up(self):
        self.assertEqual(charge_round(Decimal("0")), Decimal("0.0"))
        self.assertEqual(charge_round(Decimal("0.2")), Decimal("0.5"))
        self.assertEqual(charge_round(Decimal("0.5")), Decimal("0.5"))
        self.assertEqual(charge_round(Decimal("0.6")), Decimal("1.0"))

    def test_volumetric_weight(self):
        self.assertEqual(volumetric_weight(360_000), Decimal("60.000"))

    def test_total_recomputes_chargeable(self):
        # 毛重保留测量值，只有计费重按 0.5kg 向上取整
        a = Weight.measure("0.2", 0, 1)
        b = Weight.measure("0.2", 0, 1)
        self.assertEqual(a.gross, Decimal("0.200"))
        self.assertEqual(a.chargeable, Decimal("0.5"))
        # 汇总毛重 0.4kg -> 计费 0.5kg（而非 0.5+0.5=1.0 直接相加）
        self.assertEqual(total([a, b]).chargeable, Decimal("0.5"))
        self.assertEqual(total([a, b]).pieces, 2)
        # 体积重驱动：单件毛重 1kg 但体积重 1.3kg -> 计费 1.5kg；
        # 合计毛重 2kg、体积重 2.6kg -> 计费 3.0kg
        c = Weight.measure(1, 7800, 1)
        self.assertEqual(c.chargeable, Decimal("1.5"))
        self.assertEqual(total([c, c]).chargeable, Decimal("3.0"))


class AssignTests(unittest.TestCase):
    def test_commit_full_chain_and_manifest(self):
        book = make_book()
        rec = book.assign_chain([("H1", "M1"), ("H1", "M2"),
                                 ("H2", "M1"), ("H2", "M2")], NOW)
        self.assertEqual(rec.status, COMMITTED)
        self.assertEqual(book.hawb("H1").state, "booked")
        self.assertEqual(book.mawbs_for("H1"), ["M1", "M2"])
        self.assertEqual(len(rec.manifests), 2)
        self.assertEqual(book.mawb("M1").state, "manifested")
        totals = book.mawb_totals("M1")
        self.assertEqual((totals.gross, totals.pieces), (Decimal("200.0"), 62))

    def test_half_chain_is_blocked_and_leaves_no_trace_in_ledger(self):
        book = make_book()
        rec = book.assign_chain([("H1", "M1")], NOW)
        self.assertEqual(rec.status, BLOCKED)
        self.assertIn("HAWB_CHAIN_INCOMPLETE", [f.code for f in rec.findings])
        self.assertEqual(book.hawb("H1").state, "planned")
        self.assertEqual(book.assignment_count(), 0)
        self.assertEqual(len(book.manifests), 0)
        self.assertEqual(book.mawb("M1").state, "open")

    def test_weight_mismatch_blocked(self):
        book = make_book()
        book.add_mawb(Mawb("MX", "L1", Weight.measure(260, 0, 62),
                           customs_office="上海海关"))
        rec = book.assign_chain([("H1", "MX"), ("H1", "M2")], NOW)
        self.assertEqual(rec.status, BLOCKED)
        self.assertTrue(any(f.code == "WEIGHT_MISMATCH" for f in rec.findings))
        self.assertEqual(book.assignment_count(), 0)

    def test_missing_doc_blocked(self):
        book = make_book()
        book.add_hawb(Hawb("H9", "bonded", Weight.measure(10, 0, 2),
                           ("L1", "L2"), (Doc("HOUSE_AWB", "x", NOW),)))
        rec = book.assign_chain([("H9", "M1"), ("H9", "M2")], NOW)
        self.assertEqual(rec.status, BLOCKED)
        codes = {f.code for f in rec.findings}
        self.assertIn("DOC_MISSING", codes)

    def test_unchecked_doc_and_cutoff_blocked(self):
        book = AirCargoBook()
        depart = NOW + timedelta(minutes=60)
        book.add_leg(AirLeg("S1", "CA", "CA1", "A", "B", depart,
                            depart + timedelta(hours=10)))
        at = depart - timedelta(minutes=200)
        book.add_hawb(Hawb("G1", "general", Weight.measure(10, 0, 1), ("S1",), (
            Doc("HOUSE_AWB", "h", at), Doc("COMMERCIAL_INVOICE", "i", at),
            Doc("PACKING_LIST", "p", at), Doc("CUSTOMS_DECLARATION", "c", at))))
        book.add_mawb(Mawb("MG", "S1", Weight.measure(10, 0, 1)))
        # 单证未审核
        h = book.hawb("G1")
        book._state.hawbs["G1"] = Hawb("G1", "general", h.weight, h.leg_ids,
                                       tuple(Doc(d.doc_type, d.reference, None)
                                             for d in h.docs))
        rec = book.assign_chain([("G1", "MG")], NOW)
        self.assertIn("DOC_UNCHECKED", {f.code for f in rec.findings})
        # 过了海关截点（240 分钟）
        rec2 = book.assign_chain([("G1", "MG")], depart - timedelta(minutes=60))
        self.assertIn("CUSTOMS_CUTOFF", {f.code for f in rec2.findings})

    def test_tight_connection_blocked(self):
        book = AirCargoBook()
        book.add_leg(AirLeg("C1", "CA", "1", "PVG", "FRA",
                            datetime(2026, 10, 2, 12, tzinfo=UTC),
                            datetime(2026, 10, 2, 20, tzinfo=UTC)))
        book.add_leg(AirLeg("C2", "UA", "2", "FRA", "ORD",
                            datetime(2026, 10, 2, 21, tzinfo=UTC),
                            datetime(2026, 10, 3, 1, tzinfo=UTC)))  # 仅 1h
        book.add_hawb(Hawb("T1", "general", Weight.measure(10, 0, 1),
                           ("C1", "C2")))
        book.add_mawb(Mawb("MC1", "C1", Weight.measure(10, 0, 1)))
        book.add_mawb(Mawb("MC2", "C2", Weight.measure(10, 0, 1)))
        rec = book.assign_chain([("T1", "MC1"), ("T1", "MC2")], NOW)
        self.assertIn("CONNECTION_TOO_TIGHT", {f.code for f in rec.findings})

    def test_broken_transfer_point_blocked(self):
        book = AirCargoBook()
        book.add_leg(AirLeg("B1", "CA", "1", "PVG", "FRA",
                            datetime(2026, 10, 2, 12, tzinfo=UTC),
                            datetime(2026, 10, 2, 18, tzinfo=UTC)))
        book.add_leg(AirLeg("B2", "UA", "2", "AMS", "ORD",
                            datetime(2026, 10, 3, 0, tzinfo=UTC),
                            datetime(2026, 10, 3, 4, tzinfo=UTC)))
        book.add_hawb(Hawb("T2", "general", Weight.measure(10, 0, 1),
                           ("B1", "B2")))
        book.add_mawb(Mawb("MB1", "B1", Weight.measure(10, 0, 1)))
        book.add_mawb(Mawb("MB2", "B2", Weight.measure(10, 0, 1)))
        rec = book.assign_chain([("T2", "MB1"), ("T2", "MB2")], NOW)
        self.assertIn("CONNECTION_MISMATCH", {f.code for f in rec.findings})

    def test_regime_mix_blocked(self):
        book = make_book()
        book.add_hawb(Hawb("H3", "general", Weight.measure(5, 0, 1),
                           ("L1", "L2")))
        rec = book.assign_chain([("H1", "M1"), ("H1", "M2"),
                                 ("H3", "M1"), ("H3", "M2")], NOW)
        self.assertIn("REGIME_MIX", {f.code for f in rec.findings})

    def test_bonded_requires_customs_office(self):
        book = make_book()
        book.add_mawb(Mawb("MZ", "L1", Weight.measure(5, 0, 1)))
        book.add_hawb(Hawb("HZ", "bonded", Weight.measure(5, 0, 1),
                           ("L1", "L2"), bonded_docs("z")))
        rec = book.assign_chain([("HZ", "MZ"), ("HZ", "M2")], NOW)
        self.assertIn("BONDED_OFFICE", {f.code for f in rec.findings})


class SplitMergeTests(unittest.TestCase):
    def test_split_conserves_weight_and_docs(self):
        book = make_book()
        rec = book.split("H1", (
            SplitChild("H1A", Weight.measure(70, 150_000, 25), bonded_docs("a")),
            SplitChild("H1B", Weight.measure(50, 90_000, 15), bonded_docs("b")),
        ), NOW)
        self.assertEqual(rec.status, COMMITTED)
        self.assertEqual(book.hawb("H1").state, "split")
        self.assertEqual(book.hawb("H1A").derived_from, ("H1",))
        agg = total([book.hawb("H1A").weight, book.hawb("H1B").weight])
        self.assertEqual(agg.gross, Decimal("120.0"))
        self.assertEqual(agg.pieces, 40)

    def test_split_non_conserving_is_blocked(self):
        book = make_book()
        rec = book.split("H1", (
            SplitChild("X1", Weight.measure(70, 150_000, 25), bonded_docs("x")),
            SplitChild("X2", Weight.measure(60, 90_000, 15), bonded_docs("y")),
        ), NOW)
        self.assertEqual(rec.status, BLOCKED)
        self.assertTrue(any(f.code.startswith("SPLIT_") for f in rec.findings))
        self.assertIsNone(book.hawb("X1"))
        self.assertIsNone(book.hawb("X2"))
        self.assertEqual(book.hawb("H1").state, "planned")

    def test_split_booked_hawb_blocked(self):
        book = make_book()
        book.assign_chain([("H1", "M1"), ("H1", "M2"),
                           ("H2", "M1"), ("H2", "M2")], NOW)
        rec = book.split("H1", (
            SplitChild("H1A", Weight.measure(70, 150_000, 25), bonded_docs("a")),
            SplitChild("H1B", Weight.measure(50, 90_000, 15), bonded_docs("b")),
        ), NOW)
        self.assertEqual(rec.status, BLOCKED)
        self.assertIn("HAWB_NOT_SPLITTABLE", {f.code for f in rec.findings})

    def test_merge_combines_and_marks_parents(self):
        book = make_book()
        rec = book.merge(("H1", "H2"), "H3", NOW)
        self.assertEqual(rec.status, COMMITTED)
        merged = book.hawb("H3")
        self.assertEqual(merged.weight.gross, Decimal("200.0"))
        self.assertEqual(merged.weight.pieces, 62)
        self.assertEqual(merged.derived_from, ("H1", "H2"))
        self.assertEqual(book.hawb("H1").state, "merged")
        self.assertEqual(merged.doc("HOUSE_AWB").checked_at, NOW.astimezone(UTC))

    def test_merge_different_routes_blocked(self):
        book = make_book()
        book.add_leg(AirLeg("L3", "CA", "9", "PVG", "NRT",
                            datetime(2026, 10, 2, 12, tzinfo=UTC),
                            datetime(2026, 10, 2, 16, tzinfo=UTC)))
        book.add_hawb(Hawb("HX", "bonded", Weight.measure(5, 0, 1),
                           ("L3",), bonded_docs("x")))
        rec = book.merge(("H1", "HX"), "HY", NOW)
        self.assertEqual(rec.status, BLOCKED)
        self.assertIn("MERGE_ROUTE", {f.code for f in rec.findings})


class DossierTests(unittest.TestCase):
    def test_dossier_and_atomic_export(self):
        import json, os, tempfile
        book = make_book()
        book.assign_chain([("H1", "M1"), ("H1", "M2"),
                           ("H2", "M1"), ("H2", "M2")], NOW)
        payload = dossier.mawb_dossier(book, "M1")
        self.assertEqual(payload["declared_vs_total"]["pieces_delta"], 0)
        self.assertEqual(payload["connections"]["H1"][0]["transfer_at"], "FRA")
        self.assertEqual(len(payload["manifests"]), 1)
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "nested", "dossier.json")
            dossier.export_atomic(path, payload)
            with open(path, encoding="utf-8") as fh:
                loaded = json.load(fh)
            self.assertEqual(loaded["mawb"]["mawb_id"], "M1")


if __name__ == "__main__":
    unittest.main()
