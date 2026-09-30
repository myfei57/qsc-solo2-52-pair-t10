"""Routes for per aggregate moisture, late reading migration and reconcile."""

from __future__ import annotations

from typing import Any

from .router import Request, Response, Router

COMMANDS = (
    ("POST", "/api/moist/reading", "moist.reading"),
    ("POST", "/api/moist/change_pile", "moist.change_pile"),
    ("POST", "/api/moist/policy", "moist.set_policy"),
    ("POST", "/api/moist/reconcile", "moist.reconcile"),
    ("POST", "/api/moist/compensate", "moist.compensate"),
)


def register(router: Router, runtime: Any, pages: Any) -> None:
    """Register the moisture routes."""

    router.add("GET", "/api/moist", lambda _: Response.json(runtime.controller.moist_view()))
    router.add("GET", "/api/moist/migrations", lambda _: Response.json(
        runtime.dispatch("moist.migrations", dict(_.query))
    ))
    for method, path, command in COMMANDS:
        router.add(method, path, _command(runtime, command))


def _command(runtime: Any, command: str):
    def handler(request: Request) -> Response:
        return Response.json(runtime.dispatch(command, dict(request.body)))

    return handler
