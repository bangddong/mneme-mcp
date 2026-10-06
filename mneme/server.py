"""Compatibility facade for Mneme's legacy HTTP MCP transport."""

import sys

from mneme.transports import legacy_http


if __name__ == "__main__":
    legacy_http.main()
else:
    sys.modules[__name__] = legacy_http
