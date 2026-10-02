# ColdLedger Pharmaceutical Cold-Chain Operations Lab

Dependency-free Python reference implementation for temperature-sensitive pharmaceutical distribution. It models inventory expiry, chain-of-custody, telemetry correction, packaging qualification, dry-ice depletion, freezer allocation, customs holds, serialization, recalls, release decisions, vehicle compartment compatibility, returns, SLA calendars and cross-dock state.

Runtime third-party dependencies: none.

## Air freight — master/house airway-bill ledger (`coldledger.airfreight`)

Models MAWB/HAWB relations **per flight leg** for multi-leg air shipments and guards
against documents, weights and connections drifting apart after split/merge handling.

- `model.py` — `AirLeg`, `Mawb`, `Hawb`, `Doc` and `Manifest`; a MAWB is issued per
  leg, a house bill covers a contiguous leg chain (e.g. PVG→FRA→ORD transfer).
- `weight.py` — Decimal weight/volume math: gross weight is kept as measured,
  volumetric weight uses the IATA 6000 cm³/kg factor, chargeable weight is
  `max(gross, volumetric)` rounded **up** to 0.5 kg. Totals recompute the
  chargeable weight instead of summing rounded values. Master-vs-house tolerance
  is `max(0.5 kg, 0.5%)`.
- `validation.py` — all checks as `Finding(ERROR|WARN, code, message, refs)`:
  leg continuity and minimum transfer slack (default 120 min), transfer-point
  match, required/checked documents by regime (`general` / `bonded`),
  declaration-reference match, doc/customs/ramp cutoffs, regime mixing,
  bonded customs office, master-vs-house gross/chargeable/piece reconciliation,
  and strict weight/volume/piece conservation for splits/merges.
- `book.py` — `AirCargoBook` with atomic `assign_chain` / `split` / `merge`:
  changes are applied to a staged data copy and every rule must pass before
  commit. On any ERROR the live ledger is untouched (no half-written bill, no
  half-covered leg chain) and a `BLOCKED` `AttemptRecord` preserves every
  reason. Manifests are generated only on commit.
- `dossier.py` — master-bill dossier serialization (relations, documents,
  weight/volume totals, connection slack, findings, manifest lines) with
  temp-file + `os.replace` atomic export.
- `demo.py` — end-to-end scenarios: successful bonded two-leg assignment,
  blocked mismatch cases, and weight-conserving split/merge:
  `python -m coldledger.airfreight.demo` (with `src` on `PYTHONPATH`).

空运主分单（中文速览）：按航段建立 MAWB/HAWB 关系；一票分单的全部航段必须
一次性挂全（`HAWB_CHAIN_INCOMPLETE` 阻断半程挂单）；挂单前校验单证齐套与审核、
海关截点、中转点与最短衔接时间、监管模式一致性以及主分单重量体积件数汇总；
拆并货要求重量/体积/件数严格守恒并继承单证血缘。任何硬性校验不过，操作整体
不落账，只保留带原因代码的 BLOCKED 尝试记录。

Tests: `PYTHONPATH=src python3 -m unittest discover -s tests`

