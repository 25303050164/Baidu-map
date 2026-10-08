import gc
import ipaddress
import socket
import threading
from asyncio import proactor_events, selector_events

import pytest

# External connections are refused for the whole test session, before any app module
# is imported: backend/.env may hold a real AK, and a test that forgot to inject a fake
# provider must fail loudly instead of spending quota. Loopback stays open for the
# in-process servers some tests start.
EXTERNAL = {"attempts": 0, "targets": []}
_GUARD_LOCK = threading.Lock()


def _refuse(target) -> None:
    with _GUARD_LOCK:
        EXTERNAL["attempts"] += 1
        EXTERNAL["targets"].append(str(target)[:80])
    raise ConnectionRefusedError("test session: external connections are refused")


def _loopback(host) -> bool:
    if host in (None, "", "localhost"):
        return True
    try:
        return ipaddress.ip_address(str(host).split("%")[0]).is_loopback
    except ValueError:
        return False


def _address_host(address):
    return address[0] if isinstance(address, tuple) else address


_getaddrinfo = socket.getaddrinfo


def _guarded_getaddrinfo(host, *args, **kwargs):
    if isinstance(host, bytes):
        host = host.decode("ascii", "replace")
    if not _loopback(host):
        _refuse(host)
    return _getaddrinfo(host, *args, **kwargs)


def _guard_connect(original):
    def connect(self, address):
        if self.family in (socket.AF_INET, socket.AF_INET6) and not _loopback(_address_host(address)):
            _refuse(_address_host(address))
        return original(self, address)
    return connect


def _guard_sock_connect(original):
    async def sock_connect(self, sock, address):
        if sock.family in (socket.AF_INET, socket.AF_INET6) and not _loopback(_address_host(address)):
            _refuse(_address_host(address))
        return await original(self, sock, address)
    return sock_connect


socket.getaddrinfo = _guarded_getaddrinfo
socket.socket.connect = _guard_connect(socket.socket.connect)
socket.socket.connect_ex = _guard_connect(socket.socket.connect_ex)
# On Windows asyncio connects through ConnectEx, not socket.connect: guard both loops.
for _loop in (proactor_events.BaseProactorEventLoop, selector_events.BaseSelectorEventLoop):
    _loop.sock_connect = _guard_sock_connect(_loop.sock_connect)


def pytest_terminal_summary(terminalreporter):
    if EXTERNAL["attempts"]:
        terminalreporter.write_line(
            f"external connection attempts refused: {EXTERNAL['attempts']} "
            f"(first targets: {EXTERNAL['targets'][:5]})", red=True)


@pytest.fixture(autouse=True)
def _thaw_the_heap():
    """Loading the offline graph freezes the heap: in service it lives as long as the
    process. Tests load many small graphs; thawed after each, whatever a test leaves
    behind is still collected."""
    yield
    gc.unfreeze()
