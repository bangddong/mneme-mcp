"""Compatibility alias for the Growth Lab scheduler."""

import sys

from mneme.growth_lab import scheduler as _implementation

sys.modules[__name__] = _implementation
