import sys

from fmd.cli.app import main

if __name__ == "__main__":
    sys.argv[0] = "fmd"
    raise SystemExit(main())
