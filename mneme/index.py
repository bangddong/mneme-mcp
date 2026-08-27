"""Compatibility alias for the legacy Wiki index module.

Assigning the implementation module object preserves legacy monkeypatching:
``mneme.index`` and ``mneme.legacy.wiki_index`` are the same module object.
"""

from mneme.legacy import wiki_index as _wiki_index
import sys


sys.modules[__name__] = _wiki_index
