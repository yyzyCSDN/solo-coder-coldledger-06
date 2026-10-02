"""空运账本：主分单关系、舱单与"先校验后落账"的原子操作。

每个写操作（挂单 assign / 拆货 split / 并货 merge）的流程一致：
1. 复制当前账本的纯数据状态得到暂存副本（不复制锁，不共享任何可变对象）；
2. 在副本上应用变更并跑全部校验；
3. 出现任一 ERROR -> 丢弃副本，原账本一字不动，只追加一条
   BLOCKED 的 AttemptRecord（含全部原因），绝不留半份单据；
4. 全部通过 -> 用副本状态替换正式账本，追加 COMMITTED 记录；挂单成功
   才为每个主单生成舱单 Manifest。
"""
from __future__ import annotations
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from itertools import count
from threading import RLock

from . import weight as w
from . import validation as v
from .model import AirLeg, Hawb, Mawb, Manifest, ManifestLine

COMMITTED = "COMMITTED"
BLOCKED = "BLOCKED"


@dataclass(frozen=True)
class AttemptRecord:
    attempt_id: str
    operation: str                  # assign_chain / split / merge
    status: str                     # COMMITTED / BLOCKED
    payload: tuple                  # 操作入参（hawb/mawb 标识）
    findings: tuple[v.Finding, ...]
    at: datetime
    actor: str
    manifests: tuple[str, ...] = ()


@dataclass
class _State:
    legs: dict
    mawbs: dict
    hawbs: dict
    assignments: set
    manifests: dict
    mft_seq: iter


class AirCargoBook:
    def __init__(self):
        self._state = _State({}, {}, {}, set(), {}, count(1))
        self.attempts: list[AttemptRecord] = []
        self._att_seq = count(1)
        self._lock = RLock()

    def _snapshot(self) -> _State:
        """纯数据暂存副本：dict/set 浅拷贝即可（其中实体都是 frozen dataclass）。"""
        s = self._state
        return _State(dict(s.legs), dict(s.mawbs), dict(s.hawbs),
                      set(s.assignments), dict(s.manifests), count(next(s.mft_seq)))

    def _install(self, s: _State) -> None:
        self._state = s

    # ---------- 基础登记 ----------
    def add_leg(self, leg: AirLeg) -> AirLeg:
        with self._lock:
            if leg.leg_id in self._state.legs:
                raise ValueError("duplicate leg")
            self._state.legs[leg.leg_id] = leg
            return leg

    def add_mawb(self, mawb: Mawb) -> Mawb:
        with self._lock:
            if mawb.mawb_id in self._state.mawbs:
                raise ValueError("duplicate mawb")
            if mawb.leg_id not in self._state.legs:
                raise ValueError("mawb references unknown leg")
            self._state.mawbs[mawb.mawb_id] = mawb
            return mawb

    def add_hawb(self, hawb: Hawb) -> Hawb:
        with self._lock:
            if hawb.hawb_id in self._state.hawbs:
                raise ValueError("duplicate hawb")
            for leg_id in hawb.leg_ids:
                if leg_id not in self._state.legs:
                    raise ValueError("hawb references unknown leg")
            self._state.hawbs[hawb.hawb_id] = hawb
            return hawb

    # ---------- 查询 ----------
    def leg(self, leg_id: str) -> AirLeg | None:
        return self._state.legs.get(leg_id)

    def mawb(self, mawb_id: str) -> Mawb | None:
        return self._state.mawbs.get(mawb_id)

    def hawb(self, hawb_id: str) -> Hawb | None:
        return self._state.hawbs.get(hawb_id)

    def hawbs_on(self, mawb_id: str) -> list[str]:
        return sorted(h for (h, m) in self._state.assignments if m == mawb_id)

    def mawbs_for(self, hawb_id: str) -> list[str]:
        return sorted(m for (h, m) in self._state.assignments if h == hawb_id)

    def is_assigned(self, hawb_id: str, mawb_id: str) -> bool:
        return (hawb_id, mawb_id) in self._state.assignments

    # ---------- 原子写操作 ----------
    def assign_chain(self, entries, now: datetime, actor: str = "agent") -> AttemptRecord:
        """按航段把分单挂到主单。entries 为 (hawb_id, mawb_id) 列表，
        必须覆盖每个分单的完整航段链，禁止只挂半程。"""
        entries = tuple(entries)
        with self._lock:
            staged = _StagedBook(self, self._snapshot())
            findings = v.validate_assign_chain(staged, entries, now)
            if v.blocked(findings):
                return self._reject("assign_chain", entries, findings, now, actor)
            for hawb_id, mawb_id in entries:
                staged.state.assignments.add((hawb_id, mawb_id))
                staged.state.hawbs[hawb_id] = replace(staged.state.hawbs[hawb_id],
                                                      state="booked")
            manifest_ids = []
            for mawb_id in dict.fromkeys(m for _, m in entries):
                manifest_ids.append(staged.build_manifest(mawb_id, now))
            return self._commit(staged, "assign_chain", entries, findings, now, actor,
                                tuple(manifest_ids))

    def split(self, parent_id: str, children, now: datetime,
              actor: str = "agent") -> AttemptRecord:
        children = tuple(children)
        payload = (parent_id, tuple(c.hawb_id for c in children))
        with self._lock:
            staged = _StagedBook(self, self._snapshot())
            findings = v.validate_split(staged, parent_id, children, now)
            if v.blocked(findings):
                return self._reject("split", payload, findings, now, actor)
            parent = staged.state.hawbs[parent_id]
            staged.state.hawbs[parent_id] = replace(parent, state="split")
            for c in children:
                staged.state.hawbs[c.hawb_id] = Hawb(
                    c.hawb_id, parent.regime, c.weight, parent.leg_ids, tuple(c.docs),
                    state="planned", customs_ref=c.customs_ref,
                    shipper=c.shipper or parent.shipper,
                    consignee=c.consignee or parent.consignee,
                    derived_from=(parent_id,))
            return self._commit(staged, "split", payload, findings, now, actor)

    def merge(self, parent_ids, new_id: str, now: datetime,
              actor: str = "agent", *, shipper: str | None = None,
              consignee: str | None = None) -> AttemptRecord:
        parent_ids = tuple(parent_ids)
        payload = (parent_ids, new_id)
        with self._lock:
            staged = _StagedBook(self, self._snapshot())
            findings = v.validate_merge(
                staged, parent_ids, new_id, now, shipper=shipper, consignee=consignee)
            if v.blocked(findings):
                return self._reject("merge", payload, findings, now, actor)
            parents = [staged.state.hawbs[p] for p in parent_ids]
            merged_weight = w.total(h.weight for h in parents)
            merged_docs = v._merge_docs(parents, new_id, now)
            customs_refs = {h.customs_ref for h in parents if h.customs_ref}
            new_customs_ref = next(iter(customs_refs)) if len(customs_refs) == 1 else None
            staged.state.hawbs[new_id] = Hawb(
                new_id, parents[0].regime, merged_weight, parents[0].leg_ids,
                tuple(merged_docs), state="planned", customs_ref=new_customs_ref,
                shipper=shipper, consignee=consignee, derived_from=parent_ids)
            for p in parent_ids:
                staged.state.hawbs[p] = replace(staged.state.hawbs[p], state="merged")
            return self._commit(staged, "merge", payload, findings, now, actor)

    # ---------- 汇总 ----------
    def mawb_totals(self, mawb_id: str) -> w.Weight:
        return w.total(self._state.hawbs[h].weight
                       for h in self.hawbs_on(mawb_id))

    @property
    def manifests(self) -> dict:
        return self._state.manifests

    def assignment_count(self) -> int:
        return len(self._state.assignments)

    # ---------- 提交/拒绝 ----------
    def _reject(self, operation, payload, findings, now, actor) -> AttemptRecord:
        record = AttemptRecord(
            f"ATT-{next(self._att_seq):04d}", operation, BLOCKED, tuple(payload),
            tuple(findings), now.astimezone(timezone.utc), actor)
        self.attempts.append(record)
        return record

    def _commit(self, staged, operation, payload, findings, now, actor,
                manifests=()) -> AttemptRecord:
        record = AttemptRecord(
            f"ATT-{next(self._att_seq):04d}", operation, COMMITTED, tuple(payload),
            tuple(f for f in findings if f.level == v.WARN),
            now.astimezone(timezone.utc), actor, manifests)
        self.attempts.append(record)
        self._install(staged.state)
        return record


