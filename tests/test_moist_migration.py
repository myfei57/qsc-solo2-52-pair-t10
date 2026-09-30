"""Late probe readings: trace back to the batches they belonged to.

Covers:
* retroactive aggregates: a late reading recalculates every still open batch
  drawn from the same pile (sand, gravel/manufactured sand default);
* forward only aggregates: the reading changes new batches only and the
  skipped batches are explained;
* a pile change stops the reading being traced onto the old pile's batches;
* water already metered freezes a batch against migration;
* recalculation is idempotent (no double water deduction) and keeps the
  pre-migration revision;
* reconcile ties 配比 / 盘点 / 审计 together;
* a restart replays to the identical, already migrated state.
"""

from __future__ import annotations

import shutil
import tempfile
from pathlib import Path

import pytest

from mixplant.config import StationConfig
from mixplant.runtime import PlantRuntime
from tests.support import publish_and_select, run


def _setup(runtime: PlantRuntime, batch_ids=("B-1",), quantity: float = 1.0) -> None:
    publish_and_select(runtime)
    run(runtime, "agg.map_bin", {"bin": "bin-1", "material": "sand"})
    run(runtime, "agg.map_bin", {"bin": "bin-2", "material": "gravel"})
    for batch_id in batch_ids:
        run(runtime, "batch.declare", {"batch_id": batch_id, "quantity": quantity})


def test_late_reading_traces_back_to_open_batches_on_same_pile(runtime: PlantRuntime) -> None:
    _setup(runtime, ["B-1", "B-2"])
    result = run(
        runtime,
        "moist.reading",
        {"material": "sand", "moisture_percent": 6.0, "observed_tick": 0, "reading_id": "R-1"},
    )
    batches = sorted(item["batch_id"] for item in result["settlement"]["migrations"])
    assert batches == ["B-1", "B-2"]
    for batch_id in ("B-1", "B-2"):
        history = runtime.moist.revisions.for_batch(batch_id)
        assert [item.revision for item in history.revisions()] == [1, 2]
        assert history.latest.basis == "migrated"


def test_pre_migration_revision_is_retained(runtime: PlantRuntime) -> None:
    _setup(runtime, ["B-1"])
    run(
        runtime,
        "moist.reading",
        {"material": "sand", "moisture_percent": 6.0, "observed_tick": 0, "reading_id": "R-1"},
    )
    history = runtime.moist.revisions.for_batch("B-1")
    revision_one = history.at(1)
    assert revision_one.basis == "declared"
    # Migration semantics: the earlier plan version stays queryable.
    assert revision_one.plan.moisture_per_material["sand"] == pytest.approx(4.5)
    assert history.latest.plan.moisture_per_material["sand"] == pytest.approx(6.0)


def test_migration_does_not_deduct_water_twice_on_redelivery(runtime: PlantRuntime) -> None:
    _setup(runtime, ["B-1", "B-2"])
    payload = {"material": "sand", "moisture_percent": 6.0, "observed_tick": 0, "reading_id": "R-1"}
    first = run(runtime, "moist.reading", payload)
    second = run(runtime, "moist.reading", payload)
    assert len(first["settlement"]["migrations"]) == 2
    assert second["settlement"]["duplicate"] is True
    assert second["settlement"]["migrations"] == []
    # Exactly two migrations remain in the audit stream (one per batch).
    assert len(runtime.moist.ledger.migrations()) == 2
    report = run(runtime, "moist.reconcile", {})
    # The correction is counted once on each batch.
    assert report["ok"] is True
    batch_deltas = {row["batch_id"]: row["migration_water_delta_l"] for row in report["batches"]}
    assert batch_deltas["B-1"] == pytest.approx(0.015 * 760.0)
    assert batch_deltas["B-2"] == pytest.approx(0.015 * 760.0)


def test_second_late_reading_corrects_again_but_once(runtime: PlantRuntime) -> None:
    _setup(runtime, ["B-1"])
    run(
        runtime,
        "moist.reading",
        {"material": "sand", "moisture_percent": 6.0, "observed_tick": 0, "reading_id": "R-1"},
    )
    run(
        runtime,
        "moist.reading",
        {"material": "sand", "moisture_percent": 7.0, "observed_tick": 0, "reading_id": "R-2"},
    )
    history = runtime.moist.revisions.for_batch("B-1")
    assert [item.revision for item in history.revisions()] == [1, 2, 3]
    # cumulative correction equals the move from declared 4.5 -> 7.0 once.
    assert history.cumulative_water_delta_l() == pytest.approx((7.0 - 4.5) / 100.0 * 760.0)
    assert len(runtime.moist.ledger.migrations(batch_id="B-1")) == 2
    report = run(runtime, "moist.reconcile", {})
    assert report["ok"] is True


