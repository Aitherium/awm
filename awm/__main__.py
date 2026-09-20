"""`python -m awm` -- the invocation that still works when the console shim does not.

A pip-generated console script is an unsigned executable, and an application-control
policy can block one while its siblings run. This entry point goes through the signed
`python` interpreter instead, and it exists BEFORE the shim is blocked rather than after.

A delegation only -- the CLI lives in `cli.py`.
"""
from __future__ import annotations

import sys

from awm.cli import main

if __name__ == "__main__":
    sys.exit(main())
