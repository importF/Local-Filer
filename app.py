"""PyInstaller entry point.

PyInstaller runs the entry script as the top-level ``__main__`` module, which
has no parent package — so ``localfiler/main.py``'s relative imports
(``from . import config``) fail there with "attempted relative import with no
known parent package". This thin launcher lives *outside* the package and
imports it absolutely, so ``localfiler`` is a real package again and its
relative imports resolve. Dev still runs the app via ``python -m localfiler.main``.
"""

from __future__ import annotations

import sys

from localfiler.main import main

if __name__ == "__main__":
    sys.exit(main())
