"""Compatibility alias for the Growth Lab constitution."""

import sys

from mneme.growth_lab import constitution as _implementation

sys.modules[__name__] = _implementation
