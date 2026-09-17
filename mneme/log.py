"""Compatibility facade for the Growth Lab episode log."""

import sys

from mneme.growth_lab import log as _implementation


if __name__ == "__main__":
    _implementation.main()
else:
    sys.modules[__name__] = _implementation
