#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""ws_server.py - minimal WebSocket-server (RFC 6455) på bara standardbiblioteket.

Ett enda ansvar: ta emot WebSocket-anslutningar och leverera hela text-/binärmeddelanden.
Ingen TCI-kunskap här. Skriven själv i stället för att lägga till ett serverberoende
(websocket-client, som programmet redan använder, är bara en klient).

Stöd: handskakning (Sec-WebSocket-Accept), maskade klientramar, fragmenterade
meddelanden, ping/pong, close. Servern skickar aldrig maskade ramar (krav i RFC 6455).
"""

import base64
import hashlib
import socket
import struct
import threading

_GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"
OP_CONT, OP_TEXT, OP_BIN, OP_CLOSE, OP_PING, OP_PONG = 0x0, 0x1, 0x2, 0x8, 0x9, 0xA
MAX_MESSAGE = 1 << 20      # 1 MiB, ett TCI-ljudblock är ~16 KiB


class WsConnection:
    def __init__(self, sock):
        self._sock = sock
        self._send_lock = threading.Lock()
        self.closed = False

    def _read_exact(self, n):
        buf = b""
        while len(buf) < n:
            chunk = self._sock.recv(n - len(buf))
            if not chunk:
                raise ConnectionError("closed")
            buf += chunk
        return buf

    def _read_frame(self):
        b0, b1 = self._read_exact(2)
        fin = bool(b0 & 0x80)
        opcode = b0 & 0x0F
        masked = bool(b1 & 0x80)
        length = b1 & 0x7F
        if length == 126:
            (length,) = struct.unpack(">H", self._read_exact(2))
        elif length == 127:
            (length,) = struct.unpack(">Q", self._read_exact(8))
        if length > MAX_MESSAGE:
            raise ConnectionError("frame too large")
        mask = self._read_exact(4) if masked else None
        payload = self._read_exact(length) if length else b""
        if mask:
            payload = bytes(b ^ mask[i & 3] for i, b in enumerate(payload))
        return fin, opcode, payload

    def recv(self):
        """Nästa hela meddelande: str (text) eller bytes (binärt). ConnectionError vid stängning."""
        parts, first_op = [], None
        while True:
            fin, op, payload = self._read_frame()
            if op == OP_PING:
                self._send_frame(OP_PONG, payload)
                continue
            if op == OP_PONG:
                continue
            if op == OP_CLOSE:
                try:
                    self._send_frame(OP_CLOSE, payload[:2])
                except OSError:
                    pass
                self.closed = True
                raise ConnectionError("close frame")
            if op in (OP_TEXT, OP_BIN):
                first_op = op
                parts = [payload]
            elif op == OP_CONT and first_op is not None:
                parts.append(payload)
            if fin and first_op is not None:
                data = b"".join(parts)
                return data.decode("utf-8", "replace") if first_op == OP_TEXT else data

    def _send_frame(self, opcode, payload):
        n = len(payload)
        head = bytes([0x80 | opcode])
        if n < 126:
            head += bytes([n])
        elif n < 65536:
            head += bytes([126]) + struct.pack(">H", n)
        else:
            head += bytes([127]) + struct.pack(">Q", n)
        with self._send_lock:
            self._sock.sendall(head + payload)

    def send_text(self, text):
        self._send_frame(OP_TEXT, text.encode("utf-8"))

    def send_binary(self, data):
        self._send_frame(OP_BIN, bytes(data))

    def close(self):
        self.closed = True
        try:
            self._sock.close()
        except OSError:
            pass


def _handshake(sock):
    """Läser HTTP-upgrade-begäran och svarar. Returnerar True om det blev en WebSocket."""
    data = b""
    while b"\r\n\r\n" not in data:
        chunk = sock.recv(4096)
        if not chunk:
            return False
        data += chunk
        if len(data) > 16384:
            return False
    headers = {}
    for line in data.decode("latin-1").split("\r\n")[1:]:
        k, _, v = line.partition(":")
        headers[k.strip().lower()] = v.strip()
    key = headers.get("sec-websocket-key")
    if not key or headers.get("upgrade", "").lower() != "websocket":
        return False
    accept = base64.b64encode(hashlib.sha1((key + _GUID).encode()).digest()).decode()
    sock.sendall(("HTTP/1.1 101 Switching Protocols\r\nUpgrade: websocket\r\n"
                  "Connection: Upgrade\r\nSec-WebSocket-Accept: %s\r\n\r\n" % accept).encode())
    return True


class WsServer:
    """Lyssnar på localhost, både IPv4 (127.0.0.1) och IPv6 (::1) på samma port, eftersom
    klienter som ansluter till namnet "localhost" kan välja endera. `on_client(conn)` körs i
    en egen tråd per anslutning och bestämmer själv hur länge anslutningen lever
    (returnerar den, stängs anslutningen). `log(text)` får händelser, även misslyckade."""

    def __init__(self, host, port, on_client, log=None):
        self.host, self.port = host, port
        self._on_client = on_client
        self._log = log or (lambda _t: None)
        self._srv = None            # IPv4-socketen (port 0 = ledig port väljs här)
        self._socks = []
        self._stopping = False

    def start(self):
        srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        srv.bind((self.host, self.port))
        srv.listen(4)
        self._srv = srv
        self._socks = [srv]
        port = srv.getsockname()[1]
        try:
            srv6 = socket.socket(socket.AF_INET6, socket.SOCK_STREAM)
            srv6.setsockopt(socket.IPPROTO_IPV6, socket.IPV6_V6ONLY, 1)
            srv6.bind(("::1", port))
            srv6.listen(4)
            self._socks.append(srv6)
        except OSError as e:
            self._log("WS server: IPv6 (::1) not available: %s" % e)
        self._stopping = False
        for sock in self._socks:
            threading.Thread(target=self._accept_loop, args=(sock,), daemon=True).start()

    def stop(self):
        self._stopping = True
        for sock in self._socks:
            try:
                sock.close()
            except OSError:
                pass
        self._socks = []
        self._srv = None

    def _accept_loop(self, srv):
        while not self._stopping:
            try:
                sock, addr = srv.accept()
            except OSError:
                break
            threading.Thread(target=self._serve, args=(sock, addr), daemon=True).start()

    def _serve(self, sock, addr):
        try:
            sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
            sock.settimeout(5)
            if not _handshake(sock):
                self._log("WS server: handshake refused from %s" % (addr[0],))
                sock.close()
                return
            sock.settimeout(None)
        except OSError as e:
            self._log("WS server: handshake failed from %s: %s" % (addr[0], e))
            sock.close()
            return
        self._log("WS server: connection from %s" % (addr[0],))
        conn = WsConnection(sock)
        try:
            self._on_client(conn)
        finally:
            conn.close()
