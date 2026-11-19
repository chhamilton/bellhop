"""Allow invoking the CLI via `python -m bellhop`."""

import sys

from bellhop.cli import main

if __name__ == "__main__":
  sys.exit(main())
