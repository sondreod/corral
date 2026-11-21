# /// script
# requires-python = ">=3.11"
# dependencies = []
# ///
"""Example script: reports disk use and load every 20 seconds."""

import os
import shutil
import time

while True:
    usage = shutil.disk_usage("/")
    print(f"disk: {usage.used / usage.total:.0%} of {usage.total / 1e9:.0f} GB used")
    print(f"load: {os.getloadavg()[0]:.2f}")
    time.sleep(20)
