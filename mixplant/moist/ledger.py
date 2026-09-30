"""Versioned per batch recomputation and the water adjustment ledger.

A retroactive reading is a **migration**: every still open batch it should
have been in force for is recomputed batch by batch under a fresh revision,
while the previous revision is retained.  The two invariants this module
guards are:

* recomputation is idempotent on the effective reading basis – refiling a
  reading, replaying the stream or restarting the station never creates a
  second revision for the same moisture basis and never deducts its water
  twice;
* every water correction flows into one take stock ledger so the recipe
  revisions, the stock figures and the audit migration trail can be reconciled.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Any, Mapping

from ..errors import NotFoundError
from ..recipe.plan import BatchPlan, plan_batch
from ..recipe.recipe import Recipe
from .reading import MoistureReading, effective_reading, readings_signature

# Percentage used for a wet aggregate a probe has never reported.
FALLBACK_REF_PERCENT: float | None = None


class SkipReason(str, Enum):
    """Why a late reading was not traced back to a batch."""

    POLICY_NEW_BATCHES_ONLY = "policy_new_batches_only"
    BATCH_ALREADY_DROPPED = "batch_already_dropped"
    ALREADY_CURRENT = "already_current"


@dataclass(frozen=True, slots=True)
class WaterAdjustment:
    """The batching water one revision changes, fed into the take stock."""

    batch_id: str
    revision: int
    reading_id: str
    material: str
    delta_l: float

    def describe(self) -> dict[str, Any]:
        return {
            "batch_id": self.batch_id,
            "revision": self.revision,
            "reading_id": self.reading_id,
            "material": self.material,
            "delta_l": self.delta_l,
        }

    @classmethod
    def from_document(cls, document: Mapping[str, Any]) -> "WaterAdjustment":
        return cls(
            batch_id=str(document["batch_id"]),
            revision=int(document["revision"]),
            reading_id=str(document["reading_id"]),
            material=str(document["material"]),
            delta_l=float(document["delta_l"]),
        )


@dataclass(frozen=True, slots=True)
class BatchRevision:
    """One moisture compensated version of one batch."""

    batch_id: str
    revision: int
    plan: BatchPlan
    basis: str
    reading_ids: tuple[str, ...]
    tick: int
    superseded_by: int | None = None

    @property
    def superseded(self) -> bool:
        return self.superseded_by is not None

    @property
    def free_water_l(self) -> float:
        return self.plan.free_water_l

    def describe(self) -> dict[str, Any]:
        return {
            "batch_id": self.batch_id,
            "revision": self.revision,
            "superseded": self.superseded,
            "superseded_by": self.superseded_by,
            "tick": self.tick,
            "basis": self.basis,
            "reading_ids": list(self.reading_ids),
            "free_water_l": self.plan.free_water_l,
            "materials": dict(self.plan.materials),
            "moisture_per_material": dict(self.plan.moisture_per_material),
        }


def material_percentages(
    readings: tuple[MoistureReading, ...],
    recipe: Recipe,
    *,
    batch_tick: int,
    wet_materials: tuple[str, ...],
    references: Mapping[str, float] | None = None,
) -> dict[str, float]:
    """Resolve the percentage each wet aggregate carries for one batch."""

    percentages: dict[str, float] = {}
    for material in wet_materials:
        reading = effective_reading(readings, material=material, batch_tick=batch_tick)
        if reading is not None:
            percentages[material] = reading.moisture_percent
        elif references is not None and material in references:
            percentages[material] = float(references[material])
        else:
            percentages[material] = recipe.moisture_ref_percent
    return percentages


def plan_batch_under_readings(
    recipe: Recipe,
    *,
    batch_id: str,
    quantity: float,
    batch_tick: int,
    readings: tuple[MoistureReading, ...],
    wet_materials: tuple[str, ...],
    references: Mapping[str, float] | None = None,
) -> BatchPlan:
    """Replan one batch with the moisture readings in force at its tick."""

    percentages = material_percentages(
        readings,
        recipe,
        batch_tick=batch_tick,
        wet_materials=wet_materials,
        references=references,
    )
    return plan_batch(
        recipe,
        batch_id=batch_id,
        quantity=quantity,
        moisture_percent=percentages.get(wet_materials[0], 0.0) if wet_materials else 0.0,
        wet_materials=wet_materials,
        moisture_per_material=percentages,
    )


def basis_for(
    readings: tuple[MoistureReading, ...],
    *,
    materials: tuple[str, ...],
    batch_tick: int,
) -> tuple[str, tuple[str, ...]]:
    """Return the reading basis signature and the reading identities in it."""

    signature = readings_signature(readings, materials=materials, batch_tick=batch_tick)
    identities = tuple(
        reading.reading_id
        for material in materials
        if (reading := effective_reading(readings, material=material, batch_tick=batch_tick)) is not None
    )
    return signature, identities


@dataclass(frozen=True, slots=True)
class SkippedBatch:
    """A batch the reading was not traced back to, with the reason why."""

    batch_id: str
    material: str
    reading_id: str
    reason: str

    def describe(self) -> dict[str, Any]:
        return {
            "batch_id": self.batch_id,
            "material": self.material,
            "reading_id": self.reading_id,
            "reason": self.reason,
        }


@dataclass(frozen=True, slots=True)
class RetroResult:
    """The outcome of filing one reading."""

    reading: MoistureReading
    retroactive: bool
    revised: tuple[BatchRevision, ...]
    adjustments: tuple[WaterAdjustment, ...]
    skipped: tuple[SkippedBatch, ...]

    @property
    def dropped_batches(self) -> tuple[str, ...]:
        """Batches skipped because their aggregate had already been dropped."""

        return tuple(
            item.batch_id for item in self.skipped if item.reason == SkipReason.BATCH_ALREADY_DROPPED.value
        )

    def describe(self) -> dict[str, Any]:
        return {
            "reading": self.reading.describe(),
            "retroactive": self.retroactive,
            "revised": [revision.describe() for revision in self.revised],
            "adjustments": [adjustment.describe() for adjustment in self.adjustments],
            "skipped": [item.describe() for item in self.skipped],
            "dropped_batches": list(self.dropped_batches),
        }


class MoistureLedger:
    """In memory projection rebuilt from the stream after every restart."""

    __slots__ = (
        "_readings",
        "_recipes",
        "_batches",
        "_revisions",
        "_adjustments",
        "_wet_materials",
        "_references",
    )

    def __init__(
        self,
        wet_materials: tuple[str, ...],
        references: Mapping[str, float] | None = None,
    ) -> None:
        self._readings: dict[str, MoistureReading] = {}
        self._recipes: dict[int, Recipe] = {}
        self._batches: list[dict[str, Any]] = []
        self._revisions: dict[str, list[BatchRevision]] = {}
        self._adjustments: list[WaterAdjustment] = []
        self._wet_materials = tuple(wet_materials)
        self._references = dict(references or {})

    # ------------------------------------------------------------------ reads

    @property
    def readings(self) -> tuple[MoistureReading, ...]:
        return tuple(self._readings[key] for key in sorted(self._readings))

    def reading(self, reading_id: str) -> MoistureReading | None:
        return self._readings.get(reading_id)

    def latest_reading(self, material: str) -> MoistureReading | None:
        """Return the newest observed reading for one aggregate."""

        return effective_reading(self.readings, material=material, batch_tick=10**18)

    def open_batches(self) -> tuple[dict[str, Any], ...]:
        """Batches whose weighed aggregate has not been dropped yet."""

        return tuple(item for item in self._batches if not item["dropped"])

    def revisions(self, batch_id: str) -> tuple[BatchRevision, ...]:
        return tuple(self._revisions.get(batch_id, ()))

    def current_revision(self, batch_id: str) -> BatchRevision | None:
        revisions = self._revisions.get(batch_id)
        return None if not revisions else revisions[-1]

    def adjustments(self) -> tuple[WaterAdjustment, ...]:
        return tuple(self._adjustments)

    def stock_water_delta_l(self) -> float:
        """Total batching water the take stock has to make up for."""

        return round(sum(item.delta_l for item in self._adjustments), 4)

    # ----------------------------------------------------------------- adopt

    def _identical_observation(self, reading: MoistureReading) -> bool:
        for other in self._readings.values():
            if (
                other.material == reading.material
                and other.observed_tick == reading.observed_tick
                and other.moisture_percent == reading.moisture_percent
            ):
                return True
        return False

    def adopt_observation(self, reading: MoistureReading) -> None:
        """Store a reading during replay without running the migration.

        Revisions and adjustments are authoritative records of their own, so
        restart replay must not let the reading recompute them a second time.
        """

        self._readings[reading.reading_id] = reading

    def adopt_recipe(self, recipe: Recipe) -> None:
        self._recipes[recipe.version] = recipe

    def declare_batch(self, batch: Mapping[str, Any]) -> None:
        """Remember one declared batch (a repeated declaration is ignored)."""

        batch_id = str(batch["batch_id"])
        if any(item["batch_id"] == batch_id for item in self._batches):
            return
        self._batches.append(
            {
                "batch_id": batch_id,
                "version": int(batch.get("version", 0)),
                "quantity": float(batch["quantity"]),
                "tick": int(batch["tick"]),
                "dropped": False,
            }
        )

    def mark_dropped(self, batch_id: str) -> None:
        for item in self._batches:
            if item["batch_id"] == batch_id:
                item["dropped"] = True
                return
        raise NotFoundError("batch was never declared", batch_id=batch_id)

    def seed_declaration(self, batch: Mapping[str, Any], recipe: Recipe) -> BatchRevision:
        """Create revision zero for a freshly declared batch.

        Revision zero is planned with the readings already in force at the
        declaration tick, so the first later reading revises it only when the
        moisture basis actually changes.
        """

        batch_id = str(batch["batch_id"])
        tick = int(batch["tick"])
        self.declare_batch(batch)
        plan = plan_batch_under_readings(
            recipe,
            batch_id=batch_id,
            quantity=float(batch["quantity"]),
            batch_tick=tick,
            readings=self.readings,
            wet_materials=self._wet_materials,
            references=self._references,
        )
        basis, identities = basis_for(self.readings, materials=tuple(recipe.factors), batch_tick=tick)
        revision = BatchRevision(
            batch_id=batch_id,
            revision=0,
            plan=plan,
            basis=basis,
            reading_ids=identities,
            tick=tick,
        )
        self.seed_revision(revision)
        return revision

    def seed_revision(self, revision: BatchRevision) -> None:
        """Adopt revision zero (the original declaration) without adjusting."""

        chain = self._revisions.setdefault(revision.batch_id, [])
        if not any(existing.revision == revision.revision for existing in chain):
            chain.append(revision)

    def adopt_adjustment(self, adjustment: WaterAdjustment) -> None:
        """Rebuild the take stock from the stream, keeping it deduplicated."""

        if any(
            existing.reading_id == adjustment.reading_id
            and existing.batch_id == adjustment.batch_id
            and existing.material == adjustment.material
            for existing in self._adjustments
        ):
            return
        self._adjustments.append(adjustment)

    def mark_superseded(self, batch_id: str, revision: int, superseded_by: int) -> None:
        chain = self._revisions.get(batch_id)
        if chain is None:
            raise NotFoundError("batch has no revisions", batch_id=batch_id)
        updated = [
            (
                item
                if item.revision != revision
                else BatchRevision(
                    batch_id=item.batch_id,
                    revision=item.revision,
                    plan=item.plan,
                    basis=item.basis,
                    reading_ids=item.reading_ids,
                    tick=item.tick,
                    superseded_by=superseded_by,
                )
            )
            for item in chain
        ]
        self._revisions[batch_id] = updated

    # ----------------------------------------------------------------- filing

    def file_reading(self, reading: MoistureReading, *, retroactive: bool) -> RetroResult:
        """File one reading and migrate the batches it should have been in for.

        Recomputation is batch by batch and idempotent: when the moisture
        basis of an open batch already matches the reading, nothing is
        rewritten.  Forward only materials, and batches already dropped, are
        reported under ``skipped`` with the reason.
        """

        if self._identical_observation(reading):
            return self._no_change_result(reading, retroactive=retroactive)
        self._readings[reading.reading_id] = reading

        revised: list[BatchRevision] = []
        adjustments: list[WaterAdjustment] = []
        skipped: list[SkippedBatch] = []

        if retroactive:
            for batch in tuple(self._batches):
                if batch["dropped"]:
                    skipped.append(
                        SkippedBatch(
                            batch_id=str(batch["batch_id"]),
                            material=reading.material,
                            reading_id=reading.reading_id,
                            reason=SkipReason.BATCH_ALREADY_DROPPED.value,
                        )
                    )
                    continue
                outcome = self._recompute_if_needed(batch, reading, revised, adjustments)
                if outcome is not None:
                    skipped.append(outcome)
        else:
            for batch in self._batches:
                skipped.append(
                    SkippedBatch(
                        batch_id=str(batch["batch_id"]),
                        material=reading.material,
                        reading_id=reading.reading_id,
                        reason=SkipReason.POLICY_NEW_BATCHES_ONLY.value,
                    )
                )
        return RetroResult(
            reading=reading,
            retroactive=retroactive,
            revised=tuple(revised),
            adjustments=tuple(adjustments),
            skipped=tuple(skipped),
        )

    def _recompute_if_needed(
        self,
        batch: Mapping[str, Any],
        reading: MoistureReading,
        revised: list[BatchRevision],
        adjustments: list[WaterAdjustment],
    ) -> SkippedBatch | None:
        batch_id = str(batch["batch_id"])
        if batch["tick"] < reading.observed_tick:
            # The observation belongs to a later pile, not this batch.
            return None
        current = self.current_revision(batch_id)
        recipe = self._recipe_for(batch)
        materials = tuple(recipe.factors)
        basis, identities = basis_for(self.readings, materials=materials, batch_tick=int(batch["tick"]))
        if current is not None and current.basis == basis:
            return SkippedBatch(
                batch_id=batch_id,
                material=reading.material,
                reading_id=reading.reading_id,
                reason=SkipReason.ALREADY_CURRENT.value,
            )
        plan = plan_batch_under_readings(
            recipe,
            batch_id=batch_id,
            quantity=float(batch["quantity"]),
            batch_tick=int(batch["tick"]),
            readings=self.readings,
            wet_materials=self._wet_materials,
            references=self._references,
        )
        revision_no = 1 if current is None else current.revision + 1
        revision = BatchRevision(
            batch_id=batch_id,
            revision=revision_no,
            plan=plan,
            basis=basis,
            reading_ids=identities,
            tick=reading.received_tick,
        )
        self._revisions.setdefault(batch_id, []).append(revision)
        if current is not None:
            self.mark_superseded(batch_id, current.revision, revision_no)
            delta = round(plan.free_water_l - current.free_water_l, 4)
            adjustment = WaterAdjustment(
                batch_id=batch_id,
                revision=revision_no,
                reading_id=reading.reading_id,
                material=reading.material,
                delta_l=delta,
            )
            self._adjustments.append(adjustment)
            adjustments.append(adjustment)
        revised.append(revision)
        return None

    def _no_change_result(self, reading: MoistureReading, *, retroactive: bool) -> RetroResult:
        skipped = tuple(
            SkippedBatch(
                batch_id=str(batch["batch_id"]),
                material=reading.material,
                reading_id=reading.reading_id,
                reason=SkipReason.ALREADY_CURRENT.value,
            )
            for batch in self._batches
            if retroactive and not batch["dropped"] and batch["tick"] >= reading.observed_tick
        )
        return RetroResult(
            reading=reading,
            retroactive=retroactive,
            revised=(),
            adjustments=(),
            skipped=skipped,
        )

    def _recipe_for(self, batch: Mapping[str, Any]) -> Recipe:
        recipe = self._recipes.get(int(batch["version"]))
        if recipe is None:
            raise NotFoundError(
                "the recipe version the batch was declared against is unknown",
                batch_id=batch["batch_id"],
                version=batch["version"],
            )
        return recipe

    # ----------------------------------------------------------------- checks

    def reconcile(self) -> dict[str, Any]:
        """Cross check recipe revisions, take stock and the audit basis.

        Returns one row per revised batch plus a total.  ``ok`` is true only
        when every revision's water change is booked exactly once in the take
        stock and the per revision deltas add up to the stock total.
        """

        rows: list[dict[str, Any]] = []
        expected_total = 0.0
        for batch_id in sorted(self._revisions):
            revisions = self._revisions[batch_id]
            if len(revisions) <= 1:
                continue
            booked = round(
                sum(item.delta_l for item in self._adjustments if item.batch_id == batch_id),
                4,
            )
            chain_delta = round(revisions[-1].free_water_l - revisions[0].free_water_l, 4)
            expected_total += chain_delta
            rows.append(
                {
                    "batch_id": batch_id,
                    "revisions": len(revisions),
                    "revision_delta_l": chain_delta,
                    "stock_delta_l": booked,
                    "match": chain_delta == booked,
                }
            )
        stock_total = self.stock_water_delta_l()
        expected_total = round(expected_total, 4)
        return {
            "rows": rows,
            "revision_delta_total_l": expected_total,
            "stock_delta_total_l": stock_total,
            "ok": all(row["match"] for row in rows) and expected_total == stock_total,
        }
