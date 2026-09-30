"""Console exposure of per aggregate moisture and migration results."""

from __future__ import annotations

import json

from mixplant.console.router import Request
from mixplant.console.run import build_router
from mixplant.runtime import PlantRuntime
from mixplant.console.pages import PageStore
from tests.support import publish_and_select, run


def _router(runtime: PlantRuntime):
    return build_router(runtime, PageStore(runtime.config.web_dir))


def test_moist_get_exposes_reading_migration_and_reconcile(runtime: PlantRuntime) -> None:
    publish_and_select(runtime)
    run(runtime, "agg.map_bin", {"bin": "bin-1", "material": "sand"})
    run(runtime, "agg.map_bin", {"bin": "bin-2", "material": "gravel"})
    run(runtime, "batch.declare", {"batch_id": "B-1", "quantity": 1.0})
    run(
        runtime,
        "moist.reading",
        {"material": "sand", "moisture_percent": 6.0, "observed_tick": 0, "reading_id": "R-1"},
    )
    router = _router(runtime)
    response = router.handle(Request(method="GET", path="/api/moist"))
    body = json.loads(response.text)
    assert body["reconcile"]["ok"] is True
    assert body["migrations"][0]["batch_id"] == "B-1"
    assert body["readings"][0]["reading_id"] == "R-1"
    assert body["status"]["ledger"]["migrations"] == 1


def test_moist_reading_post_route(runtime: PlantRuntime) -> None:
    publish_and_select(runtime)
    router = _router(runtime)
    response = router.handle(
        Request(
            method="POST",
            path="/api/moist/reading",
            body={"material": "sand", "moisture_percent": 5.0},
        )
    )
    body = json.loads(response.text)
    assert body["result"]["reading"]["material"] == "sand"
    assert body["watermark"]["committed_seq"] >= 1


def test_forward_only_policy_route_explains_nonmigration(runtime: PlantRuntime) -> None:
    publish_and_select(runtime)
    run(runtime, "agg.map_bin", {"bin": "bin-1", "material": "sand"})
    run(runtime, "agg.map_bin", {"bin": "bin-2", "material": "gravel"})
    run(runtime, "batch.declare", {"batch_id": "B-1", "quantity": 1.0})
    run(runtime, "moist.set_policy", {"material": "sand", "policy": "forward_only"})
    router = _router(runtime)
    response = router.handle(
        Request(
            method="POST",
            path="/api/moist/reading",
            body={"material": "sand", "moisture_percent": 8.0, "observed_tick": 0},
        )
    )
    body = json.loads(response.text)
    non = body["result"]["settlement"]["nonmigration"]
    assert non["reason"] == "policy_forward_only"
    assert non["affected_batches"] == ["B-1"]
