"""Compatibility alias for legacy Wiki validation."""

import sys

from mneme.core.validation import wiki as _implementation

sys.modules[__name__] = _implementation

if __name__ == "__main__":
    _implementation.main()
