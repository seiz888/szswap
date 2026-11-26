"""Allow ``python -m szswap`` in addition to the console script."""

from __future__ import annotations

from szswap.cli import main

if __name__ == "__main__":
    raise SystemExit(main())
