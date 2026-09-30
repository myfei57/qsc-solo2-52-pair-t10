"""Per aggregate moisture, late reading migrations and the water take stock.

These tests pin down the rules the domain treats as fixed:

* each wet aggregate is compensated with its own percentage;
* a late reading is attributed by the tick it was *observed* at, so it
  reaches the batches it should have been in force for;
* whether it traces back at all is decided per aggregate by its policy;
* retroactive migration recomputes batch by batch, keeps the previous
  revision, is idempotent under a repeated trigger and books water once;
* recipe revisions, take stock and audit reconcile;
* a restart rebuilds the very same projection.
"""

from __future__ import annotations

import pytest

from mixplant.config import StationConfig
from mixplant.errors import InterlockBlockedError
from mixplant.moist.ledger import (
    SkipReason,
    material_percentages,
    plan_batch_under_readings,
)
from mixplant.moist.policy import RetroPolicy, default_policies
from mixplant.moist.reading import (
    effective_reading,
    idempotency_key,
    make_reading,
    reading_identity,
)
from mixplant.runtime import PlantRuntime
from tests.support import publish_and_select, run

FACTORS = {"sand": 760.0, "gravel": 1010.0, "cement": 330.0, "water": 175.0}


def _ready(runtime: PlantRuntime) -> None:
    publish_and_select(runtime)
    run(runtime, "agg.map_bin", {"bin": "bin-1", "material": "sand"})


def _declare(runtime: PlantRuntime, batch_id: str = "B-1", quantity: float = 6.0) -> None:
    run(runtime, "batch.declare", {"batch_id": batch_id, "quantity": quantity})


# ------------------------------------------------------------------- planning


def test_each_wet_aggregate_uses_its_own_percentage(runtime: PlantRuntime) -> None:
    _ready(runtime)
    run(runtime, "moist.reading", {"material": "sand", "moisture_percent": 5.0})
    run(runtime, "moist.reading", {"material": "gravel", "moisture_percent": 2.0})
    _declare(runtime)
    plan = runtime.agg._plan()
    assert plan.material_mass("sand") == pytest.approx(760 * 6 * 1.05)
    assert plan.material_mass("gravel") == pytest.approx(1010 * 6 * 1.02)
    assert plan.free_water_l == pytest.approx(760 * 6 * 0.05 + 1010 * 6 * 0.02)


def test_unread_aggregate_falls_back_to_its_own_reference(runtime: PlantRuntime) -> None:
    _ready(runtime)
    _declare(runtime)
    plan = runtime.agg._plan()
    # sand reference 4.5 and gravel reference 1.2 from the shipped table
    assert plan.material_mass("sand") == pytest.approx(760 * 6 * 1.045)
    assert plan.material_mass("gravel") == pytest.approx(1010 * 6 * 1.012)


def test_per_aggregate_reading_satisfies_the_dry_check_interlock(
    runtime: PlantRuntime,
) -> None:
    _ready(runtime)
    _declare(runtime)
    with pytest.raises(InterlockBlockedError):
        run(runtime, "agg.check", {"actual_total_kg": 12000})
    run(runtime, "moist.reading", {"material": "sand", "moisture_percent": 4.5})
    run(runtime, "moist.reading", {"material": "gravel", "moisture_percent": 1.2})
    targets = runtime.agg.describe_targets()["targets"]
    for bin_name in sorted(targets):
        run(runtime, "agg.weigh", {"bin": bin_name, "actual_kg": targets[bin_name]})
    run(runtime, "agg.persist")
    verdict = run(
        runtime,
        "agg.check",
        {"actual_total_kg": runtime.agg.describe_targets()["total_kg"] - 1050.0},
    )
    assert verdict["check"]["ok"] is True


# --------------------------------------------------------------- attribution


def test_late_reading_traces_back_to_the_batches_it_belonged_to(
    runtime: PlantRuntime,
) -> None:
    _ready(runtime)
    _declare(runtime, "B-1")
    run(runtime, "clock.advance", {"ticks": 3})
    _declare(runtime, "B-2")
    run(runtime, "clock.advance", {"ticks": 2})
    # observed after B-1 declared but before B-2 declared: only B-2 is in scope
    result = run(
        runtime,
        "moist.backfill",
        {"material": "sand", "moisture_percent": 7.0, "observed_tick": 2},
    )
    assert sorted(rev["batch_id"] for rev in result["revised"]) == ["B-2"]


