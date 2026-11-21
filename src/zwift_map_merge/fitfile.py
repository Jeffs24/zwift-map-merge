"""Minimal, lossless FIT reader/writer.

Only what the merge needs: walk every record of a FIT file, keep the raw
bytes of everything we do not touch (proprietary messages, developer fields,
compressed timestamp headers), and re-encode the file with a fresh header
and CRC. Unlike a full SDK it never drops a message it does not know.
"""
from __future__ import annotations

import struct
from dataclasses import dataclass, field
from typing import Iterator

FIT_EPOCH_UNIX = 631065600  # 1989-12-31T00:00:00Z

# Global message numbers
MESG_FILE_ID = 0
MESG_SESSION = 18
MESG_LAP = 19
MESG_RECORD = 20

FIELD_TIMESTAMP = 253

# Base types: number -> (size, struct format char, invalid value)
BASE_TYPES = {
    0x00: (1, "B", 0xFF),        # enum
    0x01: (1, "b", 0x7F),        # sint8
    0x02: (1, "B", 0xFF),        # uint8
    0x83: (2, "h", 0x7FFF),      # sint16
    0x84: (2, "H", 0xFFFF),      # uint16
    0x85: (4, "i", 0x7FFFFFFF),  # sint32
    0x86: (4, "I", 0xFFFFFFFF),  # uint32
    0x0A: (1, "B", 0x00),        # uint8z
    0x8B: (2, "H", 0x0000),      # uint16z
    0x8C: (4, "I", 0x00000000),  # uint32z
}
SINT32 = 0x85
UINT32 = 0x86
UINT16 = 0x84

_CRC_TABLE = [
    0x0000, 0xCC01, 0xD801, 0x1400, 0xF001, 0x3C00, 0x2800, 0xE401,
    0xA001, 0x6C00, 0x7800, 0xB401, 0x5000, 0x9C01, 0x8801, 0x4400,
]


class FitError(ValueError):
    """The bytes are not a FIT file we can process."""


def crc16(data: bytes, crc: int = 0) -> int:
    for byte in data:
        tmp = _CRC_TABLE[crc & 0xF]
        crc = (crc >> 4) & 0x0FFF
        crc = crc ^ tmp ^ _CRC_TABLE[byte & 0xF]
        tmp = _CRC_TABLE[crc & 0xF]
        crc = (crc >> 4) & 0x0FFF
        crc = crc ^ tmp ^ _CRC_TABLE[(byte >> 4) & 0xF]
    return crc


@dataclass
class FieldDef:
    num: int
    size: int
    base_type: int


@dataclass
class Definition:
    local: int
    big_endian: bool
    global_num: int
    fields: list[FieldDef]
    dev_fields: list[tuple[int, int, int]] = field(default_factory=list)

    @property
    def endian(self) -> str:
        return ">" if self.big_endian else "<"

    @property
    def data_size(self) -> int:
        return sum(f.size for f in self.fields) + sum(d[1] for d in self.dev_fields)

    def field_offsets(self) -> dict[int, tuple[int, FieldDef]]:
        offsets: dict[int, tuple[int, FieldDef]] = {}
        pos = 0
        for f in self.fields:
            offsets.setdefault(f.num, (pos, f))
            pos += f.size
        return offsets

    def encode(self) -> bytes:
        header = 0x40 | (0x20 if self.dev_fields else 0) | (self.local & 0x0F)
        out = bytearray([header, 0, 1 if self.big_endian else 0])
        out += struct.pack(self.endian + "H", self.global_num)
        out.append(len(self.fields))
        for f in self.fields:
            out += bytes([f.num, f.size, f.base_type])
        if self.dev_fields:
            out.append(len(self.dev_fields))
            for d in self.dev_fields:
                out += bytes(d)
        return bytes(out)


@dataclass
class Message:
    """One data message. ``raw`` holds the field bytes (no record header)."""

    header: int
    definition: Definition
    raw: bytes
    timestamp: int | None  # FIT seconds, explicit or from a compressed header

    def get(self, field_num: int) -> int | None:
        """Decode a scalar field; None when absent or invalid."""
        offsets = self.definition.field_offsets()
        if field_num not in offsets:
            return None
        pos, f = offsets[field_num]
        bt = BASE_TYPES.get(f.base_type)
        if bt is None or bt[0] != f.size:
            return None
        (value,) = struct.unpack_from(self.definition.endian + bt[1], self.raw, pos)
        if value == bt[2]:
            return None
        return value


