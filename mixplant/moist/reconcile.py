"""Three way reconciliation: mix design, stock take and audit must agree.

A retroactive migration changes three things at once:

1. **mix design (配比)** - the batch's latest plan revision carries the
   recalculated per material moisture, wet masses and free water.
2. **stock take (盘点)** - the moisture ledger's batch rows carry the same
   effective moisture and the cumulative water correction that is owed to /
   recoverable from the batch.
3. **audit (审计)** - exactly one migration record exists per (reading, batch,
   material) and the signed sum of their ``water_delta_l`` equals the
   correction the other two sides report.

``reconcile`` folds all three into one verdict.  It is a pure function of the
committed projections, so the console and the audit trail run the same check.
"""

from __future__ import annotations

import json
from dataclasses import dataclass

from .ledger import Migration, MoistureLedger
from .revisions import RevisionRegistry
from .stocktake import WaterStockTake

TOL = 1e-6


@dataclass(frozen=True, slots=True)
class BatchReconciliation:
    batch_id: str
    ok: bool
    plan_revision: int
    plan_free_water_l: float
    ledger_free_water_l: float
    ledger_water_delta_l: float
    stocktake_water_delta_l: float
    migration_water_delta_l: float
    migration_count: int
    differences: tuple[str, ...]

    def describe(self) -> dict[str, Any]:
        return {
            "batch_id": self.batch_id,
            "ok": self.ok,
            "plan_revision": self.plan_revision,
            "plan_free_water_l": self.plan_free_water_l,
            "ledger_free_water_l": self.ledger_free_water_l,
            "ledger_water_delta_l": self.ledger_water_delta_l,
            "stocktake_water_delta_l": self.stocktake_water_delta_l,
            "migration_water_delta_l": self.migration_water_delta_l,
            "migration_count": self.migration_count,
            "differences": list(self.differences),
        }


@dataclass(frozen=True, slots=True)
class ReconciliationReport:
    ok: bool
    checked_batches: int
    batches: tuple[BatchReconciliation, ...]
    migration_total_delta_l: float
    ledger_total_delta_l: float
    stocktake_total_delta_l: float

    def describe(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "checked_batches": self.checked_batches,
            "migration_total_delta_l": self.migration_total_delta_l,
            "ledger_total_delta_l": self.ledger_total_delta_l,
            "stocktake_total_delta_l": self.stocktake_total_delta_l,
            "batches": [item.describe() for item in self.batches],
        }

    def fingerprint(self) -> str:
        """Stable fingerprint a console page and audit row can compare."""

        body = {
            "ok": self.ok,
            "batches": [
                [
                    item.batch_id,
                    item.plan_revision,
                    round(item.plan_free_water_l, 4),
                    round(item.ledger_water_delta_l, 4),
                    round(item.stocktake_water_delta_l, 4),
                    round(item.migration_water_delta_l, 4),
                ]
                for item in self.batches
            ],
        }
        return json.dumps(body, sort_keys=True, separators=(",", ":"))


def _close(left: float, right: float) -> bool:
    return abs(round(left, 4) - round(right, 4)) <= TOL


def reconcile_batch(
    batch_id: str,
    *,
    ledger: MoistureLedger,
    revisions: RevisionRegistry,
    migrations: tuple[Migration, ...],
    stocktake_delta_l: float,
) -> BatchReconciliation:
    rows = ledger.batch_rows(batch_id)
    differences: list[str] = []
    plan_revision = 0
    plan_free_water = 0.0
    if revisions.has(batch_id):
        latest = revisions.for_batch(batch_id).latest
        plan_revision = latest.revision
        plan_free_water = latest.plan.free_water_l
    elif rows:
        differences.append("mix_design:missing_revision")
    ledger_free_water = round(sum(row.free_water_l for row in rows), 4)
    ledger_delta = round(sum(row.corrected_water_delta_l for row in rows), 4)
    migration_delta = round(sum(item.water_delta_l for item in migrations), 4)

    if rows and not _close(plan_free_water, ledger_free_water):
        differences.append("plan_vs_stocktake:free_water_l")

    # The signed water correction must be stated identically by the stock
    # take and by the audit migration rows.
    if not _close(ledger_delta, migration_delta):
        differences.append("stocktake_vs_audit:water_delta_l")
    # ... and the stock take projection derived from those same audit rows
    # must agree with the batch ledger (the 盘点 vs 配比 cross check).
    if not _close(stocktake_delta_l, ledger_delta):
        differences.append("plan_vs_stocktake:water_delta_l")

    # No (reading, material) may have corrected this batch more than once.
    seen: set[tuple[str, str]] = set()
    for item in migrations:
        key = (item.reading_id, item.material)
        if key in seen:
            differences.append("audit:duplicate_migration_deduction")
            break
        seen.add(key)

    # Effective moisture in the ledger must match the latest plan revision.
    if revisions.has(batch_id):
        plan_map = revisions.for_batch(batch_id).latest.plan.moisture_per_material
        for row in rows:
            planned = plan_map.get(row.material)
            if planned is not None and abs(planned - row.effective_percent) > TOL:
                differences.append(f"plan_vs_stocktake:moisture:{row.material}")

    return BatchReconciliation(
        batch_id=batch_id,
        ok=not differences,
        plan_revision=plan_revision,
        plan_free_water_l=plan_free_water,
        ledger_free_water_l=ledger_free_water,
        ledger_water_delta_l=ledger_delta,
        stocktake_water_delta_l=round(stocktake_delta_l, 4),
        migration_water_delta_l=migration_delta,
        migration_count=len(migrations),
        differences=tuple(differences),
    )


def reconcile(ledger: MoistureLedger, revisions: RevisionRegistry) -> ReconciliationReport:
    """Verify every batch the ledger and revision store know about."""

    # The stock take is rebuilt straight from the committed audit migration
    # rows, so it can never drift away from what 审计 recorded.
    stock = WaterStockTake.from_migrations(ledger.migrations())
    batch_ids = sorted({row.batch_id for row in ledger.all_batch_rows()} | set(revisions.batch_ids()))
    results: list[BatchReconciliation] = []
    for batch_id in batch_ids:
        stock_row = stock.row(batch_id)
        results.append(
            reconcile_batch(
                batch_id,
                ledger=ledger,
                revisions=revisions,
                migrations=ledger.migrations(batch_id=batch_id),
                stocktake_delta_l=0.0 if stock_row is None else stock_row.corrected_water_delta_l,
            )
        )
    migration_total = round(sum(item.water_delta_l for item in ledger.migrations()), 4)
    ledger_total = round(sum(item.ledger_water_delta_l for item in results), 4)
    return ReconciliationReport(
        ok=all(item.ok for item in results),
        checked_batches=len(results),
        batches=tuple(results),
        migration_total_delta_l=migration_total,
        ledger_total_delta_l=ledger_total,
        stocktake_total_delta_l=stock.total_delta_l(),
    )
