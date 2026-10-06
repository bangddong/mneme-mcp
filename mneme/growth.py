"""Compatibility facade for the Growth Lab action backlog."""

import sys

from mneme.growth_lab import growth as _implementation


if __name__ == "__main__":
    _implementation.main()
else:
    sys.modules[__name__] = _implementation
