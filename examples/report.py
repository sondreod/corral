# /// script
# requires-python = ">=3.11"
# dependencies = []
# ///
"""Example scheduled script: does a bit of work and exits."""

import os
import shutil

usage = shutil.disk_usage("/")
print(f"disk: {usage.used / usage.total:.0%} of {usage.total / 1e9:.0f} GB used")
print(f"load: {os.getloadavg()[0]:.2f}")
