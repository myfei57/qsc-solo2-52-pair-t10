"""Turning a recipe and a batch size into material quantities.

A batch plan accepts either a single blanket moisture percentage (the legacy
shape, kept for callers and stored plans) or a *per material* moisture map so
that sand, gravel and manufactured sand are each compensated by the moisture
their own pile probe reported.
"""

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

    def material_free_water(self, material: str) -> float:
        """Return how many litres of free water ``material`` contributes."""

        percent = self.moisture_per_material.get(material, 0.0)
        factor = self._factor_of(material)
        return round(percent / 100.0 * factor * self.quantity, 4)

    def _factor_of(self, material: str) -> float:
        # materials are stored wet; dry mass = wet / (1 + percent/100), but
        # callers only use this for wet materials tracked in the plan map.
        wet = self.materials.get(material, 0.0)
        percent = self.moisture_per_material.get(material, 0.0)
        if percent <= 0:
            return wet / self.quantity if self.quantity else 0.0
        return wet / (1.0 + percent / 100.0) / self.quantity

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


def normalise_moisture(
    recipe: Recipe,
    moisture_percent: float,
    moisture_per_material: Mapping[str, float] | None,
    wet_materials: tuple[str, ...],
) -> dict[str, float]:
    """Resolve the moisture every wet material is compensated with."""

    if moisture_per_material:
        for material, percent in moisture_per_material.items():
            if material not in recipe.factors:
                raise ValidationError(
                    "moisture given for a material the recipe does not use", material=material
                )
            if not 0.0 <= percent <= 100.0:
                raise ValidationError("moisture must be a percentage", material=material, percent=percent)
        return {
            material: float(moisture_per_material.get(material, 0.0))
            for material in wet_materials
            if material in recipe.factors
        }
    if not 0.0 <= moisture_percent <= 100.0:
        raise ValidationError("moisture must be a percentage", moisture_percent=moisture_percent)
    return {material: float(moisture_percent) for material in wet_materials if material in recipe.factors}


def plan_batch(
    recipe: Recipe,
    *,
    batch_id: str,
    quantity: float,
    moisture_percent: float = 0.0,
    wet_materials: tuple[str, ...] = ("sand",),
    moisture_per_material: Mapping[str, float] | None = None,
) -> BatchPlan:
    """Scale every factor to ``quantity`` and add back each aggregate's water."""

    if not batch_id:
        raise ValidationError("batch identity must not be empty")
    if quantity <= 0:
        raise ValidationError("batch quantity must be positive", quantity=quantity)

    moisture_map = normalise_moisture(recipe, moisture_percent, moisture_per_material, wet_materials)
    # The reported blanket percentage is the free-water weighted mean so that
    # legacy consumers reading one number still see a meaningful aggregate.
    materials: dict[str, float] = {}
    free_water = 0.0
    dry_weighted = 0.0
    for material, factor in recipe.factors.items():
        dry = factor * quantity
        percent = moisture_map.get(material, 0.0)
        if material in wet_materials and percent > 0:
            water = dry * percent / 100.0
            free_water += water
            dry_weighted += dry * percent
            dry += water
        materials[material] = round(dry, 4)
    total_dry_wet = sum(
        factor * quantity
        for material, factor in recipe.factors.items()
        if material in wet_materials and moisture_map.get(material, 0.0) > 0
    )
    blanket = round(dry_weighted / total_dry_wet, 4) if total_dry_wet > 0 else 0.0
    return BatchPlan(
        batch_id=batch_id,
        grade=recipe.grade.code,
        version=recipe.version,
        quantity=quantity,
        unit="m3",
        materials=materials,
        free_water_l=round(free_water, 4),
        moisture_percent=blanket,
        moisture_per_material=moisture_map,
    )
