from __future__ import annotations

import xmlrpc.client


class _TimeoutTransport(xmlrpc.client.Transport):
    def __init__(self, timeout: float):
        super().__init__()
        self._timeout = timeout

    def make_connection(self, host):
        connection = super().make_connection(host)
        connection.timeout = self._timeout
        return connection


class _TimeoutSafeTransport(xmlrpc.client.SafeTransport):
    def __init__(self, timeout: float):
        super().__init__()
        self._timeout = timeout

    def make_connection(self, host):
        connection = super().make_connection(host)
        connection.timeout = self._timeout
        return connection


def server_proxy(url: str, timeout: float = 10) -> xmlrpc.client.ServerProxy:
    """XML-RPC proxy with a per-connection timeout (instead of mutating the global socket default)."""
    transport_cls = _TimeoutSafeTransport if url.lower().startswith('https') else _TimeoutTransport
    return xmlrpc.client.ServerProxy(url, transport=transport_cls(timeout), allow_none=True)
