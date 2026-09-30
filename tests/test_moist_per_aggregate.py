"""Per aggregate moisture: each aggregate compensated by its own reading."""

from __future__ import annotations

import pytest

from mixplant.agg.agg import WET_MATERIALS
from mixplant.moist.policy import RetroPolicy
from mixplant.recipe.plan import normalise_moisture, plan_batch
from mixplant.runtime import PlantRuntime
from tests.support import publish_and_select, run


def _select(runtime: PlantRuntime) -> None:
    publish_and_select(runtime)


def test_every_aggregate_is_compensated_by_its_own_moisture(runtime: PlantRuntime) -> None:
    _select(runtime)
    run(runtime, "moist.reading", {"material": "sand", "moisture_percent": 6.0})
    run(runtime, "moist.reading", {"material": "gravel", "moisture_percent": 2.0})
    run(runtime, "batch.declare", {"batch_id": "B-1", "quantity": 1.0})
    plan = runtime.moist.revisions.for_batch("B-1").latest.plan
    # sand factor 760, 6% -> 45.6 L free water; gravel 1010, 2% -> 20.2 L
    assert plan.moisture_per_material["sand"] == pytest.approx(6.0)
    assert plan.moisture_per_material["gravel"] == pytest.approx(2.0)
    assert plan.free_water_l == pytest.approx(45.6 + 20.2)
    assert plan.materials["sand"] == pytest.approx(760.0 + 45.6)
    assert plan.materials["gravel"] == pytest.approx(1010.0 + 20.2)


def test_sand_and_stone_differ_in_the_same_plan(runtime: PlantRuntime) -> None:
    _select(runtime)
    run(runtime, "moist.reading", {"material": "sand", "moisture_percent": 7.0})
    run(runtime, "moist.reading", {"material": "gravel", "moisture_percent": 1.0})
    run(runtime, "batch.declare", {"batch_id": "B-1", "quantity": 1.0})
    plan = runtime.moist.revisions.for_batch("B-1").latest.plan
    sand_water = plan.material_free_water("sand")
    gravel_water = plan.material_free_water("gravel")
    assert sand_water == pytest.approx(53.2)
    assert gravel_water == pytest.approx(10.1)
    assert sand_water != gravel_water


def test_aggregate_without_reading_falls_back_to_reference_table(runtime: PlantRuntime) -> None:
    _select(runtime)
    # Only sand has a live reading; gravel uses its table reference (1.2).
    run(runtime, "moist.reading", {"material": "sand", "moisture_percent": 6.0})
    run(runtime, "batch.declare", {"batch_id": "B-1", "quantity": 1.0})
    plan = runtime.moist.revisions.for_batch("B-1").latest.plan
    assert plan.moisture_per_material["gravel"] == pytest.approx(1.2)
    assert plan.moisture_per_material["sand"] == pytest.approx(6.0)


def test_wet_materials_cover_every_aggregate() -> None:
    assert "sand" in WET_MATERIALS
    assert "gravel" in WET_MATERIALS
    assert "manufactured-sand" in WET_MATERIALS


def test_normalise_moisture_rejects_unknown_material() -> None:
    from mixplant.errors import ValidationError

    with pytest.raises(ValidationError):
        normalise_moisture(
            _simple_recipe(),
            0.0,
            {"not-a-material": 3.0},
            ("sand", "gravel"),
        )


def _simple_recipe():
    from mixplant.recipe.grade import grade_for
    from mixplant.recipe.recipe import Recipe

    return Recipe(
        name="r",
        grade=grade_for("C30"),
        version=1,
        factors={"sand": 760.0, "gravel": 1010.0, "cement": 330.0, "water": 175.0},
        mix_time_ticks=6,
        moisture_ref_percent=4.5,
    )


def test_policy_is_per_aggregate_and_fixed(runtime: PlantRuntime) -> None:
    _select(runtime)
    run(runtime, "moist.set_policy", {"material": "gravel", "policy": "forward_only"})
    assert runtime.moist.ledger.policy_for("gravel") is RetroPolicy.FORWARD_ONLY
    assert runtime.moist.ledger.policy_for("sand") is RetroPolicy.RETROACTIVE
    # The policy lands on the stream and survives a query of the state.
    run(runtime, "batch.declare", {"batch_id": "B-1", "quantity": 1.0})
    state = runtime.state()
    assert state["subsystems"]["moist"]["ledger"]["policies"]["gravel"] == "forward_only"


def test_plan_batch_blanket_percentage_kept_for_legacy_callers() -> None:
    plan = plan_batch(_simple_recipe(), batch_id="B", quantity=1.0, moisture_percent=4.0,
                      wet_materials=("sand", "gravel"))
    assert plan.moisture_per_material["sand"] == pytest.approx(4.0)
    assert plan.moisture_per_material["gravel"] == pytest.approx(4.0)
    assert plan.free_water_l == pytest.approx(0.04 * (760.0 + 1010.0))


def test_per_aggregate_policy_loads_from_station_config() -> None:
    from mixplant.config import StationConfig

    cfg = StationConfig.from_mapping(
        {"moisture": {"late_policy": {"sand": "forward_only", "gravel": "retroactive"}}}
    )
    assert cfg.moisture.late_policy["sand"] == "forward_only"
    assert cfg.moisture.late_policy["gravel"] == "retroactive"
    runtime = PlantRuntime(cfg, storage="memory")
    runtime.bootstrap()
    assert runtime.moist.ledger.policy_for("sand") is RetroPolicy.FORWARD_ONLY
    assert runtime.moist.ledger.policy_for("gravel") is RetroPolicy.RETROACTIVE