@dataclass
class FitFile:
    protocol_version: int
    profile_version: int
    # Items are Definition or Message, in file order.
    items: list[Definition | Message]
    trailing: bytes = b""  # chained FIT files, kept verbatim

    def messages(self, global_num: int | None = None) -> Iterator[Message]:
        for item in self.items:
            if isinstance(item, Message) and (
                global_num is None or item.definition.global_num == global_num
            ):
                yield item


def parse(data: bytes) -> FitFile:
    if len(data) < 12:
        raise FitError("file too short")
    header_size = data[0]
    if header_size not in (12, 14) or data[8:12] != b".FIT":
        raise FitError("invalid FIT header")
    protocol_version = data[1]
    profile_version, data_size = struct.unpack_from("<HI", data, 2)
    end = header_size + data_size
    if len(data) < end + 2:
        raise FitError("truncated FIT file")
    (stored_crc,) = struct.unpack_from("<H", data, end)
    if stored_crc != 0 and crc16(data[:end]) != stored_crc:
        raise FitError("invalid FIT CRC")

    defs: dict[int, Definition] = {}
    items: list[Definition | Message] = []
    pos = header_size
    last_ts: int | None = None
    while pos < end:
        rh = data[pos]
        pos += 1
        if rh & 0x80:  # compressed timestamp header -> data message
            local = (rh >> 5) & 0x03
            offset = rh & 0x1F
            if last_ts is None:
                raise FitError("compressed timestamp without a reference timestamp")
            ts = last_ts + ((offset - (last_ts & 0x1F)) & 0x1F)
            last_ts = ts
            d = _require_def(defs, local)
            raw = data[pos:pos + d.data_size]
            pos += d.data_size
            items.append(Message(rh, d, raw, ts))
        elif rh & 0x40:  # definition
            local = rh & 0x0F
            big_endian = data[pos + 1] == 1
            (global_num,) = struct.unpack_from(">H" if big_endian else "<H", data, pos + 2)
            nfields = data[pos + 4]
            pos += 5
            fields = []
            for _ in range(nfields):
                fields.append(FieldDef(data[pos], data[pos + 1], data[pos + 2]))
                pos += 3
            dev_fields = []
            if rh & 0x20:
                ndev = data[pos]
                pos += 1
                for _ in range(ndev):
                    dev_fields.append((data[pos], data[pos + 1], data[pos + 2]))
                    pos += 3
            d = Definition(local, big_endian, global_num, fields, dev_fields)
            defs[local] = d
            items.append(d)
        else:
            local = rh & 0x0F
            d = _require_def(defs, local)
            raw = data[pos:pos + d.data_size]
            pos += d.data_size
            msg = Message(rh, d, raw, None)
            ts = msg.get(FIELD_TIMESTAMP)
            if ts is not None:
                last_ts = ts
            msg.timestamp = ts
            items.append(msg)
    if pos != end:
        raise FitError("inconsistent end of FIT data")
    return FitFile(protocol_version, profile_version, items, data[end + 2:])


def _require_def(defs: dict[int, Definition], local: int) -> Definition:
    if local not in defs:
        raise FitError(f"message without a definition (local type {local})")
    return defs[local]


def encode(fit: FitFile) -> bytes:
    body = bytearray()
    for item in fit.items:
        if isinstance(item, Definition):
            body += item.encode()
        else:
            if len(item.raw) != item.definition.data_size:
                raise FitError("message size does not match its definition")
            body.append(item.header)
            body += item.raw
    header = bytearray(struct.pack("<BBHI4s", 14, fit.protocol_version,
                                   fit.profile_version, len(body), b".FIT"))
    header += struct.pack("<H", crc16(bytes(header)))
    out = bytes(header) + bytes(body)
    return out + struct.pack("<H", crc16(out)) + fit.trailing


def unix_from_fit(ts: int) -> int:
    return ts + FIT_EPOCH_UNIX


def fit_from_unix(ts: int) -> int:
    return ts - FIT_EPOCH_UNIX
