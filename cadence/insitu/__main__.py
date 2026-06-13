"""`python -m insitu ...` entry point (run from cadence/, or with cadence/ on PYTHONPATH).
See insitu/cli.py for the subcommands."""
from .cli import main

if __name__ == "__main__":
    raise SystemExit(main())
