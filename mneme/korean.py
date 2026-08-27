"""Compatibility module alias; use mneme.core.search.korean for new code."""

import sys

from mneme.core.search import korean as _implementation

sys.modules[__name__] = _implementation
