"""Attack scenarios: a wash trader, a monopolist with a second key, a Mint that under-remits.

    python3 -m sim.attacks wash          the Section 3.3 sweep over f and m
    python3 -m sim.attacks monopolist    a second key on the same hill against the map rule
    python3 -m sim.attacks mint          under-remitting during a conjunction, found at reconciliation

Each module builds its scenario in Python (so it can be swept), runs it, prints a table and
ends with one summary line of who lost or gained what. `run_*` functions return the figures
for tests.py.
"""

from ..engine import Run
from ..scenario import from_dict


def run_dict(raw, name):
    return Run(from_dict(raw, name)).go()
