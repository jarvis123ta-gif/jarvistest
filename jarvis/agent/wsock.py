"""wsock.py — a minimal WebSocket client, standard library only.

Chrome DevTools Protocol runs over WebSocket, and pulling in `websockets`
would break the no-package-manager promise. This is the ~10% of RFC 6455 a
CDP client actually needs: the HTTP upgrade, masked client frames, and
reassembly of server frames. No extensions, no compression, no fragmentation
on send.
"""

from __future__ import annotations

import base64
import os
import socket
import struct
import urllib.parse

OP_CONT, OP_TEXT, OP_BIN, OP_CLOSE, OP_PING, OP_PONG = 0x0, 0x1, 0x2, 0x8, 0x9, 0xA


class WebSocketError(Exception):
    pass


class WebSocket:
    def __init__(self, url: str, timeout: float = 20.0):
        u = urllib.parse.urlparse(url)
        if u.scheme not in ("ws", "wss"):
            raise WebSocketError(f"not a websocket url: {url}")
        if u.scheme == "wss":
            raise WebSocketError("wss is not supported; CDP is local and plain ws")
        host = u.hostname or "127.0.0.1"
        port = u.port or 80
        path = u.path + (("?" + u.query) if u.query else "")

        self.sock = socket.create_connection((host, port), timeout=timeout)
        self.sock.settimeout(timeout)
        self._buf = b""
        self._handshake(host, port, path)

    # -- setup -----------------------------------------------------

    def _handshake(self, host: str, port: int, path: str) -> None:
        key = base64.b64encode(os.urandom(16)).decode()
        req = (
            f"GET {path} HTTP/1.1\r\n"
            f"Host: {host}:{port}\r\n"
            "Upgrade: websocket\r\n"
            "Connection: Upgrade\r\n"
            f"Sec-WebSocket-Key: {key}\r\n"
            "Sec-WebSocket-Version: 13\r\n\r\n"
        )
        self.sock.sendall(req.encode())
        head = b""
        while b"\r\n\r\n" not in head:
            chunk = self.sock.recv(4096)
            if not chunk:
                raise WebSocketError("connection closed during handshake")
            head += chunk
        header, _, rest = head.partition(b"\r\n\r\n")
        first = header.split(b"\r\n", 1)[0].decode("latin-1")
        if "101" not in first:
            raise WebSocketError(f"upgrade refused: {first}")
        self._buf = rest

    # -- frames ----------------------------------------------------

    def send(self, payload: str) -> None:
        data = payload.encode("utf-8")
        head = bytearray([0x80 | OP_TEXT])
        n = len(data)
        if n < 126:
            head.append(0x80 | n)
        elif n < 65536:
            head.append(0x80 | 126)
            head += struct.pack(">H", n)
        else:
            head.append(0x80 | 127)
            head += struct.pack(">Q", n)
        mask = os.urandom(4)
        head += mask
        masked = bytes(b ^ mask[i % 4] for i, b in enumerate(data))
        self.sock.sendall(bytes(head) + masked)

    def _read(self, n: int) -> bytes:
        while len(self._buf) < n:
            chunk = self.sock.recv(65536)
            if not chunk:
                raise WebSocketError("connection closed")
            self._buf += chunk
        out, self._buf = self._buf[:n], self._buf[n:]
        return out

    def recv(self) -> str:
        """Return the next complete text message, reassembling fragments."""
        parts: list[bytes] = []
        while True:
            b1, b2 = self._read(2)
            fin, opcode = b1 & 0x80, b1 & 0x0F
            length = b2 & 0x7F
            if length == 126:
                length = struct.unpack(">H", self._read(2))[0]
            elif length == 127:
                length = struct.unpack(">Q", self._read(8))[0]
            if b2 & 0x80:                      # server frames are never masked
                self._read(4)
            data = self._read(length) if length else b""

            if opcode == OP_CLOSE:
                raise WebSocketError("server closed the connection")
            if opcode == OP_PING:
                self._pong(data)
                continue
            if opcode == OP_PONG:
                continue
            parts.append(data)
            if fin:
                return b"".join(parts).decode("utf-8", "replace")

    def _pong(self, data: bytes) -> None:
        mask = os.urandom(4)
        head = bytearray([0x80 | OP_PONG, 0x80 | len(data)]) + mask
        self.sock.sendall(bytes(head) + bytes(b ^ mask[i % 4] for i, b in enumerate(data)))

    def close(self) -> None:
        try:
            mask = os.urandom(4)
            self.sock.sendall(bytes([0x80 | OP_CLOSE, 0x80]) + mask)
        except OSError:
            pass
        finally:
            try:
                self.sock.close()
            except OSError:
                pass

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()
