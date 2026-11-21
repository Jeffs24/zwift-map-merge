"""Build small synthetic FIT files for tests."""
from __future__ import annotations

import struct

from zwift_map_merge import fitfile as ff
from zwift_map_merge.fitfile import Definition, FieldDef, FitFile, Message


def msg(defn: Definition, values: dict[int, int], header: int | None = None,
        dev: bytes = b"", timestamp: int | None = None) -> Message:
    raw = bytearray()
    for f in defn.fields:
        bt = ff.BASE_TYPES[f.base_type]
        raw += struct.pack(defn.endian + bt[1], values.get(f.num, bt[2]))
    raw += dev
    m = Message(header if header is not None else defn.local, defn, bytes(raw), None)
    m.timestamp = timestamp if timestamp is not None else m.get(ff.FIELD_TIMESTAMP)
    return m


def file_id(manufacturer: int, big_endian: bool = False) -> list:
    d = Definition(0, big_endian, ff.MESG_FILE_ID,
                   [FieldDef(0, 1, 0x00), FieldDef(1, 2, 0x84), FieldDef(4, 4, 0x86)])
    return [d, msg(d, {0: 4, 1: manufacturer, 4: 1_000_000_000})]


def watch_fit(start: int, n: int, *, big_endian: bool = False, compressed_every: int = 0) -> bytes:
    """Watch recording: timestamp, heart rate, cadence, one developer field, no GPS."""
    items = file_id(1, big_endian)
    rec = Definition(1, big_endian, ff.MESG_RECORD,
                     [FieldDef(253, 4, 0x86), FieldDef(3, 1, 0x02), FieldDef(4, 1, 0x02)],
                     dev_fields=[(0, 2, 0)])
    items.append(rec)
    for i in range(n):
        ts = start + i
        if compressed_every and i % compressed_every == 1:
            # compressed timestamp header, local type 1, 5-bit offset
            header = 0x80 | (1 << 5) | (ts & 0x1F)
            m = msg(rec, {253: 0xFFFFFFFF, 3: 140 + i % 10, 4: 85}, header=header,
                    dev=struct.pack("<H", 1000 + i), timestamp=ts)
            items.append(m)
        else:
            items.append(msg(rec, {253: ts, 3: 140 + i % 10, 4: 85}, dev=struct.pack("<H", 1000 + i)))
    lap = Definition(2, big_endian, ff.MESG_LAP,
                     [FieldDef(253, 4, 0x86), FieldDef(2, 4, 0x86), FieldDef(3, 4, 0x85),
                      FieldDef(4, 4, 0x85), FieldDef(5, 4, 0x85), FieldDef(6, 4, 0x85)])
    items += [lap, msg(lap, {253: start + n - 1, 2: start})]
    return ff.encode(FitFile(16, 2132, items))


def zwift_fit(start: int, n: int, lat0: int = -127_000_000, lon0: int = 1_990_000_000) -> bytes:
    items = file_id(260)
    rec = Definition(0, False, ff.MESG_RECORD,
                     [FieldDef(253, 4, 0x86), FieldDef(0, 4, 0x85), FieldDef(1, 4, 0x85),
                      FieldDef(78, 4, 0x86)])
    items.append(rec)
    for i in range(n):
        items.append(msg(rec, {253: start + i, 0: lat0 + i * 100, 1: lon0 + i * 100,
                               78: (20 + i + 500) * 5}))
    return ff.encode(FitFile(16, 2132, items))
