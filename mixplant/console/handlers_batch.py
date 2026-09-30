"""Routes for the batching page and the aggregate commands."""

from __future__ import annotations

from typing import Any

from .response import inject_snapshot
from .router import Request, Response, Router

COMMANDS = (
    ("POST", "/api/batch/declare", "batch.declare"),
    ("POST", "/api/agg/map", "agg.map_bin"),
    ("POST", "/api/agg/weigh", "agg.weigh"),
    ("POST", "/api/agg/persist", "agg.persist"),
    ("POST", "/api/agg/check", "agg.check"),
    ("POST", "/api/agg/release", "agg.release"),
    ("POST", "/api/agg/drop", "agg.drop"),
    ("POST", "/api/agg/calibrate", "agg.calibrate"),
    ("POST", "/api/moist/compensate", "moist.compensate"),
    ("POST", "/api/moist/reading", "moist.reading"),
    ("POST", "/api/moist/backfill", "moist.backfill"),
    ("POST", "/api/moist/policy", "moist.policy"),
)


def register(router: Router, runtime: Any, pages: Any) -> None:
    """Register the batching routes."""

    router.add(
        "GET",
        "/pages/batch",
        lambda _: Response.html(inject_snapshot(pages.load("batch"), runtime.controller.batch_view())),
    )
    router.add("GET", "/api/batch", lambda _: Response.json(runtime.controller.batch_view()))
    router.add(
        "GET",
        "/api/moist/takestock",
        lambda _: Response.json(runtime.moist.takestock()),
    )
    router.add(
        "GET",
        "/api/moist/reconcile",
        lambda _: Response.json(runtime.moist.reconcile()),
    )
    router.add(
        "GET",
        "/api/moist/revisions/{batch_id}",
        lambda request: Response.json(runtime.moist.revisions(request.params["batch_id"])),
    )
    for method, path, command in COMMANDS:
        router.add(method, path, _command(runtime, command))


def _command(runtime: Any, command: str):
    def handler(request: Request) -> Response:
        return Response.json(runtime.dispatch(command, dict(request.body)))

    return handler
