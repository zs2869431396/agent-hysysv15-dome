"""Module entry point: ``python -m hysys_tools``.

Kept separate from ``hysys_tools.main`` so that running the package as a module does
not import ``main`` twice, which raises a runpy warning.
"""
import sys

from .main import main

if __name__ == '__main__':
    sys.exit(main())
