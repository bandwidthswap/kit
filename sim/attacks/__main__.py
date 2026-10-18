"""python3 -m sim.attacks wash | monopolist | mint"""

import sys

from sim.attacks import mint, monopolist, wash

ATTACKS = {"wash": wash.main, "monopolist": monopolist.main, "mint": mint.main}


def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    if not argv or argv[0] not in ATTACKS:
        print("usage: python3 -m sim.attacks %s" % "|".join(ATTACKS), file=sys.stderr)
        return 2
    return ATTACKS[argv[0]](argv[1:])


if __name__ == "__main__":
    sys.exit(main())
