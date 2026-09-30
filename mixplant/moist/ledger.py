"""The moisture ledger: settled moisture per material and the migration log.

This is a pure, replayable projection.  It owns no I/O: every mutation is a
function of a committed record.  Replaying a journal rebuilds an identical
ledger, which is what makes retroactive recalculation idempotent.

Two moisture numbers exist for every material/pile pair:

* ``authoritative_percent`` - the best moisture known *now*, including late
  readings migrated back onto their pile.  This drives new batch plans.
* ``effective_percent`` per (batch, material) - the moisture a specific batch
  was recalculated with.

A batch is corrected at most once per arriving reading and a reading is
settled at most once; repeating a late reading command changes nothing.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from typing import Any, Mapping

from ..errors import ValidationError
from .policy import REASON_ALREADY_MIGRATED, REASON_SUPERSEDED_READING, RetroPolicy
from .readings import MoistureReading, PileState

FORWARD_REASON = "policy_forward_only"
CLOSED_REASON = "batch_water_already_closed"
NO_BATCH_REASON = "no_open_batch_for_pile"


@dataclass(frozen=True, slots=True)
class BatchMoisture:
    """The moisture one (batch, material) was planned and recalculated with."""

    batch_id: str
    material: str
    pile_id: str
    planned_percent: float
    effective_percent: float
    quantity: float
    dry_mass: float
    free_water_l: float
    planned_water_l: float
    actual_water_l: float
    corrected_water_delta_l: float
    migrated: bool
    revision: int
    frozen: bool
    migrated_reading_ids: tuple[str, ...] = ()

    def describe(self) -> dict[str, Any]:
        return {
            "batch_id": self.batch_id,
            "material": self.material,
            "pile_id": self.pile_id,
            "planned_percent": self.planned_percent,
            "effective_percent": self.effective_percent,
            "quantity": self.quantity,
            "dry_mass": self.dry_mass,
            "free_water_l": self.free_water_l,
            "planned_water_l": self.planned_water_l,
            "actual_water_l": self.actual_water_l,
            "water_delta_l": self.corrected_water_delta_l,
            "migrated": self.migrated,
            "revision": self.revision,
            "frozen": self.frozen,
            "migrated_reading_ids": list(self.migrated_reading_ids),
        }


@dataclass(frozen=True, slots=True)
class Migration:
    """One retroactive correction applied to one batch for one material."""

    migration_id: str
    reading_id: str
    batch_id: str
    material: str
    pile_id: str
    from_percent: float
    to_percent: float
    water_delta_l: float
    revision_from: int
    revision_to: int
    tick: int

    def describe(self) -> dict[str, Any]:
        return {
            "migration_id": self.migration_id,
            "reading_id": self.reading_id,
            "batch_id": self.batch_id,
            "material": self.material,
            "pile_id": self.pile_id,
            "from_percent": self.from_percent,
            "to_percent": self.to_percent,
            "water_delta_l": self.water_delta_l,
            "revision_from": self.revision_from,
            "revision_to": self.revision_to,
            "tick": self.tick,
        }


@dataclass(frozen=True, slots=True)
class NonMigration:
    """A late reading that was deliberately not traced back, and why."""

    reading_id: str
    material: str
    pile_id: str
    percent: float
    observed_tick: int
    arrived_tick: int
    reason: str
    affected_batch_ids: tuple[str, ...]
    tick: int

    def describe(self) -> dict[str, Any]:
        return {
            "reading_id": self.reading_id,
            "material": self.material,
            "pile_id": self.pile_id,
            "percent": self.percent,
            "observed_tick": self.observed_tick,
            "arrived_tick": self.arrived_tick,
            "reason": self.reason,
            "affected_batches": list(self.affected_batch_ids),
            "tick": self.tick,
        }


def migration_identity(reading_id: str, batch_id: str, material: str) -> str:
    """Deterministic id so replaying a migration never creates a duplicate."""

    return f"mig:{reading_id}:{batch_id}:{material}"


class MoistureLedger:
    """Projected moisture state rebuilt from the committed stream."""

    __slots__ = (
        "_piles",
        "_readings",
        "_settled",
        "_batch_rows",
        "_migrations",
        "_nonmigrations",
        "_policies",
        "_pile_generation",
        "_settled_reading_ids",
        "_migration_ids",
    )

    def __init__(self, policies: Mapping[str, RetroPolicy] | None = None) -> None:
        self._piles: dict[str, PileState] = {}
        self._readings: list[MoistureReading] = []
        self._settled: dict[tuple[str, str], float] = {}
        self._batch_rows: dict[tuple[str, str], BatchMoisture] = {}
        self._migrations: list[Migration] = []
        self._nonmigrations: list[NonMigration] = []
        self._policies: dict[str, RetroPolicy] = {
            material: RetroPolicy.parse(policy) for material, policy in (policies or {}).items()
        }
        self._pile_generation: dict[str, int] = {}
        self._settled_reading_ids: set[str] = set()
        self._migration_ids: set[str] = set()

    # ------------------------------------------------------------------ policy
    def set_policy(self, material: str, policy: RetroPolicy | str) -> None:
        self._policies[material] = RetroPolicy.parse(policy)

    def policy_for(self, material: str) -> RetroPolicy:
        return RetroPolicy.parse(self._policies.get(material, RetroPolicy.RETROACTIVE))

    def policies(self) -> Mapping[str, str]:
        return {
            material: RetroPolicy.parse(policy).value
            for material, policy in sorted(self._policies.items())
        }

    # -------------------------------------------------------------------- piles
    def change_pile(self, material: str, pile_id: str, *, tick: int) -> PileState:
        generation = self._pile_generation.get(material, 0) + 1
        pile = PileState(material=material, pile_id=pile_id, since_tick=tick, generation=generation)
        self._piles[material] = pile
        self._pile_generation[material] = generation
        return pile

    def adopt_pile(self, material: str, pile_id: str, *, tick: int, generation: int) -> None:
        self._piles[material] = PileState(
            material=material, pile_id=pile_id, since_tick=tick, generation=generation
        )
        self._pile_generation[material] = max(self._pile_generation.get(material, 0), generation)

    def pile_of(self, material: str) -> PileState | None:
        return self._piles.get(material)

    def piles(self) -> tuple[PileState, ...]:
        return tuple(sorted(self._piles.values(), key=lambda pile: pile.material))

    # ----------------------------------------------------------------- readings
    def add_reading(self, reading: MoistureReading) -> None:
        if any(item.reading_id == reading.reading_id for item in self._readings):
            raise ValidationError("reading identity was reused", reading_id=reading.reading_id)
        self._readings.append(reading)

    def readings(self, *, material: str | None = None) -> tuple[MoistureReading, ...]:
        rows = self._readings
        if material is not None:
            rows = [item for item in rows if item.material == material]
        return tuple(rows)

    def reading(self, reading_id: str) -> MoistureReading | None:
        for item in self._readings:
            if item.reading_id == reading_id:
                return item
        return None

    def latest_reading(self, material: str, pile_id: str) -> MoistureReading | None:
        found = None
        for item in self._readings:
            if item.material == material and item.pile_id == pile_id and not item.superseded:
                found = item
        return found

    # ------------------------------------------------------------- batch ledger
    def register_batch(
        self,
        *,
        batch_id: str,
        material: str,
        pile_id: str,
        planned_percent: float,
        quantity: float,
        dry_mass: float,
        free_water_l: float,
        planned_water_l: float,
        actual_water_l: float = 0.0,
        frozen: bool = False,
    ) -> BatchMoisture:
        key = (batch_id, material)
        if key in self._batch_rows:
            raise ValidationError("batch moisture row was registered twice", batch_id=batch_id, material=material)
        row = BatchMoisture(
            batch_id=batch_id,
            material=material,
            pile_id=pile_id,
            planned_percent=planned_percent,
            effective_percent=planned_percent,
            quantity=quantity,
            dry_mass=round(dry_mass, 4),
            free_water_l=round(free_water_l, 4),
            planned_water_l=round(planned_water_l, 4),
            actual_water_l=round(actual_water_l, 4),
            corrected_water_delta_l=0.0,
            migrated=False,
            revision=1,
            frozen=frozen,
        )
        self._batch_rows[key] = row
        return row

    def record_actual_water(self, batch_id: str, material: str, actual_water_l: float) -> BatchMoisture:
        row = self._require_row(batch_id, material)
        self._batch_rows[(batch_id, material)] = replace(
            row, actual_water_l=round(actual_water_l, 4)
        )
        return self._batch_rows[(batch_id, material)]

    def freeze_batch_water(self, batch_id: str) -> None:
        """Mark every material row of ``batch_id`` water closed (metered)."""

        for material in {key[1] for key in self._batch_rows if key[0] == batch_id}:
            row = self._batch_rows[(batch_id, material)]
            if not row.frozen:
                self._batch_rows[(batch_id, material)] = replace(row, frozen=True)

    def batch_rows(self, batch_id: str) -> tuple[BatchMoisture, ...]:
        return tuple(row for (bid, _material), row in self._batch_rows.items() if bid == batch_id)

    def all_batch_rows(self) -> tuple[BatchMoisture, ...]:
        return tuple(self._batch_rows.values())

    def _require_row(self, batch_id: str, material: str) -> BatchMoisture:
        row = self._batch_rows.get((batch_id, material))
        if row is None:
            raise ValidationError("no moisture row for batch/material", batch_id=batch_id, material=material)
        return row

    # -------------------------------------------------------------- settlement
    def settle_reading(self, reading: MoistureReading) -> "Settlement":
        """Apply one reading exactly once.

        Settlement is deterministic and idempotent: the *newest* reading for a
        pile is authoritative.  An older reading that arrives after a newer one
        has already been settled is recorded as a non migration
        (``reading_superseded_before_settlement``) and touches nothing.
        """

        if reading.reading_id in self._settled_reading_ids:
            return Settlement(
                reading=reading,
                migrations=(),
                nonmigration=NonMigration(
                    reading_id=reading.reading_id,
                    material=reading.material,
                    pile_id=reading.pile_id,
                    percent=reading.percent,
                    observed_tick=reading.observed_tick,
                    arrived_tick=reading.arrived_tick,
                    reason=REASON_ALREADY_MIGRATED,
                    affected_batch_ids=(),
                    tick=reading.arrived_tick,
                ),
                applied=False,
                duplicate=True,
            )

        newest = self.latest_observed(reading.material, reading.pile_id)
        superseded = newest is not None and newest.reading_id != reading.reading_id and (
            (newest.observed_tick, newest.arrived_tick)
            > (reading.observed_tick, reading.arrived_tick)
        )
        if superseded:
            non = NonMigration(
                reading_id=reading.reading_id,
                material=reading.material,
                pile_id=reading.pile_id,
                percent=reading.percent,
                observed_tick=reading.observed_tick,
                arrived_tick=reading.arrived_tick,
                reason=REASON_SUPERSEDED_READING,
                affected_batch_ids=self._batch_ids(reading.material, reading.pile_id),
                tick=reading.arrived_tick,
            )
            self._nonmigrations.append(non)
            self._settled_reading_ids.add(reading.reading_id)
            return Settlement(reading=reading, migrations=(), nonmigration=non, applied=False)

        result = self._apply_reading(reading)
        self._settled_reading_ids.add(reading.reading_id)
        return result

    def _apply_reading(self, reading: MoistureReading) -> "Settlement":
        policy = self.policy_for(reading.material)
        self._settled[(reading.material, reading.pile_id)] = reading.percent

        candidates = [
            row
            for row in tuple(self._batch_rows.values())
            if row.material == reading.material
            and row.pile_id == reading.pile_id
            and reading.reading_id not in row.migrated_reading_ids
        ]

        if not policy.retroactive:
            non = NonMigration(
                reading_id=reading.reading_id,
                material=reading.material,
                pile_id=reading.pile_id,
                percent=reading.percent,
                observed_tick=reading.observed_tick,
                arrived_tick=reading.arrived_tick,
                reason=FORWARD_REASON,
                affected_batch_ids=tuple(
                    sorted({row.batch_id for row in candidates if not row.frozen})
                ),
                tick=reading.arrived_tick,
            )
            self._nonmigrations.append(non)
            return Settlement(reading=reading, migrations=(), nonmigration=non, applied=True)

        migrations: list[Migration] = []
        frozen_hits = tuple(sorted({row.batch_id for row in candidates if row.frozen}))
        for row in [item for item in candidates if not item.frozen]:
            if abs(row.effective_percent - reading.percent) < 1e-9:
                continue
            delta = round((reading.percent - row.effective_percent) / 100.0 * row.dry_mass, 4)
            new_free_water = round(reading.percent / 100.0 * row.dry_mass, 4)
            migrated = replace(
                row,
                effective_percent=reading.percent,
                free_water_l=new_free_water,
                corrected_water_delta_l=round(row.corrected_water_delta_l + delta, 4),
                migrated=True,
                revision=row.revision + 1,
                migrated_reading_ids=tuple(sorted(row.migrated_reading_ids + (reading.reading_id,))),
            )
            self._batch_rows[(row.batch_id, row.material)] = migrated
            migration = Migration(
                migration_id=migration_identity(reading.reading_id, row.batch_id, row.material),
                reading_id=reading.reading_id,
                batch_id=row.batch_id,
                material=row.material,
                pile_id=row.pile_id,
                from_percent=row.effective_percent,
                to_percent=reading.percent,
                water_delta_l=delta,
                revision_from=row.revision,
                revision_to=row.revision + 1,
                tick=reading.arrived_tick,
            )
            migrations.append(migration)
            self._migration_ids.add(migration.migration_id)
        self._migrations.extend(migrations)

        non: NonMigration | None = None
        if frozen_hits:
            non = NonMigration(
                reading_id=reading.reading_id,
                material=reading.material,
                pile_id=reading.pile_id,
                percent=reading.percent,
                observed_tick=reading.observed_tick,
                arrived_tick=reading.arrived_tick,
                reason=CLOSED_REASON,
                affected_batch_ids=frozen_hits,
                tick=reading.arrived_tick,
            )
        elif not migrations:
            non = NonMigration(
                reading_id=reading.reading_id,
                material=reading.material,
                pile_id=reading.pile_id,
                percent=reading.percent,
                observed_tick=reading.observed_tick,
                arrived_tick=reading.arrived_tick,
                reason=NO_BATCH_REASON,
                affected_batch_ids=(),
                tick=reading.arrived_tick,
            )
        if non is not None:
            self._nonmigrations.append(non)
        return Settlement(reading=reading, migrations=tuple(migrations), nonmigration=non, applied=True)

    def _batch_ids(self, material: str, pile_id: str) -> tuple[str, ...]:
        return tuple(
            sorted(
                {
                    row.batch_id
                    for row in self._batch_rows.values()
                    if row.material == material and row.pile_id == pile_id
                }
            )
        )

    def latest_observed(self, material: str, pile_id: str) -> MoistureReading | None:
        """The on-file reading that is newest by observation, then arrival."""

        found = None
        for item in self._readings:
            if item.material != material or item.pile_id != pile_id:
                continue
            if found is None or (item.observed_tick, item.arrived_tick) >= (
                found.observed_tick,
                found.arrived_tick,
            ):
                found = item
        return found

    def authoritative_percent(self, material: str, pile_id: str) -> float | None:
        return self._settled.get((material, pile_id))

    def migrations(self, *, batch_id: str | None = None) -> tuple[Migration, ...]:
        rows = self._migrations
        if batch_id is not None:
            rows = [item for item in rows if item.batch_id == batch_id]
        return tuple(rows)

    def nonmigrations(self) -> tuple[NonMigration, ...]:
        return tuple(self._nonmigrations)

    # ------------------------------------------------------------------- adopt
    def adopt_migration(self, migration: Migration) -> None:
        """Adopt a migration read back from the stream after a restart."""

        if migration.migration_id in self._migration_ids:
            return
        self._migrations.append(migration)
        self._migration_ids.add(migration.migration_id)
        self._settled_reading_ids.add(migration.reading_id)
        self._settled[(migration.material, migration.pile_id)] = migration.to_percent
        key = (migration.batch_id, migration.material)
        row = self._batch_rows.get(key)
        if row is None:
            return
        if migration.reading_id in row.migrated_reading_ids:
            return
        self._batch_rows[key] = replace(
            row,
            effective_percent=migration.to_percent,
            free_water_l=round(migration.to_percent / 100.0 * row.dry_mass, 4),
            corrected_water_delta_l=round(row.corrected_water_delta_l + migration.water_delta_l, 4),
            migrated=True,
            revision=max(row.revision, migration.revision_to),
            migrated_reading_ids=tuple(sorted(set(row.migrated_reading_ids) | {migration.reading_id})),
        )

    def adopt_nonmigration(self, non: NonMigration) -> None:
        if any(item.reading_id == non.reading_id and item.reason == non.reason for item in self._nonmigrations):
            return
        self._nonmigrations.append(non)
        self._settled_reading_ids.add(non.reading_id)

    def adopt_settled(self, material: str, pile_id: str, percent: float) -> None:
        self._settled[(material, pile_id)] = percent

    def status(self) -> dict[str, Any]:
        return {
            "piles": [pile.describe() for pile in self.piles()],
            "policies": self.policies(),
            "authoritative": [
                {"material": material, "pile_id": pile_id, "percent": percent}
                for (material, pile_id), percent in sorted(self._settled.items())
            ],
            "readings": len(self._readings),
            "settled_readings": len(self._settled_reading_ids),
            "migrations": len(self._migrations),
            "nonmigrations": len(self._nonmigrations),
        }


@dataclass(frozen=True, slots=True)
class Settlement:
    """The result of settling one reading."""

    reading: MoistureReading
    migrations: tuple[Migration, ...]
    nonmigration: NonMigration | None
    applied: bool
    duplicate: bool = False

    def describe(self) -> dict[str, Any]:
        return {
            "reading": self.reading.describe(),
            "applied": self.applied,
            "duplicate": self.duplicate,
            "migrations": [item.describe() for item in self.migrations],
            "nonmigration": None if self.nonmigration is None else self.nonmigration.describe(),
        }


__all__ = [
    "BatchMoisture",
    "Migration",
    "MoistureLedger",
    "NonMigration",
    "Settlement",
    "migration_identity",
]
