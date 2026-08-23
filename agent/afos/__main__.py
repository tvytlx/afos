"""`python -m afos daemon|console` -- handy before the entry points are installed."""

import sys

USAGE = "usage: python -m afos {daemon|console} [args...]"


def main() -> int:
    if len(sys.argv) < 2 or sys.argv[1] in ("-h", "--help"):
        print(USAGE, file=sys.stderr)
        return 2
    which, sys.argv = sys.argv[1], [f"afos-{sys.argv[1]}"] + sys.argv[2:]
    if which == "daemon":
        from .daemon import main as run
    elif which == "console":
        from .client import main as run
    else:
        print(USAGE, file=sys.stderr)
        return 2
    return run()


if __name__ == "__main__":
    raise SystemExit(main())
