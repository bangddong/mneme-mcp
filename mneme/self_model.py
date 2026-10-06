"""Compatibility alias for the Growth Lab self model."""

import sys

from mneme.growth_lab import self_model as _implementation

sys.modules[__name__] = _implementation