def test_observation_at_zero_reaches_every_open_batch(runtime: PlantRuntime) -> None:
    _ready(runtime)
    _declare(runtime, "B-1")
    run(runtime, "clock.advance", {"ticks": 3})
    _declare(runtime, "B-2")
    run(runtime, "clock.advance", {"ticks": 2})
    result = run(
        runtime,
        "moist.backfill",
        {"material": "sand", "moisture_percent": 7.0, "observed_tick": 0},
    )
    assert sorted(rev["batch_id"] for rev in result["revised"]) == ["B-1", "B-2"]


def test_effective_reading_uses_the_newest_observation_not_arrival_order() -> None:
    readings = (
        make_reading(material="sand", moisture_percent=4.0, observed_tick=0, received_tick=5),
        make_reading(material="sand", moisture_percent=6.0, observed_tick=2, received_tick=6),
    )
    chosen = effective_reading(readings, material="sand", batch_tick=4)
    assert chosen is not None and chosen.moisture_percent == 6.0
    assert effective_reading(readings, material="sand", batch_tick=1).moisture_percent == 4.0


# -------------------------------------------------------------------- policy


def test_forward_only_material_never_rewrites_previous_batches(
    runtime: PlantRuntime,
) -> None:
    _ready(runtime)
    _declare(runtime, "B-1")
    run(runtime, "clock.advance", {"ticks": 2})
    result = run(
        runtime,
        "moist.backfill",
        {"material": "cement", "moisture_percent": 0.4, "observed_tick": 0},
    )
    assert result["retroactive"] is False
    assert result["revised"] == []
    reasons = {item["reason"] for item in result["skipped"]}
    assert SkipReason.POLICY_NEW_BATCHES_ONLY.value in reasons


def test_policy_can_be_changed_per_aggregate(runtime: PlantRuntime) -> None:
    _ready(runtime)
    _declare(runtime, "B-1")
    run(runtime, "clock.advance", {"ticks": 2})
    run(runtime, "moist.policy", {"material": "sand", "policy": "new_batches_only"})
    result = run(
        runtime,
        "moist.backfill",
        {"material": "sand", "moisture_percent": 7.0, "observed_tick": 0},
    )
    assert result["retroactive"] is False
    assert result["revised"] == []


def test_default_policies_migrate_sand_and_gravel_only() -> None:
    policies = default_policies()
    assert policies.is_retroactive("sand")
    assert policies.is_retroactive("gravel")
    assert not policies.is_retroactive("cement")
    assert policies.policy_for("stone-dust") is RetroPolicy.NEW_BATCHES_ONLY


# ------------------------------------------------------------- dropped cutoff


def _drop_first_batch(runtime: PlantRuntime) -> None:
    targets = runtime.agg.describe_targets()["targets"]
    for bin_name in sorted(targets):
        run(runtime, "agg.weigh", {"bin": bin_name, "actual_kg": targets[bin_name]})
    run(runtime, "agg.persist")
    run(runtime, "agg.drop")


def test_a_dropped_batch_cannot_be_retraced(runtime: PlantRuntime) -> None:
    _ready(runtime)
    _declare(runtime, "B-1")
    _drop_first_batch(runtime)
    run(runtime, "clock.advance", {"ticks": 3})
    result = run(
        runtime,
        "moist.backfill",
        {"material": "sand", "moisture_percent": 7.0, "observed_tick": 0},
    )
    assert result["revised"] == []
    skip = result["skipped"][0]
    assert skip["reason"] == SkipReason.BATCH_ALREADY_DROPPED.value
    assert result["dropped_batches"] == ["B-1"]


def test_late_reading_still_reaches_later_open_batches_after_a_drop(
    runtime: PlantRuntime,
) -> None:
    _ready(runtime)
    _declare(runtime, "B-1")
    _drop_first_batch(runtime)
    run(runtime, "clock.advance", {"ticks": 3})
    _declare(runtime, "B-2")
    result = run(
        runtime,
        "moist.backfill",
        {"material": "sand", "moisture_percent": 7.0, "observed_tick": 0},
    )
    revised = [rev["batch_id"] for rev in result["revised"]]
    assert revised == ["B-2"]
    assert [item["reason"] for item in result["skipped"]] == [
        SkipReason.BATCH_ALREADY_DROPPED.value
    ]


