"""Request-local, elapsed-deadline transport for read-only origin inventory."""

import copy
import socket
import threading
import time
from typing import TYPE_CHECKING, Any, Protocol, cast

if TYPE_CHECKING:
    from urllib3._base_connection import (  # noqa: F401 -- quoted casts preserve runtime imports
        BaseHTTPConnection,
        BaseHTTPSConnection,
    )

    class _Sender(Protocol):
        def send(self, data: Any) -> None:
            ...


from urllib3._collections import RecentlyUsedContainer
from urllib3.connection import HTTPConnection, HTTPSConnection
from urllib3.connectionpool import HTTPConnectionPool, HTTPSConnectionPool
from urllib3.util.connection import allowed_gai_family

from app.schemas.quota import AccountingUnavailable

# A stuck platform resolver retains its slot until it actually returns.
_CAPACITY = threading.BoundedSemaphore(2)


class _Request:
    def __init__(self, deadline):
        self.deadline = deadline
        self.cancelled = threading.Event()
        self.lock = threading.Lock()
        self.sockets = []

    def check(self):
        if self.cancelled.is_set() or time.monotonic() >= self.deadline:
            raise AccountingUnavailable(
                "Native origin inventory elapsed deadline exceeded"
            )

    def track(self, sock):
        with self.lock:
            self.check()
            # A duplicate descriptor remains valid when TLS takes ownership of
            # the original descriptor; shutdown cancels the same connection.
            self.sockets.append((sock, sock.dup()))

    def close(self):
        with self.lock:
            self.cancelled.set()
            for original, sock in self.sockets:
                try:
                    sock.shutdown(socket.SHUT_RDWR)
                except OSError:
                    pass
                sock.close()
                original.close()
            self.sockets.clear()


def _connect(connection, request):
    request.check()
    addresses = socket.getaddrinfo(
        connection._dns_host.strip("[]"),
        connection.port,
        allowed_gai_family(),
        socket.SOCK_STREAM,
    )
    request.check()  # No late connect after an uninterruptible resolver returns.
    error = OSError("Native origin inventory address unavailable")
    for family, kind, proto, _, address in addresses:
        request.check()
        sock = socket.socket(family, kind, proto)
        try:
            request.track(sock)
            sock.settimeout(max(0.001, request.deadline - time.monotonic()))
            for option in connection.socket_options or []:
                sock.setsockopt(*option)
            if connection.source_address:
                sock.bind(connection.source_address)
            request.check()
            sock.connect(address)
            request.check()
            return sock
        except OSError as exc:
            error = exc
            sock.close()
        except BaseException:
            sock.close()
            raise
    raise error


def _pool(original, request):
    class BoundedConnection:
        def _new_conn(self):
            return _connect(self, request)

        def send(self, data):
            request.check()
            connection = cast(HTTPConnection, self)
            if connection.sock is None:
                connection.connect()
            request.check()
            return cast("_Sender", super()).send(data)

    class HTTP(BoundedConnection, HTTPConnection):
        pass

    class HTTPS(BoundedConnection, HTTPSConnection):
        pass

    class HTTPPool(HTTPConnectionPool):
        ConnectionCls = cast("type[BaseHTTPConnection]", HTTP)

    class HTTPSPool(HTTPSConnectionPool):
        ConnectionCls = cast("type[BaseHTTPSConnection]", HTTPS)

    # Keep the actual manager's proxy, TLS, authentication and pool settings;
    # replace only its connection storage/classes, never global urllib3 state.
    pool = copy.copy(original)
    pool.connection_pool_kw = original.connection_pool_kw.copy()
    pool.pools = RecentlyUsedContainer(
        1, dispose_func=lambda value: cast(HTTPConnectionPool, value).close()
    )
    pool.pool_classes_by_scheme = {"http": HTTPPool, "https": HTTPSPool}
    return pool


def read(original, url, *, deadline, **kwargs):
    if not _CAPACITY.acquire(blocking=False):
        raise AccountingUnavailable(
            "Native origin inventory transport capacity unavailable"
        )
    request, done, result = _Request(deadline), threading.Event(), {}

    def run():
        pool = None
        try:
            request.check()
            pool = _pool(original, request)
            response = pool.request("GET", url, **kwargs)
            try:
                result["value"] = response.status, response.read(4 * 1024 * 1024 + 1)
                request.check()
            finally:
                response.close()
        except BaseException as exc:
            result["error"] = exc
        finally:
            request.close()
            if pool is not None:
                pool.clear()
            _CAPACITY.release()
            done.set()

    try:
        threading.Thread(
            target=run, daemon=True, name="native-origin-inventory"
        ).start()
    except BaseException:
        _CAPACITY.release()
        raise
    if (
        not done.wait(max(0, deadline - time.monotonic()))
        or time.monotonic() >= deadline
    ):
        request.close()
        raise AccountingUnavailable("Native origin inventory elapsed deadline exceeded")
    if "error" in result:
        raise result["error"]
    return result["value"]
