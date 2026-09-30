"""A probe reading and the batches it was supposed to be in force for.

A probe on a freshly swapped pile often reports one or two batches late.  A
reading therefore carries the tick the moisture was actually observed at
(``observed_tick``) in addition to the tick it arrived on.  The batches a
reading belongs to are derived from ``observed_tick`` alone, never from the
arrival tick, so the same late reading is attributed identically whether it
arrives immediately, after a restart or via a repeated backfill.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Mapping

from ..errors import ValidationError


@dataclass(frozen=True, slots=True)
class MoistureReading:
    """One observed moisture percentage for one aggregate."""

    reading_id: str
    material: str
    moisture_percent: float
    observed_tick: int
    received_tick: int

    def __post_init__(self) -> None:
        if not self.reading_id:
            raise ValidationError("a moisture reading needs an identity")
        if not self.material:
            raise ValidationError("a moisture reading needs a material")
        if not 0.0 <= self.moisture_percent <= 100.0:
            raise ValidationError(
                "moisture must be a percentage", material=self.material, moisture_percent=self.moisture_percent
            )
        if self.observed_tick < 0:
            raise ValidationError("the observed tick must not be negative", tick=self.observed_tick)
        if self.received_tick < self.observed_tick:
            raise ValidationError(
                "a reading cannot be received before it was observed",
                observed_tick=self.observed_tick,
                received_tick=self.received_tick,
            )

    @property
    def late(self) -> bool:
        return self.received_tick > self.observed_tick

    def describe(self) -> dict[str, Any]:
        return {
            "reading_id": self.reading_id,
            "material": self.material,
            "moisture_percent": self.moisture_percent,
            "observed_tick": self.observed_tick,
            "received_tick": self.received_tick,
            "late": self.late,
        }


def reading_identity(material: str, observed_tick: int) -> str:
    """Build the deterministic identity a backfill is idempotent under.

    Re-filing the same observation for the same aggregate, tick **and**
    percentage returns the same identity, so a repeated late-data trigger
    cannot rewrite a batch twice or deduct its water twice.  A genuinely new
    observation at the same tick (a changed percentage) gets a different
    identity and is allowed to migrate the batches again.
    """

    return f"reading:{material}:{observed_tick}"


def idempotency_key(material: str, observed_tick: int, moisture_percent: float) -> str:
    """Return the key a repeated late-data trigger collapses under."""

    return f"{reading_identity(material, observed_tick)}:{moisture_percent}"


def make_reading(
    *,
    material: str,
    moisture_percent: float,
    observed_tick: int,
    received_tick: int,
    reading_id: str | None = None,
) -> MoistureReading:
    """Build one reading; callers supply a unique identity when they have one."""

    return MoistureReading(
        reading_id=reading_id or reading_identity(material, observed_tick),
        material=material,
        moisture_percent=float(moisture_percent),
        observed_tick=int(observed_tick),
        received_tick=int(received_tick),
    )


def reading_from_document(document: Mapping[str, Any]) -> MoistureReading:
    """Rebuild a reading from a stored payload."""

    return MoistureReading(
        reading_id=str(document["reading_id"]),
        material=str(document["material"]),
        moisture_percent=float(document["moisture_percent"]),
        observed_tick=int(document["observed_tick"]),
        received_tick=int(document["received_tick"]),
    )


def effective_reading(
    readings: tuple[MoistureReading, ...], *, material: str, batch_tick: int
) -> MoistureReading | None:
    """Return the reading in force for ``material`` when a batch was declared.

    The effective reading is the newest observation of the material whose
    ``observed_tick`` is not later than the batch declaration.  When several
    observations share a tick the one with the highest identity wins, so a
    corrected observation at the same tick supersedes the earlier one while a
    replayed identical reading keeps the same answer.
    """

    candidate: MoistureReading | None = None
    for reading in readings:
        if reading.material != material or reading.observed_tick > batch_tick:
            continue
        if (
            candidate is None
            or reading.observed_tick > candidate.observed_tick
            or (reading.observed_tick == candidate.observed_tick and reading.reading_id > candidate.reading_id)
        ):
            candidate = reading
    return candidate


def readings_signature(
    readings: tuple[MoistureReading, ...], *, materials: tuple[str, ...], batch_tick: int
) -> str:
    """Hash the moisture basis a batch was planned under.

    Two batches planned under the same effective readings carry the same
    signature.  The reading *identity* is part of the hash, so a changed
    observation at the same tick produces a new basis and a new revision,
    while an identical retrigger collapses onto the same one.
    """

    effective = {
        material: reading.reading_id
        for material in materials
        if (reading := effective_reading(readings, material=material, batch_tick=batch_tick)) is not None
    }
    return json.dumps(effective, sort_keys=True, separators=(",", ":"))
