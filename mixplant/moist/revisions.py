"""Revision history of a batch's moisture compensated plan.

A retroactive migration never mutates history in place: every recalculation
appends a new immutable revision and keeps the revision it replaced.  The
pre migration version (revision 1, and every later one) stays queryable so an
auditor can show exactly what changed and what water was corrected.

Revisions are content addressed by the moisture map they were calculated
with.  Replaying the same migration therefore produces the same revision id
and collapses to no-op, which is the idempotency guarantee behind "the late
data补算 must not deduct water twice".
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping

from ..errors import ValidationError
from ..recipe.plan import BatchPlan


@dataclass(frozen=True, slots=True)
class PlanRevision:
    """One immutable moisture compensated version of a batch plan."""

    revision: int
    plan: BatchPlan
    basis: str  # "declared" | "migrated"
    source_reading_ids: tuple[str, ...]
    water_delta_l: float
    tick: int

    @property
    def free_water_l(self) -> float:
        return self.plan.free_water_l

    def describe(self) -> dict[str, Any]:
        return {
            "revision": self.revision,
            "basis": self.basis,
            "source_reading_ids": list(self.source_reading_ids),
            "water_delta_l": self.water_delta_l,
            "tick": self.tick,
            "free_water_l": self.plan.free_water_l,
            "moisture_per_material": dict(self.plan.moisture_per_material),
            "materials": dict(self.plan.materials),
        }


def revision_key(moisture_per_material: Mapping[str, float]) -> tuple[tuple[str, float], ...]:
    return tuple(sorted((str(m), round(float(p), 6)) for m, p in moisture_per_material.items()))


class BatchRevisionHistory:
    """Keeps every plan revision of one batch, oldest first."""

    __slots__ = ("batch_id", "_revisions", "_keys")

    def __init__(self, batch_id: str) -> None:
        if not batch_id:
            raise ValidationError("a revision history needs a batch identity")
        self.batch_id = batch_id
        self._revisions: list[PlanRevision] = []
        self._keys: set[tuple[tuple[str, float], ...]] = set()

    def seed(self, plan: BatchPlan, *, tick: int) -> PlanRevision:
        """Record revision 1, the plan the batch was declared with."""

        if self._revisions:
            raise ValidationError("the declared revision already exists", batch_id=self.batch_id)
        revision = PlanRevision(
            revision=1,
            plan=plan,
            basis="declared",
            source_reading_ids=(),
            water_delta_l=0.0,
            tick=tick,
        )
        self._revisions.append(revision)
        self._keys.add(revision_key(plan.moisture_per_material))
        return revision

    def add_migration(
        self,
        plan: BatchPlan,
        *,
        source_reading_ids: tuple[str, ...],
        tick: int,
    ) -> tuple[PlanRevision, bool]:
        """Append a recalculated revision; collapse to no-op on repeat.

        Returns the revision and whether it was newly added.  An identical
        moisture map already on file returns the existing revision with
        ``added=False`` so replayed/duplicate migrations deduct nothing.
        """

        key = revision_key(plan.moisture_per_material)
        for existing in self._revisions:
            if revision_key(existing.plan.moisture_per_material) == key:
                return existing, False
        previous = self.latest
        delta = round(plan.free_water_l - previous.free_water_l, 4)
        revision = PlanRevision(
            revision=previous.revision + 1,
            plan=plan,
            basis="migrated",
            source_reading_ids=tuple(sorted(source_reading_ids)),
            water_delta_l=delta,
            tick=tick,
        )
        self._revisions.append(revision)
        self._keys.add(key)
        return revision, True

    def adopt(self, revision: PlanRevision) -> None:
        """Adopt a revision read back from the stream after a restart."""

        key = revision_key(revision.plan.moisture_per_material)
        if key in self._keys:
            return
        self._revisions.append(revision)
        self._keys.add(key)
        self._revisions.sort(key=lambda item: item.revision)

    @property
    def latest(self) -> PlanRevision:
        if not self._revisions:
            raise ValidationError("the batch has no plan revision yet", batch_id=self.batch_id)
        return self._revisions[-1]

    def at(self, revision: int) -> PlanRevision:
        for item in self._revisions:
            if item.revision == revision:
                return item
        raise ValidationError("unknown plan revision", batch_id=self.batch_id, revision=revision)

    def revisions(self) -> tuple[PlanRevision, ...]:
        return tuple(self._revisions)

    def cumulative_water_delta_l(self) -> float:
        return round(sum(item.water_delta_l for item in self._revisions if item.basis == "migrated"), 4)

    def describe(self) -> dict[str, Any]:
        return {
            "batch_id": self.batch_id,
            "latest_revision": self.latest.revision if self._revisions else 0,
            "cumulative_water_delta_l": self.cumulative_water_delta_l(),
            "revisions": [item.describe() for item in self._revisions],
        }


class RevisionRegistry:
    """Every batch's revision history in one place."""

    __slots__ = ("_histories",)

    def __init__(self) -> None:
        self._histories: dict[str, BatchRevisionHistory] = {}

    def for_batch(self, batch_id: str) -> BatchRevisionHistory:
        history = self._histories.get(batch_id)
        if history is None:
            history = BatchRevisionHistory(batch_id)
            self._histories[batch_id] = history
        return history

    def has(self, batch_id: str) -> bool:
        return batch_id in self._histories

    def batch_ids(self) -> tuple[str, ...]:
        return tuple(sorted(self._histories))

    def describe_batch(self, batch_id: str) -> dict[str, Any]:
        return self.for_batch(batch_id).describe()
