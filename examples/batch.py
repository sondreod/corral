# /// script
# requires-python = ">=3.11"
# dependencies = ["corral"]
#
# [tool.uv.sources]
# corral = { path = ".." }   # this checkout; see "Metrics from your processes" in the README
# ///
"""Example script that reports metrics with corral.metrics: a batch every 15 seconds."""

import os
import shutil
import time

from corral.metrics import Counter, Gauge, Histogram

BATCHES = Counter("example_batches", "Batches run")
DISK_USED = Gauge("example_disk_used_ratio", "Used share of a filesystem", ["mount"])
LOAD = Gauge("example_load1", "One-minute load average")
DURATION = Histogram("example_batch_duration_seconds", "Time taken by a batch")

while True:
    with DURATION.time():
        usage = shutil.disk_usage("/")
        load = os.getloadavg()[0]
    DISK_USED.labels(mount="/").set(round(usage.used / usage.total, 4))
    LOAD.set(load)
    BATCHES.inc()
    print(f"batch done: disk {usage.used / usage.total:.0%} used, load {load:.2f}")
    time.sleep(15)
