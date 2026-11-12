# /// script
# requires-python = ">=3.11"
# dependencies = ["rich"]
# ///
"""Example long-running script: prints a line every few seconds."""

import itertools
import sys
import time

from rich.console import Console

console = Console(highlight=False)

for n in itertools.count(1):
    console.print(f"tick {n}")
    if n % 5 == 0:
        print(f"warning: {n} is divisible by five", file=sys.stderr)
    time.sleep(3)
