"""Compatibility alias for the Growth Lab notifier."""

import sys

from mneme.growth_lab import notify as _implementation

sys.modules[__name__] = _implementation
