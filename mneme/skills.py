"""Compatibility alias for Growth Lab procedural memory."""

import sys

from mneme.growth_lab import skills as _implementation

sys.modules[__name__] = _implementation
