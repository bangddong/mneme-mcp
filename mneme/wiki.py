"""Compatibility alias for the legacy mutable Wiki API."""

import sys

from mneme.legacy import wiki as _implementation

sys.modules[__name__] = _implementation
