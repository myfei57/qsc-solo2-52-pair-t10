"""Serialisation of a batch revision through the record stream."""

from __future__ import annotations

from typing import Any, Mapping

from ..recipe.plan import BatchPlan
from .ledger import BatchRevision, WaterAdjustment

REVISION_KIND = "moist.revision"
ADJUST_KIND = "moist.adjust"
READING_KIND = "moist.reading"


def plan_document(plan: BatchPlan) -> dict[str, Any]:
    """Turn a plan into the body stored on a revision record."""

    return {
        "batch_id": plan.batch_id,
        "grade": plan.grade,
        "version": plan.version,
        "quantity": plan.quantity,
        "unit": plan.unit,
        "materials": dict(plan.materials),
        "free_water_l": plan.free_water_l,
        "moisture_percent": plan.moisture_percent,
        "moisture_per_material": dict(plan.moisture_per_material),
    }


def plan_from_document(document: Mapping[str, Any]) -> BatchPlan:
    """Rebuild a plan from a stored revision body."""

    return BatchPlan(
        batch_id=str(document["batch_id"]),
        grade=str(document.get("grade", "")),
        version=int(document.get("version", 0)),
        quantity=float(document["quantity"]),
        unit=str(document.get("unit", "m3")),
        materials=dict(document["materials"]),
        free_water_l=float(document.get("free_water_l", 0.0)),
        moisture_percent=float(document.get("moisture_percent", 0.0)),
        moisture_per_material=dict(document.get("moisture_per_material") or {}),
    )


def revision_document(revision: BatchRevision) -> dict[str, Any]:
    """Build the payload a revision is stored under."""

    return {
        "active": True,
        "batch_id": revision.batch_id,
        "revision": revision.revision,
        "superseded_by": revision.superseded_by,
        "basis": revision.basis,
        "reading_ids": list(revision.reading_ids),
        **plan_document(revision.plan),
    }


def revision_from_document(document: Mapping[str, Any], *, tick: int) -> BatchRevision:
    """Rebuild a revision from a stored payload."""

    raw_superseded = document.get("superseded_by")
    return BatchRevision(
        batch_id=str(document["batch_id"]),
        revision=int(document["revision"]),
        plan=plan_from_document(document),
        basis=str(document.get("basis", "")),
        reading_ids=tuple(str(value) for value in document.get("reading_ids", [])),
        tick=int(document.get("tick", tick)),
        superseded_by=None if raw_superseded is None else int(raw_superseded),
    )


def adjust_document(adjustment: WaterAdjustment) -> dict[str, Any]:
    """Build the payload a take stock adjustment is stored under."""

    return {"active": True, **adjustment.describe()}
