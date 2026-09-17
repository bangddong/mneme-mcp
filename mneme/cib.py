"""Compatibility alias for the Growth Lab constitutional boundary."""

import sys

from mneme.growth_lab import cib as _implementation

sys.modules[__name__] = _implementation
