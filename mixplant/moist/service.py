"""Moisture service: per aggregate compensation and retroactive migration."""

from __future__ import annotations

from typing import Any, Mapping

from ..context import SharedContext
from ..agg.agg import WET_MATERIALS
from ..event.bus import Event
from ..recipe.book import RecipeBook
from ..service_base import PlantService
from ..statemachine.machine import SequenceMachine
from ..store.records import Record
from .comp import Compensation, compensate, water_offset
from .ledger import (
    BatchRevision,
    MoistureLedger,
    RetroResult,
    WaterAdjustment,
)
from .mode import mode_for
from .policy import POLICY_KIND, PolicyTable, RetroPolicy, default_policies
from .reading import (
    idempotency_key,
    make_reading,
    reading_from_document,
    reading_identity,
)
from .revision import (
    ADJUST_KIND,
    READING_KIND,
    REVISION_KIND,
    adjust_document,
    revision_document,
    revision_from_document,
)
from .table import MoistureTable

COMP_KIND = "moist.comp"
MODE_KIND = "moist.mode"
SUPERSEDE_KIND = "moist.supersede"


class MoistService(PlantService):
    """Records each aggregate's moisture and migrates late readings.

    Whether a late reading rewrites earlier batches is fixed per aggregate by
    the :class:`PolicyTable`.  Retroactive aggregates are recomputed batch by
    batch; forward only aggregates affect new batches alone.
    """

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
        wet_materials: tuple[str, ...] = WET_MATERIALS,
        policies: PolicyTable | None = None,
    ) -> None:
        super().__init__(context, machine)
        self.book = book
        self.table = table
        self.wet_materials = tuple(wet_materials)
        self.policies = policies or default_policies()
        self.ledger = MoistureLedger(self.wet_materials, references=table.references)
        self._compensations: list[Compensation] = []
        self._idempotency: dict[str, str] = {}

    # ------------------------------------------------------------- compensation

    def compensate(self, *, material: str, moisture_percent: float, quantity: float) -> dict[str, Any]:
        """Apply the moisture correction for ``material`` for the next batch.

        This is the on time path: the reading is observed and received on the
        current tick, so it only ever feeds batches declared afterwards.
        """

        self.require_order(quantity > 0, "a compensation needs a positive batch size", quantity=quantity)
        recipe = self.book.selected()
        correction = compensate(material, recipe.factor(material) * quantity, moisture_percent)
        filed = self.file_reading(
            material=material,
            moisture_percent=moisture_percent,
            observed_tick=self.context.clock.now(),
        )
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
            "reading": filed["reading"],
            "retroactive": filed["retroactive"],
            "seq": record.seq,
        }

    # ---------------------------------------------------------------- readings

    def file_reading(
        self,
        *,
        material: str,
        moisture_percent: float,
        observed_tick: int | None = None,
        reading_id: str | None = None,
    ) -> dict[str, Any]:
        """File a probe reading, tracing a late one back per its policy.

        A repeated filing of the same observation is a no-op: the migration
        returns no new revision and books no water a second time.
        """

        self._sync_recipes()
        now = self.context.clock.now()
        observed = now if observed_tick is None else int(observed_tick)
        self.require_order(observed <= now, "a reading cannot be observed in the future", observed_tick=observed)
        # A repeated trigger carrying the very same observation collapses onto
        # the reading it produced the first time.  A changed percentage at the
        # same observed tick is a new observation and migrates again.
        idem_key = idempotency_key(material, observed, float(moisture_percent))
        duplicate_id = self._idempotency.get(idem_key)
        if duplicate_id is not None and self.ledger.reading(duplicate_id) is not None:
            reading = self.ledger.reading(duplicate_id)
            result = self.ledger.file_reading(reading, retroactive=self.policies.is_retroactive(material))
            return {**result.describe(), "idempotent": True}
        identity = reading_id or f"{reading_identity(material, observed)}-{self.context.ids.next('reading')}"
        reading = make_reading(
            material=material,
            moisture_percent=moisture_percent,
            observed_tick=observed,
            received_tick=now,
            reading_id=identity,
        )
        retroactive = self.policies.is_retroactive(material)
        if self.ledger.reading(identity) is None:
            self.publish(READING_KIND, {"active": True, **reading.describe()})
        self._idempotency[idem_key] = identity
        result = self.ledger.file_reading(reading, retroactive=retroactive)
        self._persist_migration(result)
        return {**result.describe(), "idempotent": len(result.revised) == 0}

    def backfill_reading(
        self,
        *,
        material: str,
        moisture_percent: float,
        observed_tick: int,
        reading_id: str | None = None,
    ) -> dict[str, Any]:
        """Explicitly file a reading that arrived late."""

        return self.file_reading(
            material=material,
            moisture_percent=moisture_percent,
            observed_tick=observed_tick,
            reading_id=reading_id,
        )

    def set_policy(self, *, material: str, policy: str) -> dict[str, Any]:
        """Fix whether late readings for one aggregate trace back or not.

        The policy only governs readings filed afterwards; it never reopens a
        batch an earlier policy already decided against.
        """

        parsed = RetroPolicy.parse(policy)
        self.policies = self.policies.with_policy(material, parsed)
        self.publish(POLICY_KIND, {"active": True, "material": material, "policy": parsed.value})
        self.emit("moist.policy_set", {"material": material, "policy": parsed.value})
        return {"material": material, "policy": parsed.value, "policies": self.policies.describe()}

    # -------------------------------------------------------------- declarations

    def seed_declared_batch(self, *, batch_id: str, quantity: float, version: int, tick: int) -> BatchRevision:
        """Create revision zero when a batch is declared."""

        self._sync_recipes()
        recipe = self._recipe_version(version)
        return self.ledger.seed_declaration(
            {"batch_id": batch_id, "quantity": quantity, "version": version, "tick": tick},
            recipe,
        )

    def mark_batch_dropped(self, batch_id: str) -> None:
        self.ledger.mark_dropped(batch_id)

    def percentages_for(self, *, batch_tick: int) -> dict[str, float]:
        """Return the moisture percentage each wet aggregate currently carries."""

        recipe = self.book.selected()
        from .ledger import material_percentages

        return material_percentages(
            self.ledger.readings,
            recipe,
            batch_tick=batch_tick,
            wet_materials=self.wet_materials,
            references=self.table.references,
        )

    def current_plan(self, batch_id: str) -> BatchRevision | None:
        return self.ledger.current_revision(batch_id)

    # ---------------------------------------------------------------- queries

    def water_offset(self) -> float:
        """Return the free water the on time compensations already supplied."""

        return water_offset(self.compensations)

    def takestock(self) -> dict[str, Any]:
        """Return the water take stock the retroactive migrations moved."""

        adjustments = [item.describe() for item in self.ledger.adjustments()]
        return {
            "pending_water_delta_l": self.ledger.stock_water_delta_l(),
            "adjustments": adjustments,
            "count": len(adjustments),
        }

    def revisions(self, batch_id: str) -> dict[str, Any]:
        """Return the retained revision chain of one batch."""

        chain = self.ledger.revisions(batch_id)
        return {
            "batch_id": batch_id,
            "current_revision": 0 if not chain else chain[-1].revision,
            "revisions": [revision.describe() for revision in chain],
        }

    def reconcile(self) -> dict[str, Any]:
        """Cross check recipe revisions, take stock and the audit trail."""

        report = dict(self.ledger.reconcile())
        audit_total = round(
            sum(float(record.payload.get("delta_l", 0.0)) for record in self.store.history(ADJUST_KIND)),
            4,
        )
        report["audit_delta_total_l"] = audit_total
        report["ok"] = bool(report["ok"]) and audit_total == report["stock_delta_total_l"]
        return report

    @property
    def compensations(self) -> tuple[Compensation, ...]:
        return tuple(self._compensations)

    # ------------------------------------------------------------------ events

    def handle_batch_event(self, event: Event) -> None:
        """Track declarations and drops so the ledger knows the cutoff."""

        if event.name == "agg.batch_declared":
            nested = event.payload.get("batch")
            batch = nested if isinstance(nested, Mapping) else event.payload
            self.seed_declared_batch(
                batch_id=str(batch["batch_id"]),
                quantity=float(batch["quantity"]),
                version=int(batch.get("generation", batch.get("version", 0))),
                tick=int(batch["tick"]),
            )
        elif event.name == "agg.dropped":
            latest = self.context.batches.latest()
            if latest is not None:
                self.ledger.mark_dropped(latest.batch_id)

    # ------------------------------------------------------------------ restart

    def sync(self, corrections: list[Compensation]) -> None:
        """Adopt compensations rebuilt from the stream after a restart."""

        self._compensations = list(corrections)

    def restore_reading(self, record: Record) -> None:
        """Adopt one reading observation during replay (no migration rerun)."""

        reading = reading_from_document(record.payload)
        self.ledger.adopt_observation(reading)
        key = idempotency_key(reading.material, reading.observed_tick, reading.moisture_percent)
        self._idempotency.setdefault(key, reading.reading_id)

    def restore_policy(self, record: Record) -> None:
        self.policies = self.policies.with_policy(
            str(record.payload["material"]),
            RetroPolicy.parse(str(record.payload["policy"])),
        )

    def restore_revision(self, record: Record) -> None:
        """Adopt one revision record read back from the stream."""

        revision = revision_from_document(record.payload, tick=record.tick)
        chain = self.ledger.revisions(revision.batch_id)
        if any(existing.revision == revision.revision for existing in chain):
            return
        self.ledger.seed_revision(revision)
        raw = record.payload.get("superseded_by")
        if raw is not None:
            self.ledger.mark_superseded(
                str(record.payload["batch_id"]),
                int(record.payload["revision"]),
                int(raw),
            )

    def restore_supersede(self, record: Record) -> None:
        """Replay the pointer that marked one revision as the previous one."""

        self.ledger.mark_superseded(
            str(record.payload["batch_id"]),
            int(record.payload["revision"]),
            int(record.payload["superseded_by"]),
        )

    def restore_adjustment(self, record: Record) -> None:
        self.ledger.adopt_adjustment(WaterAdjustment.from_document(record.payload))

    def restore_batch(self, record: Record) -> None:
        """Rebuild revision zero and the declaration from a batch record."""

        payload = record.payload
        batch_id = str(payload["batch_id"])
        version = int(payload.get("version", payload.get("generation", 0)))
        if self.ledger.revisions(batch_id):
            return
        try:
            recipe = self._recipe_version(version)
        except Exception:
            return
        self.ledger.seed_declaration(
            {"batch_id": batch_id, "quantity": float(payload["quantity"]), "version": version, "tick": record.tick},
            recipe,
        )

    def restore_drop(self, record: Record) -> None:
        latest = self.context.batches.latest()
        if latest is not None:
            self.ledger.mark_dropped(latest.batch_id)

    # ------------------------------------------------------------------ status

    def status(self) -> dict[str, Any]:
        entry = self.payload_entry("moist", "comp")
        per_material = {
            material: (
                None if (reading := self.ledger.latest_reading(material)) is None
                else reading.moisture_percent
            )
            for material in self.wet_materials
        }
        latest_readings = {
            material: reading.describe()
            for material in self.wet_materials
            if (reading := self.ledger.latest_reading(material)) is not None
        }
        return {
            "compensated": entry is not None,
            "material": entry.get("material") if entry else None,
            "percent": float(entry.get("percent", 0.0)) if entry else None,
            "per_material_percent": per_material,
            "latest_readings": latest_readings,
            "free_water_l": round(self.water_offset(), 4),
            "pending_water_delta_l": self.ledger.stock_water_delta_l(),
            "policies": self.policies.describe(),
            "mode": (self.payload_entry("moist", "mode") or {}).get("mode"),
        }

    # ---------------------------------------------------------------- internal

    def _sync_recipes(self) -> None:
        name = self.book.selected_name()
        if name is not None:
            for recipe in self.book.history(name):
                self.ledger.adopt_recipe(recipe)

    def _recipe_version(self, version: int):
        from ..errors import NotFoundError

        name = self.book.selected_name()
        if name is None:
            raise NotFoundError("select a recipe before filing moisture readings")
        return self.book.version(name, version)

    def _persist_migration(self, result: RetroResult) -> None:
        """Append revision, supersede and take stock records, then audit it."""

        for revision in result.revised:
            if revision.revision >= 1:
                self.publish(
                    SUPERSEDE_KIND,
                    {
                        "active": True,
                        "batch_id": revision.batch_id,
                        "revision": revision.revision - 1,
                        "superseded_by": revision.revision,
                    },
                )
            self.publish(REVISION_KIND, revision_document(revision))
        for adjustment in result.adjustments:
            self.publish(ADJUST_KIND, adjust_document(adjustment))
        self.emit(
            "moist.reading_filed",
            {**result.describe(), "dropped_batches": list(result.dropped_batches)},
        )
        if result.revised:
            self.emit(
                "moist.batches_retraced",
                {
                    "reading_id": result.reading.reading_id,
                    "material": result.reading.material,
                    "batch_ids": [revision.batch_id for revision in result.revised],
                    "water_delta_l": round(sum(item.delta_l for item in result.adjustments), 4),
                },
            )
