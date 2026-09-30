"""Whether a late moisture reading rewrites earlier batches.

The decision is made **per aggregate** and is fixed on purpose: a material is
either migratory (a late reading is traced back to the batches it should have
been in force for) or forward only (it applies to new batches alone).  It must
never be decided ad hoc by the caller, so the policy lives here as the single
source of truth and is stored in the stream whenever an operator changes it.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Mapping

from ..errors import ValidationError

POLICY_KIND = "moist.policy"


class RetroPolicy(str, Enum):
    """How a late reading for one aggregate behaves."""

    RETROACTIVE = "retroactive"
    NEW_BATCHES_ONLY = "new_batches_only"

    @classmethod
    def parse(cls, value: "str | RetroPolicy") -> "RetroPolicy":
        if isinstance(value, RetroPolicy):
            return value
        try:
            return cls(str(value))
        except ValueError as exc:  # pragma: no cover - defensive parsing
            raise ValidationError("unknown moisture retro policy", value=str(value)) from exc


# Wet aggregates that vary pile to pile get traced back; everything else
# (cement, fly ash, admixture, water) only ever affects the next batch.
DEFAULT_POLICIES: Mapping[str, RetroPolicy] = {
    "sand": RetroPolicy.RETROACTIVE,
    "gravel": RetroPolicy.RETROACTIVE,
    "cement": RetroPolicy.NEW_BATCHES_ONLY,
    "fly-ash": RetroPolicy.NEW_BATCHES_ONLY,
    "admixture": RetroPolicy.NEW_BATCHES_ONLY,
    "water": RetroPolicy.NEW_BATCHES_ONLY,
}


@dataclass(frozen=True, slots=True)
class PolicyTable:
    """The retroactive policy in force for every aggregate."""

    policies: Mapping[str, RetroPolicy]

    def policy_for(self, material: str) -> RetroPolicy:
        """Return the policy for ``material`` (forward only when unknown)."""

        return self.policies.get(material, RetroPolicy.NEW_BATCHES_ONLY)

    def is_retroactive(self, material: str) -> bool:
        return self.policy_for(material) is RetroPolicy.RETROACTIVE

    def with_policy(self, material: str, policy: RetroPolicy) -> "PolicyTable":
        if not material:
            raise ValidationError("moisture policy material must not be empty")
        policies = dict(self.policies)
        policies[material] = policy
        return PolicyTable(policies=policies)

    def describe(self) -> dict[str, str]:
        return {material: policy.value for material, policy in sorted(self.policies.items())}


def default_policies() -> PolicyTable:
    """Return the policy table the station ships with."""

    return PolicyTable(policies=dict(DEFAULT_POLICIES))
