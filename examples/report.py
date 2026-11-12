# /// script
# requires-python = ">=3.11"
# dependencies = ["scriptrunner-metrics"]
#
# [tool.uv.sources]
# scriptrunner-metrics = { path = "../client" }
# ///
"""Example scheduled script: does a bit of work, reports metrics and exits."""

import os
import shutil

from scriptrunner_metrics import Counter, Gauge, Histogram

REPORTS = Counter("example_reports", "Reports written")  # keeps counting across runs
DISK_USED = Gauge("example_disk_used_ratio", "Used share of a filesystem", ["mount"])
LOAD = Gauge("example_load1", "One-minute load average")
DURATION = Histogram("example_report_duration_seconds", "Time taken to gather the report")

with DURATION.time():
    usage = shutil.disk_usage("/")
    load = os.getloadavg()[0]

DISK_USED.labels(mount="/").set(round(usage.used / usage.total, 4))
LOAD.set(load)
REPORTS.inc()

print(f"disk: {usage.used / usage.total:.0%} of {usage.total / 1e9:.0f} GB used")
print(f"load: {load:.2f}")
