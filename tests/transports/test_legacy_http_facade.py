def test_server_facade_exports_legacy_transport():
    """The historical module path remains the same module for monkeypatch seams."""
    from mneme import server
    from mneme.transports import legacy_http

    assert server is legacy_http