# ------------------------------------------------- migration, versions, stock


def test_retroactive_migration_keeps_the_previous_revision(
    runtime: PlantRuntime,
) -> None:
    _ready(runtime)
    _declare(runtime, "B-1")
    before = runtime.moist.revisions("B-1")
    assert before["current_revision"] == 0
    run(runtime, "clock.advance", {"ticks": 2})
    run(
        runtime,
        "moist.backfill",
        {"material": "sand", "moisture_percent": 7.0, "observed_tick": 0},
    )
    chain = runtime.moist.revisions("B-1")["revisions"]
    assert [item["revision"] for item in chain] == [0, 1]
    assert chain[0]["superseded"] is True
    assert chain[0]["superseded_by"] == 1
    assert chain[1]["superseded"] is False
    # the original free water figure is preserved on revision zero
    assert chain[0]["free_water_l"] < chain[1]["free_water_l"]


def test_migration_books_water_once_and_recomputes_batch_by_batch(
    runtime: PlantRuntime,
) -> None:
    _ready(runtime)
    _declare(runtime, "B-1", quantity=6.0)
    _declare(runtime, "B-2", quantity=6.0)
    run(runtime, "clock.advance", {"ticks": 2})
    result = run(
        runtime,
        "moist.backfill",
        {"material": "sand", "moisture_percent": 7.0, "observed_tick": 0},
    )
    per_batch = round(760 * 6 * (7.0 - 4.5) / 100.0, 4)
    assert [item["delta_l"] for item in result["adjustments"]] == pytest.approx(
        [per_batch, per_batch]
    )
    assert runtime.moist.takestock()["pending_water_delta_l"] == pytest.approx(
        2 * per_batch
    )


def test_repeated_backfill_trigger_is_idempotent(runtime: PlantRuntime) -> None:
    _ready(runtime)
    _declare(runtime, "B-1")
    run(runtime, "clock.advance", {"ticks": 2})
    payload = {"material": "sand", "moisture_percent": 7.0, "observed_tick": 0}
    first = run(runtime, "moist.backfill", payload)
    second = run(runtime, "moist.backfill", payload)
    assert len(first["revised"]) == 1
    assert second["revised"] == []
    assert second["adjustments"] == []
    assert second["idempotent"] is True
    # the water is only booked once
    assert runtime.moist.takestock()["count"] == 1


def test_a_changed_same_tick_reading_migrates_again_and_accumulates(
    runtime: PlantRuntime,
) -> None:
    _ready(runtime)
    _declare(runtime, "B-1")
    run(runtime, "clock.advance", {"ticks": 2})
    run(
        runtime,
        "moist.backfill",
        {"material": "sand", "moisture_percent": 7.0, "observed_tick": 0},
    )
    run(
        runtime,
        "moist.backfill",
        {"material": "sand", "moisture_percent": 8.0, "observed_tick": 0},
    )
    chain = runtime.moist.revisions("B-1")["revisions"]
    assert [item["revision"] for item in chain] == [0, 1, 2]
    stock = runtime.moist.takestock()
    assert stock["count"] == 2
    assert stock["pending_water_delta_l"] == pytest.approx(
        760 * 6 * (8.0 - 4.5) / 100.0
    )


def test_identity_and_idempotency_key_distinguish_a_corrected_reading() -> None:
    assert reading_identity("sand", 0) == "reading:sand:0"
    assert idempotency_key("sand", 0, 7.0) != idempotency_key("sand", 0, 8.0)
    assert idempotency_key("sand", 0, 7.0) == idempotency_key("sand", 0, 7.0)


# ----------------------------------------------------------------- reconcile


