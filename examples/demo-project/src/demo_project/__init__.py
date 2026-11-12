"""Example project: `demo-clock` runs forever, `demo-greet` runs once."""

import sys
import time


def clock() -> None:
    while True:
        print(time.strftime("it is %H:%M:%S"))
        time.sleep(5)


def greet() -> int:
    names = sys.argv[1:] or ["world"]
    print("hello, " + " and ".join(names))
    return 0


def housekeeping() -> None:
    """Not declared in [project.scripts]; reachable as "demo_project:housekeeping"."""
    print("nothing to clean up")
