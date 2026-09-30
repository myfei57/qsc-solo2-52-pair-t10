"""Water stock take (盘点) implied by moisture migrations.

The stock take answers one operational question per batch: *how much batching
water do we still owe this batch (negative) or how much extra did it get
(positive), after every late reading that was traced back to it?*

It is a pure projection of the committed migration rows, so:

* it rebuilds to the identical total on restart (no journal of its own);
* the signed sum here is the number the audit migration rows must total to,
  which is the 配比 / 盘点 / 审计 cross check in ``reconcile.py``.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from .ledger import Migration


@dataclass(frozen=True, slots=True)
class WaterStockRow:
    """The open water position of one batch."""

    batch_id: str
    corrected_water_delta_l: float
    migration_count: int

    def describe(self) -> dict[str, Any]:
        return {
            "batch_id": self.batch_id,
            "water_owed_l": round(-self.corrected_water_delta_l, 4),
            "corrected_water_delta_l": round(self.corrected_water_delta_l, 4),
            "migration_count": self.migration_count,
        }


class WaterStockTake:
    """Accumulates the signed water correction of every migrated batch."""

    __slots__ = ("_rows",)

    def __init__(self) -> None:
        self._rows: dict[str, WaterStockRow] = {}

    def apply(self, migration: Migration) -> WaterStockRow:
        """Fold one committed migration into the batch's water position."""

        current = self._rows.get(
            migration.batch_id,
            WaterStockRow(
                batch_id=migration.batch_id,
                corrected_water_delta_l=0.0,
                migration_count=0,
            ),
        )
        row = WaterStockRow(
            batch_id=migration.batch_id,
            corrected_water_delta_l=round(current.corrected_water_delta_l + migration.water_delta_l, 4),
            migration_count=current.migration_count + 1,
        )
        self._rows[migration.batch_id] = row
        return row

    def row(self, batch_id: str) -> WaterStockRow | None:
        return self._rows.get(batch_id)

    def rows(self) -> tuple[WaterStockRow, ...]:
        return tuple(self._rows[batch_id] for batch_id in sorted(self._rows))

    def total_delta_l(self) -> float:
        return round(sum(row.corrected_water_delta_l for row in self._rows.values()), 4)

    def describe(self) -> dict[str, Any]:
        return {
            "total_corrected_water_delta_l": self.total_delta_l(),
            "batches": [row.describe() for row in self.rows()],
        }

    @classmethod
    def from_migrations(cls, migrations: tuple[Migration, ...]) -> "WaterStockTake":
        stock = cls()
        for migration in sorted(migrations, key=lambda item: (item.tick, item.migration_id)):
            stock.apply(migration)
        return stock
