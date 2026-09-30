"""Moisture service.

Owns the per aggregate probe readings, the pile each aggregate is drawn
from, the per aggregate late-reading policy and the retroactive migration
that recalculates the batches a late reading really belonged to.

Everything lands on the append-only stream:

* ``moist.policy``   - retroactive vs forward-only, fixed per aggregate
* ``moist.pile``     - a material pile change (料堆更换)
* ``moist.reading``  - one probe observation, observed vs arrival tick
* ``moist.settle``   - one reading settled onto its pile (authoritative now)
* ``moist.migration``- one retroactive recalculation of one batch/material
* ``moist.nonmigration`` - a reading deliberately not traced back, with reason
* ``moist.reconcile``- the three-way consistency verdict, for console/audit
"""

from __future__ import annotations

from typing import Any, Mapping

from ..context import SharedContext
from ..recipe.book import RecipeBook
from ..recipe.plan import plan_batch
from ..service_base import PlantService
from ..statemachine.machine import SequenceMachine
from ..agg.agg import WET_MATERIALS
from .comp import Compensation, compensate, water_offset
from .ledger import (
    Migration,
    MoistureLedger,
    NonMigration,
    Settlement,
)
from .mode import mode_for
from .policy import DEFAULT_POLICIES, RetroPolicy
from .readings import (
    MIGRATION_KIND,
    NONMIGRATION_KIND,
    PILE_KIND,
    POLICY_KIND,
    READING_KIND,
    RECONCILE_KIND,
    SETTLE_KIND,
    MoistureReading,
    reading_from_payload,
)
from .reconcile import reconcile
from .revisions import RevisionRegistry
from .table import MoistureTable

COMP_KIND = "moist.comp"
MODE_KIND = "moist.mode"
BATCH_ROW_KIND = "moist.batch"


