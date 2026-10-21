"""Keep offline validation traffic on loopback; no provider credentials are used."""

import ipaddress
import socket


def _permit(address):
    if not isinstance(address, tuple):
        return
    host = address[0]
    if host == "localhost":
        return
    try:
        if ipaddress.ip_address(host).is_loopback:
            return
    except ValueError:
        pass
    raise RuntimeError("Offline validation blocked non-loopback network access")


_connect = socket.socket.connect
_connect_ex = socket.socket.connect_ex


def _guarded_connect(self, address):
    _permit(address)
    return _connect(self, address)


def _guarded_connect_ex(self, address):
    _permit(address)
    return _connect_ex(self, address)


socket.socket.connect = _guarded_connect
socket.socket.connect_ex = _guarded_connect_ex