def test_recipe_stock_and_audit_reconcile(runtime: PlantRuntime) -> None:
    _ready(runtime)
    _declare(runtime, "B-1")
    run(runtime, "clock.advance", {"ticks": 2})
    run(
        runtime,
        "moist.backfill",
        {"material": "sand", "moisture_percent": 7.0, "observed_tick": 0},
    )
    report = runtime.moist.reconcile()
    assert report["ok"] is True
    assert report["revision_delta_total_l"] == report["stock_delta_total_l"]
    assert report["audit_delta_total_l"] == report["stock_delta_total_l"]
    assert report["rows"][0]["match"] is True


def test_retro_migration_is_audited(runtime: PlantRuntime) -> None:
    _ready(runtime)
    _declare(runtime, "B-1")
    run(runtime, "clock.advance", {"ticks": 2})
    run(
        runtime,
        "moist.backfill",
        {"material": "sand", "moisture_percent": 7.0, "observed_tick": 0},
    )
    audited = [
        record.payload.get("name")
        for record in runtime.store.history("audit.event")
        if str(record.payload.get("name", "")).startswith("moist.")
    ]
    assert "moist.batches_retraced" in audited


# ------------------------------------------------------------------ restart


def test_restart_rebuilds_revisions_stock_and_cutoff(tmp_path) -> None:
    config = StationConfig(data_dir=tmp_path / "state")
    first = PlantRuntime(config)
    first.bootstrap()
    _ready(first)
    _declare(first, "B-1")
    run(first, "clock.advance", {"ticks": 2})
    run(
        first,
        "moist.backfill",
        {"material": "sand", "moisture_percent": 7.0, "observed_tick": 0},
    )
    expected_stock = first.moist.takestock()["pending_water_delta_l"]
    first.close()

    second = PlantRuntime(config)
    second.bootstrap()
    chain = second.moist.revisions("B-1")["revisions"]
    assert [item["revision"] for item in chain] == [0, 1]
    assert chain[0]["superseded"] is True
    assert second.moist.takestock()["pending_water_delta_l"] == pytest.approx(expected_stock)
    assert second.moist.reconcile()["ok"] is True
    # the rebuilt cutoff still refuses to retrace a batch that was open live
    second.close()


def test_backfill_stays_idempotent_after_a_restart(tmp_path) -> None:
    config = StationConfig(data_dir=tmp_path / "state")
    first = PlantRuntime(config)
    first.bootstrap()
    _ready(first)
    _declare(first, "B-1")
    run(first, "clock.advance", {"ticks": 2})
    payload = {"material": "sand", "moisture_percent": 7.0, "observed_tick": 0}
    run(first, "moist.backfill", payload)
    first.close()

    second = PlantRuntime(config)
    second.bootstrap()
    repeated = run(second, "moist.backfill", payload)
    assert repeated["idempotent"] is True
    assert repeated["adjustments"] == []
    assert second.moist.takestock()["count"] == 1
    second.close()


# -------------------------------------------------------------- domain units


def test_ledger_recomputes_under_readings_without_a_service() -> None:
    from mixplant.recipe.grade import grade_for
    from mixplant.recipe.recipe import Recipe

    recipe = Recipe(
        name="m",
        grade=grade_for("C30"),
        version=1,
        factors=FACTORS,
        mix_time_ticks=6,
        moisture_ref_percent=4.5,
    )
    reading = make_reading(
        material="sand", moisture_percent=7.0, observed_tick=0, received_tick=3
    )
    plan = plan_batch_under_readings(
        recipe,
        batch_id="B-9",
        quantity=6.0,
        batch_tick=1,
        readings=(reading,),
        wet_materials=("sand", "gravel"),
    )
    assert plan.material_mass("sand") == pytest.approx(760 * 6 * 1.07)


def test_material_percentages_fall_back_per_aggregate() -> None:
    from mixplant.recipe.grade import grade_for
    from mixplant.recipe.recipe import Recipe

    recipe = Recipe(
        name="m",
        grade=grade_for("C30"),
        version=1,
        factors=FACTORS,
        mix_time_ticks=6,
        moisture_ref_percent=4.5,
    )
    percentages = material_percentages(
        (),
        recipe,
        batch_tick=0,
        wet_materials=("sand", "gravel"),
        references={"sand": 4.5, "gravel": 1.2},
    )
    assert percentages == {"sand": 4.5, "gravel": 1.2}
