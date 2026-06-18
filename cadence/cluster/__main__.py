"""`python -m cluster ...` -> the one-corner Donau+ALPS smoke runner.

Thin entrypoint delegating to cluster.run_corner.main(). Using `python -m cluster` (rather
than `python -m cluster.run_corner`) avoids the runpy double-import warning, because the
package __init__ eagerly imports the run_corner SUBMODULE (the pinned interface) and runpy
would otherwise re-execute it as __main__.
"""
import sys

from .run_corner import main

if __name__ == "__main__":
    sys.exit(main())
