"""Compatibility alias for the Growth Lab outer loop."""

import sys

from mneme.growth_lab import outer_loop as _implementation

sys.modules[__name__] = _implementation
