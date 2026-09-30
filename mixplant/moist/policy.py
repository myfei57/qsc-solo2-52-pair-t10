"""Per aggregate late reading policy.

A probe reading that arrives after the batches it was true for is handled one
of two ways, chosen per aggregate:

* ``RETROACTIVE``: the reading belongs to the material pile it was taken on.
  Every batch drawn from that pile that is still open is recalculated and the
  water already dosed for it is corrected once.  This is a migration: the
  pre migration version of every touched batch is retained.
* ``FORWARD_ONLY``: the reading only updates batches that have not been dosed
  yet.  Earlier batches keep the moisture they were actually batched with and
  the reading is logged as an explained non migration.

The policy is fixed configuration for an aggregate, not a per reading choice,
so "rewrite or not" can never drift with the operator on shift.
"""

from __future__ import annotations

from enum import Enum
from typing import Mapping

from ..errors import ValidationError


class RetroPolicy(str, Enum):
    """What happens to a reading that arrives late for one aggregate."""

    RETROACTIVE = "retroactive"
    FORWARD_ONLY = "forward_only"

    @classmethod
    def parse(cls, raw: "str | RetroPolicy") -> "RetroPolicy":
        if isinstance(raw, RetroPolicy):
            return raw
        try:
            return cls(str(raw))
        except ValueError:
            raise ValidationError(
                "unknown late reading policy",
                value=str(raw),
                known=[item.value for item in cls],
            ) from None

    @property
    def retroactive(self) -> bool:
        return self is RetroPolicy.RETROACTIVE


# Why a batch can keep its original moisture even under a retroactive policy.
REASON_POLICY_FORWARD = "policy_forward_only"
REASON_PILE_CHANGED = "pile_changed_before_reading"
REASON_BATCH_CLOSED = "batch_water_already_closed"
REASON_NO_APPLICABLE_BATCH = "no_open_batch_for_pile"
REASON_SUPERSEDED_READING = "reading_superseded_before_settlement"
REASON_ALREADY_MIGRATED = "reading_already_settled"

#: Defaults the station ships with.  Sand tracks fast and is corrected back;
#: crushed stone (gravel) and manufactured sand probes lag a batch or two and
#: are migrated onto the open batches they really belonged to.
DEFAULT_POLICIES: Mapping[str, RetroPolicy] = {
    "sand": RetroPolicy.RETROACTIVE,
    "gravel": RetroPolicy.RETROACTIVE,
    "manufactured-sand": RetroPolicy.RETROACTIVE,
    "cement": RetroPolicy.FORWARD_ONLY,
    "fly-ash": RetroPolicy.FORWARD_ONLY,
}

#: A batch stops being retroactively correctable once its batching water line
#: passes this phase (water physically metered: it cannot be un dosed).
WATER_FROZEN_PHASE = "water_in"
