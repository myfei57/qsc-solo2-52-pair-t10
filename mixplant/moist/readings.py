"""Moisture readings and the material pile they describe.

Probe readings are not corrections yet.  A reading says: *for pile
``pile_id`` of ``material``, at (or about) tick ``observed_tick``, the
moisture was ``percent``*.  It carries the tick it was taken at separately
from the tick it arrived on, which is what makes late readings detectable
and traceable.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping

from ..errors import ValidationError
from .mode import MoistureMode, mode_for
from ..config import Limits

READING_KIND = "moist.reading"
PILE_KIND = "moist.pile"
SETTLE_KIND = "moist.settle"
MIGRATION_KIND = "moist.migration"
NONMIGRATION_KIND = "moist.nonmigration"
POLICY_KIND = "moist.policy"
RECONCILE_KIND = "moist.reconcile"


@dataclass(frozen=True, slots=True)
class MoistureReading:
    """One probe observation for one pile of one aggregate."""

    reading_id: str
    material: str
    pile_id: str
    percent: float
    observed_tick: int
    arrived_tick: int
    policy: str
    superseded: bool = False

    def __post_init__(self) -> None:
        if not self.reading_id:
            raise ValidationError("a reading needs an identity")
        if not self.material:
            raise ValidationError("a reading needs a material", reading_id=self.reading_id)
        if not self.pile_id:
            raise ValidationError("a reading needs a pile identity", reading_id=self.reading_id)
        if not 0.0 <= self.percent <= 100.0:
            raise ValidationError(
                "moisture must be a percentage", reading_id=self.reading_id, percent=self.percent
            )
        if self.observed_tick < 0 or self.arrived_tick < 0:
            raise ValidationError("reading ticks must not be negative", reading_id=self.reading_id)
        if self.observed_tick > self.arrived_tick:
            raise ValidationError(
                "a reading cannot arrive before it was taken",
                reading_id=self.reading_id,
                observed_tick=self.observed_tick,
                arrived_tick=self.arrived_tick,
            )

    @property
    def late(self) -> bool:
        """A reading is late when settlement happens after it arrived."""

        return self.arrived_tick > self.observed_tick

    def mode(self, limits: Limits) -> MoistureMode:
        return mode_for(self.percent, limits)

    def describe(self) -> dict[str, Any]:
        return {
            "reading_id": self.reading_id,
            "material": self.material,
            "pile_id": self.pile_id,
            "percent": self.percent,
            "observed_tick": self.observed_tick,
            "arrived_tick": self.arrived_tick,
            "late": self.late,
            "policy": self.policy,
            "superseded": self.superseded,
        }


@dataclass(frozen=True, slots=True)
class PileState:
    """Which pile of an aggregate the plant is currently drawing from."""

    material: str
    pile_id: str
    since_tick: int
    generation: int

    def describe(self) -> dict[str, Any]:
        return {
            "material": self.material,
            "pile_id": self.pile_id,
            "since_tick": self.since_tick,
            "generation": self.generation,
        }


def reading_from_payload(payload: Mapping[str, Any]) -> MoistureReading:
    """Rebuild a reading from a stored record payload."""

    return MoistureReading(
        reading_id=str(payload["reading_id"]),
        material=str(payload["material"]),
        pile_id=str(payload["pile_id"]),
        percent=float(payload["percent"]),
        observed_tick=int(payload.get("observed_tick", payload.get("tick", 0))),
        arrived_tick=int(payload.get("arrived_tick", payload.get("tick", 0))),
        policy=str(payload.get("policy", "retroactive")),
        superseded=bool(payload.get("superseded", False)),
    )
