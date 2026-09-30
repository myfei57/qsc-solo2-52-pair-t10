"""Turning a recipe and a batch size into material quantities."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping

from ..errors import ValidationError
from .recipe import Recipe


@dataclass(frozen=True, slots=True)
class BatchPlan:
    """The masses one batch needs, already moisture compensated."""

    batch_id: str
    grade: str
    version: int
    quantity: float
    unit: str
    materials: Mapping[str, float]
    free_water_l: float
    moisture_percent: float
    moisture_per_material: Mapping[str, float]

    def material_mass(self, material: str) -> float:
        """Return the compensated mass of ``material``."""

        value = self.materials.get(material)
        if value is None:
            raise ValidationError("batch plan has no mass for this material", material=material)
        return value

    @property
    def dry_total(self) -> float:
        return round(sum(self.materials.values()), 4)

    def describe(self) -> dict[str, Any]:
        return {
            "batch_id": self.batch_id,
            "grade": self.grade,
            "version": self.version,
            "quantity": self.quantity,
            "unit": self.unit,
            "materials": dict(self.materials),
            "dry_total": self.dry_total,
            "free_water_l": self.free_water_l,
            "moisture_percent": self.moisture_percent,
            "moisture_per_material": dict(self.moisture_per_material),
        }


def _normalise_moisture(
    moisture_percent: float,
    moisture_per_material: Mapping[str, float] | None,
    wet_materials: tuple[str, ...],
) -> dict[str, float]:
    """Resolve the per material percentages the plan compensates with.

    A single ``moisture_percent`` stays supported for callers that only track
    one wet aggregate; it is spread across every wet material.  An explicit
    per material mapping wins and lets sand and gravel carry their own water.
    """

    if moisture_per_material is None:
        return {material: moisture_percent for material in wet_materials}
    percentages: dict[str, float] = {}
    for material, percent in moisture_per_material.items():
        if not material:
            raise ValidationError("moisture material must not be empty")
        if not 0.0 <= float(percent) <= 100.0:
            raise ValidationError("moisture must be a percentage", material=material, moisture_percent=percent)
        percentages[material] = float(percent)
    return percentages


def plan_batch(
    recipe: Recipe,
    *,
    batch_id: str,
    quantity: float,
    moisture_percent: float = 0.0,
    wet_materials: tuple[str, ...] = ("sand",),
    moisture_per_material: Mapping[str, float] | None = None,
) -> BatchPlan:
    """Scale every factor to ``quantity`` and add back the aggregate water.

    Each wet aggregate is compensated with its own percentage when
    ``moisture_per_material`` is supplied; otherwise the single
    ``moisture_percent`` is spread across ``wet_materials``.
    """

    if not batch_id:
        raise ValidationError("batch identity must not be empty")
    if quantity <= 0:
        raise ValidationError("batch quantity must be positive", quantity=quantity)
    if not 0.0 <= moisture_percent <= 100.0:
        raise ValidationError("moisture must be a percentage", moisture_percent=moisture_percent)
    percentages = _normalise_moisture(moisture_percent, moisture_per_material, wet_materials)
    materials: dict[str, float] = {}
    free_water = 0.0
    for material, factor in recipe.factors.items():
        dry = factor * quantity
        percent = percentages.get(material, 0.0)
        if percent > 0:
            water = dry * percent / 100.0
            free_water += water
            dry += water
        materials[material] = round(dry, 4)
    effective_per_material = {material: percentages.get(material, 0.0) for material in recipe.factors}
    return BatchPlan(
        batch_id=batch_id,
        grade=recipe.grade.code,
        version=recipe.version,
        quantity=quantity,
        unit="m3",
        materials=materials,
        free_water_l=round(free_water, 4),
        moisture_percent=moisture_percent,
        moisture_per_material=effective_per_material,
    )