class MoistService(PlantService):
    """Records aggregate moisture and migrates late readings onto open batches."""

    origin = "moist"
    line = "batch"
    subject = "moist"

    def __init__(
        self,
        context: SharedContext,
        book: RecipeBook,
        table: MoistureTable,
        machine: SequenceMachine | None = None,
        *,
        policies: Mapping[str, str | RetroPolicy] | None = None,
    ) -> None:
        super().__init__(context, machine)
        self.book = book
        self.table = table
        merged = dict(DEFAULT_POLICIES)
        # Station config fixes the per aggregate policy; an explicit
        # constructor argument (used in tests) overrides it.
        moisture_config = getattr(context.config, "moisture", None)
        if moisture_config is not None:
            merged.update(dict(moisture_config.late_policy))
        for material, policy in (policies or {}).items():
            merged[material] = RetroPolicy.parse(policy)
        self.ledger = MoistureLedger(merged)
        self.revisions = RevisionRegistry()
        self._compensations: list[Compensation] = []

    # ------------------------------------------------------------------ policy
    def set_late_policy(self, material: str, policy: str | RetroPolicy) -> dict[str, Any]:
        """Fix how late readings of one aggregate are handled."""

        parsed = RetroPolicy.parse(policy)
        self.ledger.set_policy(material, parsed)
        record = self.publish(
            POLICY_KIND,
            {"active": True, "material": material, "policy": parsed.value},
        )
        self.emit("moist.policy_set", {"material": material, "policy": parsed.value})
        return {"material": material, "policy": parsed.value, "seq": record.seq}

    # -------------------------------------------------------------------- pile
    def change_pile(self, material: str, pile_id: str) -> dict[str, Any]:
        """Record that the plant switched the pile it draws ``material`` from."""

        pile = self.ledger.change_pile(material, pile_id, tick=self.context.clock.now())
        record = self.publish(
            PILE_KIND,
            {
                "active": True,
                "material": material,
                "pile_id": pile.pile_id,
                "since_tick": pile.since_tick,
                "generation": pile.generation,
            },
        )
        self.emit("moist.pile_changed", {"material": material, "pile_id": pile_id})
        return {"pile": pile.describe(), "seq": record.seq}

    def current_pile_id(self, material: str) -> str:
        pile = self.ledger.pile_of(material)
        if pile is None:
            # First use of an aggregate implicitly opens pile "pile-1".
            pile = self.ledger.change_pile(material, "pile-1", tick=self.context.clock.now())
        return pile.pile_id

    # ---------------------------------------------------------------- readings
    def reading(
        self,
        *,
        material: str,
        moisture_percent: float,
        observed_tick: int | None = None,
        pile_id: str | None = None,
        reading_id: str | None = None,
    ) -> dict[str, Any]:
        """Accept a probe reading and settle it onto the pile it describes.

        ``observed_tick`` is when the probe actually took the reading; it
        defaults to the current tick.  Passing an earlier tick models a
        late-arriving reading.
        """

        now = self.context.clock.now()
        observed = now if observed_tick is None else observed_tick
        if observed < 0 or observed > now:
            from ..errors import ValidationError

            raise ValidationError(
                "a reading cannot be observed in the future",
                observed_tick=observed,
                now=now,
            )
        pile = pile_id or self.current_pile_id(material)
        policy = self.ledger.policy_for(material)
        rid = reading_id or self.context.ids.next("moisture")
        existing = self.ledger.reading(rid)
        if existing is not None:
            # Re-delivery of the same physical probe datum is idempotent:
            # it returns the stored settlement and writes nothing new.  The
            # pile named in a redelivery must match the pile the original
            # reading was taken on, not whatever pile is current at re-send.
            from ..errors import ValidationError

            if pile_id is not None and pile_id != existing.pile_id:
                raise ValidationError(
                    "reading identity was reused on a different pile",
                    reading_id=rid,
                    pile_id=pile_id,
                    original_pile_id=existing.pile_id,
                )
            if (
                existing.material != material
                or abs(existing.percent - moisture_percent) > 1e-9
                or existing.observed_tick != observed
            ):
                raise ValidationError("reading identity was reused with different data", reading_id=rid)
            from .ledger import NonMigration, Settlement
            from .policy import REASON_ALREADY_MIGRATED

            non = NonMigration(
                reading_id=rid,
                material=material,
                pile_id=existing.pile_id,
                percent=moisture_percent,
                observed_tick=observed,
                arrived_tick=existing.arrived_tick,
                reason=REASON_ALREADY_MIGRATED,
                affected_batch_ids=(),
                tick=existing.arrived_tick,
            )
            return {
                "reading": existing.describe(),
                "mode": str(mode_for(moisture_percent, self.context.config.limits).value),
                "settlement": Settlement(
                    reading=existing, migrations=(), nonmigration=non, applied=False, duplicate=True
                ).describe(),
                "redelivered": True,
            }
        probe = MoistureReading(
            reading_id=rid,
            material=material,
            pile_id=pile,
            percent=moisture_percent,
            observed_tick=observed,
            arrived_tick=now,
            policy=policy.value,
        )
        self.ledger.add_reading(probe)
        mode = mode_for(moisture_percent, self.context.config.limits)
        self.publish(READING_KIND, {"active": True, **probe.describe()})
        self.publish(
            MODE_KIND,
            {"active": True, "mode": str(mode.value), "material": material, "percent": moisture_percent},
        )
        self.emit(
            "moist.reading_received",
            {
                "reading_id": rid,
                "material": material,
                "pile_id": pile,
                "percent": moisture_percent,
                "late": probe.late,
                "observed_tick": observed,
            },
        )
        settlement = self._settle(probe)
        return {
            "reading": probe.describe(),
            "mode": str(mode.value),
            "settlement": settlement.describe(),
        }

    # --------------------------------------------------------------- migration
    def _settle(self, probe: MoistureReading) -> Settlement:
        settlement = self.ledger.settle_reading(probe)
        self.publish(
            SETTLE_KIND,
            {
                "active": True,
                "reading_id": probe.reading_id,
                "material": probe.material,
                "pile_id": probe.pile_id,
                "percent": probe.percent,
                "applied": settlement.applied,
                "duplicate": settlement.duplicate,
            },
        )
        recipe = self.book.selected()
        for migration in settlement.migrations:
            self._persist_migration(migration, recipe)
            self._compensate_migration_event(migration)
        if settlement.nonmigration is not None:
            non = settlement.nonmigration
            self.publish(NONMIGRATION_KIND, {"active": True, **non.describe()})
            self.emit(
                "moist.reading_not_migrated",
                {
                    "reading_id": non.reading_id,
                    "material": non.material,
                    "reason": non.reason,
                    "affected_batches": list(non.affected_batch_ids),
                },
            )
        if settlement.migrations:
            self.emit(
                "moist.migrated",
                {
                    "reading_id": probe.reading_id,
                    "material": probe.material,
                    "batches": [item.batch_id for item in settlement.migrations],
                },
            )
        return settlement

    def _persist_migration(self, migration: Migration, recipe: Any) -> None:
        """Write the migration record and append the new plan revision."""

        history = self.revisions.for_batch(migration.batch_id)
        # Find the batch quantity off the registered row so the revision is
        # rebuilt from the same dry mass the batch was declared with.
        rows = self.ledger.batch_rows(migration.batch_id)
        quantity = rows[0].quantity if rows else 0.0
        moisture_map = dict(history.latest.plan.moisture_per_material) if history.revisions() else {}
        moisture_map[migration.material] = migration.to_percent
        plan = plan_batch(
            recipe,
            batch_id=migration.batch_id,
            quantity=quantity,
            moisture_percent=migration.to_percent,
            wet_materials=tuple(moisture_map) or WET_MATERIALS,
            moisture_per_material=moisture_map,
        )
        _revision, added = history.add_migration(
            plan,
            source_reading_ids=(migration.reading_id,),
            tick=migration.tick,
        )
        self.publish(MIGRATION_KIND, {"active": True, **migration.describe()})
        self.publish(
            "moist.revision",
            {
                "active": True,
                "batch_id": migration.batch_id,
                "revision": history.latest.revision,
                "basis": history.latest.basis,
                "water_delta_l": migration.water_delta_l,
                "free_water_l": plan.free_water_l,
                "moisture_per_material": dict(plan.moisture_per_material),
                "added": added,
                "source_reading_ids": [migration.reading_id],
            },
        )

    def _compensate_migration_event(self, migration: Migration) -> None:
        """Raise the signed correction event the audit trail totals."""

        self.emit(
            "moist.water_corrected",
            {
                "migration_id": migration.migration_id,
                "reading_id": migration.reading_id,
                "batch_id": migration.batch_id,
                "material": migration.material,
                "water_delta_l": migration.water_delta_l,
                "revision_to": migration.revision_to,
            },
        )

    # ----------------------------------------------------- legacy compensation
    def compensate(self, *, material: str, moisture_percent: float, quantity: float) -> dict[str, Any]:
        """Apply the moisture correction for ``material`` (per aggregate)."""

        self.require_order(quantity > 0, "a compensation needs a positive batch size", quantity=quantity)
        recipe = self.book.selected()
        correction = compensate(material, recipe.factor(material) * quantity, moisture_percent)
        mode = mode_for(moisture_percent, self.context.config.limits)
        self._compensations.append(correction)
        self.publish(
            MODE_KIND,
            {"active": True, "mode": str(mode.value), "material": material, "percent": moisture_percent},
        )
        record = self.publish(
            COMP_KIND,
            {"active": True, **correction.describe(), "batch_quantity": quantity, "version": recipe.version},
        )
        self.emit("moist.compensated", {"material": material, "free_water_l": correction.free_water_l})
        return {
            "compensation": correction.describe(),
            "mode": str(mode.value),
            "table_reference": self.table.reference(material),
            "seq": record.seq,
        }

    @property
    def compensations(self) -> tuple[Compensation, ...]:
        return tuple(self._compensations)

    def water_offset(self) -> float:
        """Return the free water the compensations already supplied."""

        return water_offset(self.compensations)

    # ------------------------------------------------------- planning support
    def moisture_map_for(self, quantity: float) -> dict[str, float]:
        """Authoritative per-material moisture for the next batch plan."""

        recipe = self.book.selected()
        result: dict[str, float] = {}
        for material in WET_MATERIALS:
            if material not in recipe.factors:
                continue
            pile = self.current_pile_id(material)
            percent = self.ledger.authoritative_percent(material, pile)
            result[material] = self.table.reference(material) if percent is None else percent
        return result

    def plan(self, batch_id: str, quantity: float) -> dict[str, Any]:
        """Plan a batch with the moisture currently settled on each aggregate."""

        recipe = self.book.selected()
        moisture_map = self.moisture_map_for(quantity)
        plan = plan_batch(
            recipe,
            batch_id=batch_id,
            quantity=quantity,
            moisture_percent=recipe.moisture_ref_percent,
            wet_materials=WET_MATERIALS,
            moisture_per_material=moisture_map,
        )
        return plan.describe()

    def register_batch_plan(self, batch_id: str, quantity: float, plan: Any) -> None:
        """Seed the revision history and ledger rows for a declared batch."""

        history = self.revisions.for_batch(batch_id)
        if not history.revisions():
            history.seed(plan, tick=self.context.clock.now())
            self.publish(
                "moist.revision",
                {
                    "active": True,
                    "batch_id": batch_id,
                    "revision": 1,
                    "basis": "declared",
                    "water_delta_l": 0.0,
                    "free_water_l": plan.free_water_l,
                    "moisture_per_material": dict(plan.moisture_per_material),
                    "added": True,
                    "source_reading_ids": [],
                },
            )
        for material, percent in plan.moisture_per_material.items():
            if self.ledger.batch_rows(batch_id) and any(
                row.material == material for row in self.ledger.batch_rows(batch_id)
            ):
                continue
            dry_mass = self._dry_mass(plan, material)
            free_water_l = round(percent / 100.0 * dry_mass, 4)
            pile_id = self.current_pile_id(material)
            self.ledger.register_batch(
                batch_id=batch_id,
                material=material,
                pile_id=pile_id,
                planned_percent=percent,
                quantity=quantity,
                dry_mass=dry_mass,
                free_water_l=free_water_l,
                planned_water_l=free_water_l,
            )
            self.publish(
                BATCH_ROW_KIND,
                {
                    "active": True,
                    "batch_id": batch_id,
                    "material": material,
                    "pile_id": pile_id,
                    "planned_percent": percent,
                    "effective_percent": percent,
                    "quantity": quantity,
                    "dry_mass": dry_mass,
                    "free_water_l": free_water_l,
                },
            )

    @staticmethod
    def _dry_mass(plan: Any, material: str) -> float:
        percent = plan.moisture_per_material.get(material, 0.0)
        wet = float(plan.materials.get(material, 0.0))
        return round(wet / (1.0 + percent / 100.0), 4) if percent > 0 else round(wet, 4)

    def freeze_batch_water(self, batch_id: str) -> None:
        """Close a batch to retroactive correction once water is metered."""

        self.ledger.freeze_batch_water(batch_id)
        self.publish("moist.frozen", {"active": True, "batch_id": batch_id})

    # ------------------------------------------------------------ reconcile
    def stocktake(self) -> dict[str, Any]:
        """Return the water stock take implied by committed migrations."""

        from .stocktake import WaterStockTake

        return WaterStockTake.from_migrations(self.ledger.migrations()).describe()

    def reconcile(self) -> dict[str, Any]:
        """Run the three-way consistency check and file it for the audit."""

        report = reconcile(self.ledger, self.revisions)
        described = report.describe()
        described["stocktake"] = self.stocktake()
        self.publish(RECONCILE_KIND, {"active": True, "fingerprint": report.fingerprint(), **described})
        self.emit(
            "moist.reconciled",
            {"ok": report.ok, "checked_batches": report.checked_batches, "fingerprint": report.fingerprint()},
        )
        return described

    def migration_report(self, batch_id: str | None = None) -> dict[str, Any]:
        return {
            "migrations": [item.describe() for item in self.ledger.migrations(batch_id=batch_id)],
            "nonmigrations": [item.describe() for item in self.ledger.nonmigrations()],
        }

    # -------------------------------------------------------------- restart
    def sync(self, corrections: list[Compensation]) -> None:
        """Adopt compensations rebuilt from the stream after a restart."""

        self._compensations = list(corrections)

    def adopt_reading(self, payload: Mapping[str, Any]) -> None:
        probe = reading_from_payload(payload)
        if self.ledger.reading(probe.reading_id) is None:
            self.ledger.add_reading(probe)

    def adopt_pile(self, payload: Mapping[str, Any]) -> None:
        self.ledger.adopt_pile(
            str(payload["material"]),
            str(payload["pile_id"]),
            tick=int(payload.get("since_tick", 0)),
            generation=int(payload.get("generation", 1)),
        )

    def adopt_policy(self, payload: Mapping[str, Any]) -> None:
        self.ledger.set_policy(str(payload["material"]), str(payload["policy"]))

    def adopt_migration(self, payload: Mapping[str, Any]) -> None:
        migration = Migration(
            migration_id=str(payload["migration_id"]),
            reading_id=str(payload["reading_id"]),
            batch_id=str(payload["batch_id"]),
            material=str(payload["material"]),
            pile_id=str(payload["pile_id"]),
            from_percent=float(payload["from_percent"]),
            to_percent=float(payload["to_percent"]),
            water_delta_l=float(payload["water_delta_l"]),
            revision_from=int(payload["revision_from"]),
            revision_to=int(payload["revision_to"]),
            tick=int(payload.get("tick", 0)),
        )
        self.ledger.adopt_migration(migration)

    def adopt_nonmigration(self, payload: Mapping[str, Any]) -> None:
        self.ledger.adopt_nonmigration(
            NonMigration(
                reading_id=str(payload["reading_id"]),
                material=str(payload["material"]),
                pile_id=str(payload["pile_id"]),
                percent=float(payload["percent"]),
                observed_tick=int(payload["observed_tick"]),
                arrived_tick=int(payload["arrived_tick"]),
                reason=str(payload["reason"]),
                affected_batch_ids=tuple(str(item) for item in payload.get("affected_batches", ())),
                tick=int(payload.get("tick", 0)),
            )
        )

    def adopt_batch_row(self, payload: Mapping[str, Any]) -> None:
        batch_id = str(payload["batch_id"])
        material = str(payload["material"])
        if any(row.material == material for row in self.ledger.batch_rows(batch_id)):
            return
        self.ledger.register_batch(
            batch_id=batch_id,
            material=material,
            pile_id=str(payload.get("pile_id", "pile-1")),
            planned_percent=float(payload.get("planned_percent", 0.0)),
            quantity=float(payload.get("quantity", 0.0)),
            dry_mass=float(payload.get("dry_mass", 0.0)),
            free_water_l=float(payload.get("free_water_l", 0.0)),
            planned_water_l=float(payload.get("free_water_l", 0.0)),
        )

    # ---------------------------------------------------------------- status
    def status(self) -> dict[str, Any]:
        entry = self.payload_entry("moist", "comp")
        report = reconcile(self.ledger, self.revisions)
        return {
            "compensated": entry is not None,
            "material": entry.get("material") if entry else None,
            "percent": float(entry.get("percent", 0.0)) if entry else None,
            "free_water_l": round(self.water_offset(), 4),
            "mode": (self.payload_entry("moist", "mode") or {}).get("mode"),
            "ledger": self.ledger.status(),
            "reconciliation": {"ok": report.ok, "checked_batches": report.checked_batches},
            "open_migrations": [
                item.describe()
                for item in self.ledger.migrations()
                if not any(
                    row.frozen
                    for row in self.ledger.batch_rows(item.batch_id)
                    if row.material == item.material
                )
            ],
        }
