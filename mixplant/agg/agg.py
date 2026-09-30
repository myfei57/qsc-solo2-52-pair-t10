"""Record kinds and constants shared by the aggregate services."""

from __future__ import annotations

WEIGH_KIND = "agg.weigh"
BATCH_KIND = "agg.batch"
PERSIST_KIND = "agg.persist"
DROP_KIND = "agg.drop"
BIN_KIND = "agg.bin"
CHECK_KIND = "agg.check"
RELEASE_KIND = "agg.release"
HOPPER_KIND = "agg.hopper"
CALIBRATE_KIND = "agg.calibrate"
REPORT_KIND = "agg.report"

# The wet aggregates that carry free water and need their own compensation.
# Each one is corrected with its own moisture percentage; the moisture
# subsystem decides whether a late reading rewrites earlier batches.
WET_MATERIALS: tuple[str, ...] = ("sand", "gravel")