class _StagedBook:
    """校验器眼里的"账本"：既读暂存状态，也能读到尚未落账的尝试上下文。"""

    def __init__(self, owner: AirCargoBook, state: _State):
        self.owner = owner
        self.state = state

    def leg(self, leg_id: str) -> AirLeg | None:
        return self.state.legs.get(leg_id)

    def mawb(self, mawb_id: str) -> Mawb | None:
        return self.state.mawbs.get(mawb_id)

    def hawb(self, hawb_id: str) -> Hawb | None:
        return self.state.hawbs.get(hawb_id)

    def hawbs_on(self, mawb_id: str) -> list[str]:
        return sorted(h for (h, m) in self.state.assignments if m == mawb_id)

    def is_assigned(self, hawb_id: str, mawb_id: str) -> bool:
        return (hawb_id, mawb_id) in self.state.assignments

    def mawb_totals(self, mawb_id: str) -> w.Weight:
        return w.total(self.state.hawbs[h].weight
                       for h in self.hawbs_on(mawb_id))

    def build_manifest(self, mawb_id: str, now: datetime) -> str:
        mawb = self.state.mawbs[mawb_id]
        house_ids = self.hawbs_on(mawb_id)
        lines = tuple(
            ManifestLine(mawb_id, mawb.leg_id, h,
                         self.state.hawbs[h].weight.gross,
                         self.state.hawbs[h].weight.chargeable,
                         self.state.hawbs[h].weight.pieces)
            for h in house_ids
        )
        manifest_id = f"MFT-{mawb_id}-{next(self.state.mft_seq)}"
        self.state.manifests[manifest_id] = Manifest(
            manifest_id, mawb_id, mawb.leg_id, lines, self.mawb_totals(mawb_id),
            now.astimezone(timezone.utc))
        self.state.mawbs[mawb_id] = replace(mawb, state="manifested")
        return manifest_id