def test_forward_only_aggregate_keeps_old_batches_and_explains_why(
    runtime: PlantRuntime,
) -> None:
    _setup(runtime, ["B-1", "B-2"])
    run(runtime, "moist.set_policy", {"material": "sand", "policy": "forward_only"})
    result = run(
        runtime,
        "moist.reading",
        {"material": "sand", "moisture_percent": 7.0, "observed_tick": 0, "reading_id": "F-1"},
    )
    assert result["settlement"]["migrations"] == []
    non = result["settlement"]["nonmigration"]
    assert non["reason"] == "policy_forward_only"
    assert sorted(non["affected_batches"]) == ["B-1", "B-2"]
    # The batches keep their declared moisture.
    for batch_id in ("B-1", "B-2"):
        history = runtime.moist.revisions.for_batch(batch_id)
        assert [item.revision for item in history.revisions()] == [1]
    # The reading is nevertheless authoritative for later batches.
    run(runtime, "batch.declare", {"batch_id": "B-3", "quantity": 1.0})
    plan = runtime.moist.revisions.for_batch("B-3").latest.plan
    assert plan.moisture_per_material["sand"] == pytest.approx(7.0)


def test_pile_change_stops_traceback_onto_the_old_pile(runtime: PlantRuntime) -> None:
    _setup(runtime, ["B-1"])
    run(runtime, "moist.change_pile", {"material": "sand", "pile_id": "pile-2"})
    result = run(
        runtime,
        "moist.reading",
        {"material": "sand", "moisture_percent": 7.0, "observed_tick": 0, "reading_id": "P-1"},
    )
    # Reading defaults to the *current* pile (pile-2); B-1 belongs to pile-1.
    assert result["settlement"]["migrations"] == []
    assert result["settlement"]["nonmigration"]["reason"] == "no_open_batch_for_pile"
    history = runtime.moist.revisions.for_batch("B-1")
    assert history.latest.revision == 1


def test_water_closed_batch_is_explained_not_migrated(runtime: PlantRuntime) -> None:
    publish_and_select(runtime)
    run(runtime, "agg.map_bin", {"bin": "bin-1", "material": "sand"})
    run(runtime, "agg.map_bin", {"bin": "bin-2", "material": "gravel"})
    run(runtime, "batch.declare", {"batch_id": "B-1", "quantity": 6.0})
    run(runtime, "moist.reading", {"material": "sand", "moisture_percent": 4.5})
    run(runtime, "moist.reading", {"material": "gravel", "moisture_percent": 1.2})
    targets = runtime.agg.describe_targets()["targets"]
    for bin_name in sorted(targets):
        run(runtime, "agg.weigh", {"bin": bin_name, "actual_kg": targets[bin_name]})
    run(runtime, "agg.persist")
    run(
        runtime,
        "agg.check",
        {"actual_total_kg": runtime.agg.describe_targets()["total_kg"] - 1050.0},
    )
    run(runtime, "agg.drop")
    run(runtime, "mix.start")
    run(runtime, "agg.release", {"residual_kg": 3.0})
    run(runtime, "water.open_valve", {"target_l": 1000})
    run(runtime, "water.dispense", {"litres": 1000, "rate_lps": 45})

    result = run(
        runtime,
        "moist.reading",
        {"material": "sand", "moisture_percent": 8.0, "observed_tick": 0, "reading_id": "W-1"},
    )
    assert result["settlement"]["migrations"] == []
    assert result["settlement"]["nonmigration"]["reason"] == "batch_water_already_closed"
    assert result["settlement"]["nonmigration"]["affected_batches"] == ["B-1"]


def test_reconcile_ties_mix_design_stocktake_and_audit(runtime: PlantRuntime) -> None:
    _setup(runtime, ["B-1", "B-2"])
    run(
        runtime,
        "moist.reading",
        {"material": "sand", "moisture_percent": 6.0, "observed_tick": 0, "reading_id": "R-1"},
    )
    run(
        runtime,
        "moist.reading",
        {"material": "gravel", "moisture_percent": 3.0, "observed_tick": 0, "reading_id": "R-2"},
    )
    report = run(runtime, "moist.reconcile", {})
    assert report["ok"] is True
    assert report["checked_batches"] == 2
    # Audit migration total equals the stock-take total.
    assert report["migration_total_delta_l"] == pytest.approx(report["ledger_total_delta_l"])
    assert report["stocktake_total_delta_l"] == pytest.approx(report["migration_total_delta_l"])
    # Plan free water equals ledger free water for every checked batch.
    for batch in report["batches"]:
        assert batch["plan_free_water_l"] == pytest.approx(batch["ledger_free_water_l"])
        assert batch["stocktake_water_delta_l"] == pytest.approx(batch["migration_water_delta_l"])
        assert batch["differences"] == []


def test_stocktake_command_reports_open_water_position(runtime: PlantRuntime) -> None:
    _setup(runtime, ["B-1", "B-2"])
    run(
        runtime,
        "moist.reading",
        {"material": "sand", "moisture_percent": 6.0, "observed_tick": 0, "reading_id": "R-1"},
    )
    stock = run(runtime, "moist.stocktake", {})
    rows = {row["batch_id"]: row for row in stock["batches"]}
    expected = pytest.approx(0.015 * 760.0)
    assert rows["B-1"]["corrected_water_delta_l"] == expected
    assert rows["B-2"]["corrected_water_delta_l"] == expected
    # water_owed is the operational sign: more free water found -> owe less
    # batching water, so owed is the negative of the correction.
    assert rows["B-1"]["water_owed_l"] == pytest.approx(-0.015 * 760.0)


