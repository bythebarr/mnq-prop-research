"""Allows ``python -m mnq_research ...`` as an alternative to the ``mnq`` command."""

from mnq_research.cli import main

if __name__ == "__main__":
    raise SystemExit(main())
