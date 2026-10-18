"""python3 -m sim scenarios/street.toml [--by phase|day|window] [--windows A-B] [--csv PATH] [--log]"""

import argparse
import os
import sys

from .engine import Run
from .report import render, write_csv
from .scenario import ScenarioError, load


def main(argv=None):
    ap = argparse.ArgumentParser(prog="python3 -m sim", description="Run a Bandwidth Swap scenario and count the money.")
    ap.add_argument("scenario", help="a scenario file (TOML or JSON), or the name of one in sim/scenarios/")
    ap.add_argument("--by", choices=("phase", "day", "window"), help="group the flow table by phase (between events), day or window")
    ap.add_argument("--windows", help="print every row of these windows, e.g. 24-26 or 7")
    ap.add_argument("--csv", help="where to write the payment rows (default sim/out/<scenario>.csv)")
    ap.add_argument("--no-csv", action="store_true")
    ap.add_argument("--log", action="store_true", help="print every log line, reconciliations included")
    a = ap.parse_args(argv)
    try:
        scn = load(a.scenario)
    except ScenarioError as e:
        print("error:", e, file=sys.stderr)
        return 2
    run = Run(scn).go()
    detail = None
    if a.windows:
        if "-" in a.windows:
            x, y = a.windows.split("-", 1)
            detail = list(range(int(x), int(y) + 1))
        else:
            detail = [int(a.windows)]
    print(render(run, by=a.by, detail=detail, log=a.log))
    if not a.no_csv:
        path = a.csv or os.path.join(os.path.dirname(os.path.abspath(__file__)), "out", scn.name_slug() + ".csv")
        write_csv(run, path)
        print("\nrows written to %s" % os.path.relpath(path))
    return 0


if __name__ == "__main__":
    sys.exit(main())
