"""Read model behind the batching page."""

from __future__ import annotations

from typing import Any, Mapping

from ..recipe.batch import plan_summary


def batch_view(*, recipe_service: Any, agg_service: Any, moist_service: Any, config: Mapping[str, Any]) -> dict[str, Any]:
    """Build the batching view, including the per bin targets."""

    batch = agg_service.context.batches.latest()
    payload: dict[str, Any] = {
        "batch": None if batch is None else batch.describe(),
        "recipe": recipe_service.status(),
        "aggregate": agg_service.status(),
        "moisture": moist_service.status(),
        "config": dict(config),
        "targets": None,
        "plan": None,
        "takestock": moist_service.takestock(),
        "reconcile": moist_service.reconcile(),
        "revisions": None,
    }
    if batch is not None:
        payload["revisions"] = moist_service.revisions(batch.batch_id)
    if batch is not None and recipe_service.mapping_or_none() is not None:
        payload["targets"] = agg_service.describe_targets()
        payload["plan"] = plan_summary(_plan(recipe_service, batch, moist_service))
    return payload


def _plan(recipe_service: Any, batch: Any, moist_service: Any):
    """Return the current moisture revision's plan for the batch page."""

    current = moist_service.current_plan(batch.batch_id)
    if current is not None:
        return current.plan
    from ..recipe.plan import plan_batch

    recipe = recipe_service.current()
    percentages = moist_service.percentages_for(batch_tick=batch.tick)
    return plan_batch(
        recipe,
        batch_id=batch.batch_id,
        quantity=batch.quantity,
        moisture_percent=percentages.get("sand", recipe.moisture_ref_percent),
        moisture_per_material=percentages,
    )