def test_migration_results_land_in_audit_stream(runtime: PlantRuntime) -> None:
    _setup(runtime, ["B-1"])
    run(
        runtime,
        "moist.reading",
        {"material": "sand", "moisture_percent": 6.0, "observed_tick": 0, "reading_id": "R-1"},
    )
    rows = runtime.records({"kind": "moist.migration"})["rows"]
    assert len(rows) == 1
    assert rows[0]["payload"]["reading_id"] == "R-1"
    assert rows[0]["payload"]["batch_id"] == "B-1"
    revisions = runtime.records({"kind": "moist.revision"})["rows"]
    bases = [row["payload"]["basis"] for row in revisions]
    assert bases.count("declared") == 1
    assert bases.count("migrated") == 1


def test_migration_is_filed_in_the_audit_ledger(runtime: PlantRuntime) -> None:
    _setup(runtime, ["B-1"])
    run(
        runtime,
        "moist.reading",
        {"material": "sand", "moisture_percent": 6.0, "observed_tick": 0, "reading_id": "R-1"},
    )
    names = [entry["name"] for entry in runtime.audit.recent(50)]
    assert "moist.water_corrected" in names
    assert "moist.migrated" in names
    # The signed correction the ledger totals matches the migration delta.
    corrected = [
        entry for entry in runtime.audit.recent(50) if entry["name"] == "moist.water_corrected"
    ]
    assert corrected[0]["detail"]["water_delta_l"] == pytest.approx(0.015 * 760.0)
    assert corrected[0]["detail"]["batch_id"] == "B-1"


def test_forward_only_reason_is_filed_in_the_audit_ledger(runtime: PlantRuntime) -> None:
    _setup(runtime, ["B-1"])
    run(runtime, "moist.set_policy", {"material": "sand", "policy": "forward_only"})
    run(
        runtime,
        "moist.reading",
        {"material": "sand", "moisture_percent": 8.0, "observed_tick": 0, "reading_id": "F-1"},
    )
    names = [entry["name"] for entry in runtime.audit.recent(50)]
    assert "moist.reading_not_migrated" in names


def test_restart_replays_to_the_same_migrated_state() -> None:
    data_dir = tempfile.mkdtemp()
    try:
        def boot() -> PlantRuntime:
            cfg = StationConfig().with_overrides(data_dir=Path(data_dir))
            plant = PlantRuntime(cfg, storage="file")
            plant.bootstrap()
            return plant

        first = boot()
        _setup(first, ["B-1", "B-2"])
        first.moist.reading(
            material="sand",
            moisture_percent=6.0,
            observed_tick=0,
            reading_id="R-1",
        )
        from mixplant.moist.reconcile import reconcile

        before = reconcile(first.moist.ledger, first.moist.revisions).describe()
        first.close()

        second = boot()
        after = reconcile(second.moist.ledger, second.moist.revisions).describe()
        assert after["ok"] == before["ok"] is True
        assert after["migration_total_delta_l"] == before["migration_total_delta_l"]
        for batch_id in ("B-1", "B-2"):
            revisions = [
                (item.revision, item.basis)
                for item in second.moist.revisions.for_batch(batch_id).revisions()
            ]
            assert revisions == [(1, "declared"), (2, "migrated")]
        # Redelivery after restart still does not double-correct.
        redelivered = second.moist.reading(
            material="sand", moisture_percent=6.0, observed_tick=0, reading_id="R-1"
        )
        assert redelivered["settlement"]["duplicate"] is True
        assert reconcile(second.moist.ledger, second.moist.revisions).describe()[
            "migration_total_delta_l"
        ] == before["migration_total_delta_l"]
        second.close()
    finally:
        shutil.rmtree(data_dir, ignore_errors=True)


def test_late_manufactured_sand_is_traced_under_retroactive_default(
    runtime: PlantRuntime,
) -> None:
    # manufactured-sand is a retroactive aggregate even though it is not in
    # the default recipe; policy defaults are independent of the recipe.
    assert runtime.moist.ledger.policy_for("manufactured-sand").retroactive is True


def test_console_status_carries_the_migration_view(runtime: PlantRuntime) -> None:
    _setup(runtime, ["B-1"])
    run(
        runtime,
        "moist.reading",
        {"material": "sand", "moisture_percent": 6.0, "observed_tick": 0, "reading_id": "R-1"},
    )
    moist = runtime.state()["subsystems"]["moist"]
    assert moist["ledger"]["migrations"] == 1
    assert moist["reconciliation"]["ok"] is True
    assert moist["ledger"]["authoritative"]
    report = run(runtime, "moist.migrations", {})
    assert report["migrations"][0]["batch_id"] == "B-1"
    assert report["nonmigrations"] == []
