"""Linux TCP peer ownership for the loopback application boundary."""
from __future__ import annotations

import os
import socket
import struct


# Linux UAPI: netlink.h, sock_diag.h and inet_diag.h.
NETLINK_SOCK_DIAG = 4
SOCK_DIAG_BY_FAMILY = 20
NLM_F_REQUEST = 1
TCP_ESTABLISHED = 1
HEADER = struct.Struct("=IHHII")
REQUEST = struct.Struct("=BBBBI")
IDENTITY_TRAILER = struct.Struct("=III")
STATS = struct.Struct("=IIIII")


def socket_tuple(client: tuple[str, int], server: tuple[str, int]) -> bytes:
    """Encode the client side of the accepted IPv4 TCP connection."""
    return (
        struct.pack("!HH", client[1], server[1])
        + socket.inet_pton(socket.AF_INET, client[0]).ljust(16, b"\0")
        + socket.inet_pton(socket.AF_INET, server[0]).ljust(16, b"\0")
    )


def response_uid(packet: bytes, address: tuple[int, int], identity: bytes) -> int | None:
    if address != (0, 0) or len(packet) < HEADER.size:
        return None
    length, kind, _, sequence, _ = HEADER.unpack_from(packet)
    stats_offset = HEADER.size + 4 + len(identity) + IDENTITY_TRAILER.size
    if (kind != SOCK_DIAG_BY_FAMILY or sequence != 1
            or not stats_offset + STATS.size <= length <= len(packet)):
        return None
    if packet[HEADER.size:HEADER.size + 2] != bytes((socket.AF_INET, TCP_ESTABLISHED)):
        return None
    if packet[HEADER.size + 4:HEADER.size + 4 + len(identity)] != identity:
        return None
    _, _, _, uid, inode = STATS.unpack_from(packet, stats_offset)
    return uid if inode else None


def same_user_tcp_peer(connection: socket.socket) -> bool:
    try:
        if connection.family != socket.AF_INET:
            return False
        identity = socket_tuple(connection.getpeername(), connection.getsockname())
        body = (
            REQUEST.pack(socket.AF_INET, socket.IPPROTO_TCP, 0, 0, 1 << TCP_ESTABLISHED)
            + identity + IDENTITY_TRAILER.pack(0, 0xFFFFFFFF, 0xFFFFFFFF)
        )
        query = HEADER.pack(HEADER.size + len(body), SOCK_DIAG_BY_FAMILY, NLM_F_REQUEST, 1, 0) + body
        with socket.socket(socket.AF_NETLINK, socket.SOCK_DGRAM, NETLINK_SOCK_DIAG) as channel:
            channel.settimeout(0.25)
            channel.bind((0, 0))
            channel.sendto(query, (0, 0))
            packet, address = channel.recvfrom(4096)
        return response_uid(packet, address, identity) == os.getuid()
    except (OSError, ValueError, TypeError, struct.error):
        return False
