"""Compatibility alias for the legacy LLM semantic helper module."""

import sys

from mneme.legacy import llm as _legacy_llm


sys.modules[__name__] = _legacy_llm
