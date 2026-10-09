"""Refuse every external connection inside a benchmark process.

The benchmark's transport is synthetic, so nothing in it should ever open a socket
off this machine. This guard exists for the case where a later caller wires a real
transport into a tool whose whole premise is "no live traffic": the accident then
fails loudly and is counted, instead of quietly spending a real account's quota.

It is deliberately standalone rather than imported from ``tests/conftest.py`` — a
tool must not depend on the test package, and that guard reports through pytest's
terminal writer, which does not exist here. Loopback stays open: the tool starts no
server, but a future one that does should not have to fight its own harness.
"""
import ipaddress
import socket
import threading
from asyncio import proactor_events, selector_events

#: How many refused targets are worth keeping. A run that retries in a loop would
#: otherwise grow this list without bound while the count keeps rising.
KEPT_TARGETS = 5


class Guard:
    """The counted refusal record a benchmark run reports at the end."""

    def __init__(self):
        self.attempts = 0
        self.targets: list[str] = []
        self._lock = threading.Lock()

    def refuse(self, target) -> None:
        with self._lock:
            self.attempts += 1
            if len(self.targets) < KEPT_TARGETS:
                self.targets.append(str(target)[:120])
        raise ConnectionRefusedError('benchmark session: external connections are refused')

    def report(self) -> dict:
        with self._lock:
            return {'attempts': self.attempts, 'firstTargets': list(self.targets)}


def _loopback(host) -> bool:
    if host in (None, '', 'localhost'):
        return True
    try:
        return ipaddress.ip_address(str(host).split('%')[0]).is_loopback
    except ValueError:
        return False


def _address_host(address):
    return address[0] if isinstance(address, tuple) else address


def install(guard: Guard) -> Guard:
    """Patch every send path a transport could take, before any client exists."""
    getaddrinfo = socket.getaddrinfo

    def guarded_getaddrinfo(host, *args, **kwargs):
        if isinstance(host, bytes):
            host = host.decode('ascii', 'replace')
        if not _loopback(host):
            guard.refuse(host)
        return getaddrinfo(host, *args, **kwargs)

    def guard_connect(original):
        def connect(self, address):
            if self.family in (socket.AF_INET, socket.AF_INET6) and not _loopback(_address_host(address)):
                guard.refuse(_address_host(address))
            return original(self, address)
        return connect

    def guard_sock_connect(original):
        async def sock_connect(self, sock, address):
            if sock.family in (socket.AF_INET, socket.AF_INET6) and not _loopback(_address_host(address)):
                guard.refuse(_address_host(address))
            return await original(self, sock, address)
        return sock_connect

    socket.getaddrinfo = guarded_getaddrinfo
    socket.socket.connect = guard_connect(socket.socket.connect)
    socket.socket.connect_ex = guard_connect(socket.socket.connect_ex)
    # Windows asyncio connects through ConnectEx rather than socket.connect, so both
    # loop implementations are guarded rather than only the one this platform uses.
    for loop in (proactor_events.BaseProactorEventLoop, selector_events.BaseSelectorEventLoop):
        loop.sock_connect = guard_sock_connect(loop.sock_connect)
    return guard
