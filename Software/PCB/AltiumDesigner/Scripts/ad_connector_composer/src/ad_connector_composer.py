#!/usr/bin/env python3
"""
ad_connector_composer — linear connector footprint generator for Altium .PcbLib.

Example:
  ad_connector_composer.exe --pitch 2.54 --max 10
  python ad_connector_composer.py -p 2.0 -c 8 --prefix HDR-2
"""

from __future__ import annotations

import argparse
import random
import re
import string
import struct
import sys
from datetime import datetime
from pathlib import Path

from compoundfiles import CompoundFileWriter
import olefile

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

FRAGMENT_ROLES = ("BASE", "BEGIN", "PIN", "END")
DEFAULT_PITCH_MM = 2.54
DEFAULT_NAME_PREFIX = "PLS-2.54"
APP_NAME = "ad_connector_composer"

REQUIREMENTS_TEXT = (
    "Требования к библиотеке (.PcbLib):\n"
    "• Внутри должны быть 4 фрагмента с окончаниями BASE, BEGIN, PIN, END\n"
    "• Префикс задаёт набор фрагментов и имена результатов\n"
    "  (напр. PLS-2.54 → PLS-2.54-BASE … END и PLS-2.54-7P)\n"
    "• Шаг (pitch) — только расстояние между колонками при копировании pads, mm\n"
    "• BEGIN / PIN / END — контакты begin-1, begin-2, … / pin-1, … / end-1, …\n"
    "  (число падов = числу рядов R; при R=1 достаточно -1) + 3D на каждый\n"
    "• BASE — шелк/ключ без pads; 3D опционален\n"
    "• Имя footprint: {prefix}-{C×R}P (напр. 4 кол. × 2 ряда → …-8P)\n"
    "• Нумерация V (по умолчанию): вниз по колонке; H — вдоль ряда\n"
    "Мастер не перезаписывается — каждый запуск создаёт новый .PcbLib."
)

# ComponentBody layout (PIN/BEGIN/END) — payload inside length-prefixed block:
#   [0:18]  fixed header
#   [18:22] u32 param length (incl. trailing NUL)
#   [22:22+plen] parameter string
#   rest    contour doubles
_BODY_HDR = 18
_BODY_PLEN_OFF = 18

# Altium internal units: 1 mil = 10000; 1 mm = 10000/0.0254
def mm_to_internal(mm: float) -> int:
    return int(round(mm * 10000.0 / 0.0254))


def mils_to_internal(mils: float) -> int:
    return int(round(mils * 10000.0))


def internal_to_mils(v: int) -> float:
    return v / 10000.0


def normalize_prefix(name_prefix: str) -> str:
    """User prefix / fragment stem, e.g. 'PLS-2.54' (no trailing '-')."""
    return name_prefix.strip().strip("-") or DEFAULT_NAME_PREFIX


def footprint_name_base(name_prefix: str) -> str:
    """Prefix for generated footprint names, always ending with '-'."""
    return normalize_prefix(name_prefix) + "-"


def expected_fragment_stem(name_prefix: str) -> str:
    """Stem of source fragments: PLS-2.54 → PLS-2.54-BASE / BEGIN / PIN / END."""
    return normalize_prefix(name_prefix)


def target_name(cols: int, rows: int, name_prefix: str) -> str:
    """Name by total pad count with P suffix, e.g. PLS-2.54-14P for 7×2."""
    base = footprint_name_base(name_prefix)
    return f"{base}{cols * rows}P"

def read_u32(buf: bytes, pos: int) -> tuple[int, int]:
    return int.from_bytes(buf[pos : pos + 4], "little"), pos + 4


def write_u32(v: int) -> bytes:
    return int(v & 0xFFFFFFFF).to_bytes(4, "little", signed=False)


def write_i32(v: int) -> bytes:
    return int(v).to_bytes(4, "little", signed=True)


def take_block(buf: bytes, pos: int) -> tuple[bytes, int]:
    ln, pos = read_u32(buf, pos)
    return buf[pos : pos + ln], pos + ln


def put_block(data: bytes) -> bytes:
    return write_u32(len(data)) + data


def put_pcb_string(s: str) -> bytes:
    """uint32 block_len + uint8 strlen + bytes (windows-1252)."""
    encoded = s.encode("windows-1252", errors="replace")
    inner = bytes([len(encoded)]) + encoded
    return put_block(inner)


def put_param_block(text: str) -> bytes:
    """Length-prefixed parameter collection ending with NUL."""
    payload = text.encode("windows-1252", errors="replace")
    if not payload.endswith(b"\x00"):
        payload += b"\x00"
    return write_u32(len(payload)) + payload


def shift_i32_at(buf: bytearray, offset: int, dx: int) -> None:
    if offset + 4 > len(buf):
        return
    old = int.from_bytes(buf[offset : offset + 4], "little", signed=True)
    buf[offset : offset + 4] = write_i32(old + dx)


def shift_f64_at(buf: bytearray, offset: int, dx_mils: float) -> None:
    """Region vertices are doubles in mils (scaled)."""
    if offset + 8 > len(buf):
        return
    old = struct.unpack_from("<d", buf, offset)[0]
    struct.pack_into("<d", buf, offset, old + dx_mils)


def random_unique_id() -> str:
    alphabet = string.ascii_uppercase
    return "".join(random.choice(alphabet) for _ in range(8))


# ---------------------------------------------------------------------------
# Numbering
# ---------------------------------------------------------------------------

def pin_number(col: int, row: int, cols: int, rows: int, mode: str) -> int:
    """
    Pad number from fragment pad begin/pin/end-{row} at column col.

    V (column-major, default): down each column, then next column.
      4 cols × 2 rows: begin-1=1, begin-2=2, pin-1=3, pin-2=4, … end-1=7, end-2=8
    H (row-major): across each row, then next row.
      4 cols × 2 rows: begin-1=1, pin-1=2, … end-1=4, begin-2=5, … end-2=8
    """
    if mode == "H":
        return (row - 1) * cols + col
    return (col - 1) * rows + row


def parse_pad_name(name: str) -> tuple[str, int] | None:
    m = re.fullmatch(r"(begin|end|pin)-(\d+)", name.strip(), re.I)
    if not m:
        return None
    zone = m.group(1).lower()
    return zone, int(m.group(2))


# ---------------------------------------------------------------------------
# Binary helpers
# ---------------------------------------------------------------------------

def rewrite_pad_designator(block0: bytes, new_name: str) -> bytes:
    """block0 is inner content: uint8 strlen + string (+ maybe extra)."""
    if not block0:
        return bytes([len(new_name)]) + new_name.encode("windows-1252")
    # keep any trailing bytes after the string
    strlen = block0[0]
    rest = block0[1 + strlen :]
    enc = new_name.encode("windows-1252", errors="replace")
    return bytes([len(enc)]) + enc + rest


def _format_mil_token(val: float, width: int) -> bytes:
    """Fixed-width mil number without spaces (Altium rejects spaced values)."""
    token = None
    for prec in range(4, -1, -1):
        s = f"{val:.{prec}f}"
        if len(s) <= width:
            token = s
            break
    if token is None:
        token = f"{val:.0f}"
    if len(token) < width:
        if token.startswith("-"):
            token = "-" + token[1:].zfill(width - 1)
        else:
            token = token.zfill(width)
    return token[:width].encode("ascii")


def _patch_mil_property(params: bytearray, key: bytes, dx_mils: float) -> None:
    """In-place update of KEY=<number>mil keeping <number> width."""
    i = params.find(key)
    if i < 0:
        return
    num_start = i + len(key)
    num_end = params.find(b"mil", num_start)
    if num_end <= num_start:
        return
    old = params[num_start:num_end].decode("ascii", errors="replace").replace(" ", "")
    try:
        val = float(old.replace(",", ".")) + dx_mils
    except ValueError:
        return
    params[num_start:num_end] = _format_mil_token(val, num_end - num_start)


def shift_body_block(block: bytes, dx: int) -> bytes:
    """
    Shift Mechanical 7 ComponentBody with the pin column.

    - MODEL.2D.X / TEXTURECENTERX: in-place fixed-width patch (mils)
    - contour vertex X doubles: add dx in Altium internal units
    Never rewrite param length / paths — that triggers GPC_MALLOC.
    """
    if dx == 0 or len(block) < _BODY_PLEN_OFF + 4:
        return block

    dx_mils = internal_to_mils(dx)
    param_len = int.from_bytes(
        block[_BODY_PLEN_OFF : _BODY_PLEN_OFF + 4], "little"
    )
    param_start = _BODY_PLEN_OFF + 4
    param_end = param_start + param_len
    if param_end > len(block) or param_len <= 0:
        return block

    out = bytearray(block)
    params = bytearray(block[param_start:param_end])
    _patch_mil_property(params, b"MODEL.2D.X=", dx_mils)
    _patch_mil_property(params, b"TEXTURECENTERX=", dx_mils)
    out[param_start:param_end] = params
    if len(out) != len(block):
        return block

    # Contour: u32 vertex_count + N * (x:f64, y:f64) in internal units
    tail_off = param_end
    if tail_off + 4 <= len(out):
        nverts = int.from_bytes(out[tail_off : tail_off + 4], "little")
        p = tail_off + 4
        if 1 <= nverts <= 64 and p + nverts * 16 <= len(out):
            dx_f = float(dx)
            for _ in range(nverts):
                old = struct.unpack_from("<d", out, p)[0]
                struct.pack_into("<d", out, p, old + dx_f)
                p += 16  # skip y

    return bytes(out)


def transform_record(
    rid: int,
    payload_start: int,
    data: bytes,
    dx: int,
    expected_zone: str | None,
    col: int,
    cols: int,
    rows: int,
    mode: str,
    stats: dict,
) -> tuple[bytes, int]:
    """
    Consume one record starting at payload_start (after rid byte already read).
    Returns (record_bytes_including_rid, new_pos).
    """
    pos = payload_start
    dx_mils = internal_to_mils(dx)

    def one_block() -> bytes:
        nonlocal pos
        b, pos = take_block(data, pos)
        return b

    out = bytearray([rid])

    if rid == 2:  # Pad
        blocks = [one_block() for _ in range(6)]
        des = ""
        if blocks[0]:
            ln = blocks[0][0]
            des = blocks[0][1 : 1 + ln].decode("windows-1252", errors="replace")
        parsed = parse_pad_name(des)
        if expected_zone and parsed and parsed[0] == expected_zone:
            zone, row = parsed
            if 1 <= row <= rows:
                new_des = str(pin_number(col, row, cols, rows, mode))
                blocks[0] = rewrite_pad_designator(blocks[0], new_des)
                stats["pads_assigned"] += 1
            else:
                stats["pads_skipped"] += 1
        elif expected_zone:
            stats["pads_skipped"] += 1

        # shift location in block4 after common(13)
        if blocks[4] and len(blocks[4]) >= 17:
            b4 = bytearray(blocks[4])
            shift_i32_at(b4, 13, dx)
            blocks[4] = bytes(b4)

        for b in blocks:
            out += put_block(b)

    elif rid in (1, 3, 4, 6):  # Arc, Via, Track, Fill
        block = bytearray(one_block())
        if rid == 4 and len(block) >= 29:
            # Track: common13 + x1 y1 x2 y2
            shift_i32_at(block, 13, dx)
            shift_i32_at(block, 21, dx)
        elif rid in (1, 3) and len(block) >= 17:
            # Arc / Via: common13 + x y
            shift_i32_at(block, 13, dx)
        elif rid == 6 and len(block) >= 25:
            # Fill: common13 + c1x c1y c2x c2y
            shift_i32_at(block, 13, dx)
            shift_i32_at(block, 21, dx)
        out += put_block(bytes(block))

    elif rid == 5:  # String
        geom = bytearray(one_block())
        text = one_block()
        if len(geom) >= 17:
            shift_i32_at(geom, 13, dx)
        out += put_block(bytes(geom)) + put_block(text)

    elif rid == 11:  # Region
        block = bytearray(one_block())
        # after common13 + unk4 + unk1 + param string(len u32? or u8?)
        # pyaltium: read_common(13); read_int32; read_byte; ParameterCollection(read_string_block size_string=4)
        # then num_vertices int32; vertices as double coords
        try:
            p = 13 + 4 + 1
            slen = int.from_bytes(block[p : p + 4], "little")
            p += 4 + slen
            nverts = int.from_bytes(block[p : p + 4], "little")
            p += 4
            for _ in range(nverts):
                shift_f64_at(block, p, dx_mils)
                p += 16  # x double + y double
        except Exception:
            pass
        out += put_block(bytes(block))

    elif rid == 12:  # ComponentBody — one payload block only (no empty 2nd block)
        one_block()
        return b"", pos

    else:
        raise ValueError(f"Unsupported RecordID={rid} — cannot compose safely")

    return bytes(out), pos


def extract_records(data: bytes) -> tuple[str, list[tuple[int, int, int]]]:
    """Return footprint name and (rid, payload_start, payload_end) spans."""
    pos = 0
    name_block, pos = take_block(data, pos)
    nlen = name_block[0]
    name = name_block[1 : 1 + nlen].decode("windows-1252", errors="replace")
    spans: list[tuple[int, int, int]] = []
    while pos < len(data):
        rid = data[pos]
        pos += 1
        if rid == 0:
            break
        payload_start = pos
        if rid == 2:
            for _ in range(6):
                _, pos = take_block(data, pos)
        elif rid in (1, 3, 4, 6, 11):
            _, pos = take_block(data, pos)
        elif rid == 5:
            _, pos = take_block(data, pos)
            _, pos = take_block(data, pos)
        elif rid == 12:
            # ComponentBody: single length-prefixed payload (Altium has NO empty 2nd block).
            # Writing put_block(b"") emitted 00000000 → Altium treats 0x00 as END and
            # drops all subsequent 3D bodies (only first pin showed).
            _, pos = take_block(data, pos)
        else:
            raise ValueError(f"Unsupported RecordID={rid} in '{name}'")
        spans.append((rid, payload_start, pos))
    return name, spans


def extract_first_body(data: bytes) -> bytes | None:
    """Return body payload of first ComponentBody in footprint Data."""
    pos = 0
    _, pos = take_block(data, pos)  # name
    while pos < len(data):
        rid = data[pos]
        pos += 1
        if rid == 0:
            break
        if rid == 2:
            for _ in range(6):
                _, pos = take_block(data, pos)
        elif rid in (1, 3, 4, 6, 11):
            _, pos = take_block(data, pos)
        elif rid == 5:
            _, pos = take_block(data, pos)
            _, pos = take_block(data, pos)
        elif rid == 12:
            b1, pos = take_block(data, pos)
            return b1
        else:
            break
    return None


def place_body(template: bytes, dx: int) -> bytes:
    """Verbatim ComponentBody + fixed-width MODEL.2D.X / contour shift."""
    return shift_body_block(template, dx)


def body_record_bytes(body_block: bytes) -> bytes:
    """One ComponentBody record = rid 12 + single payload (matches Altium Save)."""
    return bytes([12]) + put_block(body_block)


def body_model_name(body: bytes) -> str:
    m = re.search(br"MODEL\.NAME=([^|]+)", body)
    return m.group(1).decode("latin-1", errors="replace") if m else "?"


def body_model_id(body: bytes) -> str:
    m = re.search(br"MODELID=(\{[^}]+\})", body)
    return m.group(1).decode("ascii", errors="replace") if m else "?"



def transform_footprint_data(
    data: bytes,
    dx: int,
    expected_zone: str | None,
    col: int,
    cols: int,
    rows: int,
    mode: str,
    stats: dict,
    rename_pattern: str | None = None,
) -> tuple[bytes, int]:
    """
    Returns (records_only_bytes without name/terminator, primitive_count).
    """
    _, spans = extract_records(data)
    out = bytearray()
    count = 0
    for rid, payload_start, payload_end in spans:
        # re-transform from original using transform_record which re-reads
        # Use a helper that works on the payload slice — call transform on full data
        rec, _ = transform_record(
            rid, payload_start, data, dx, expected_zone, col, cols, rows, mode, stats
        )
        if not rec:
            continue
        out += rec
        count += 1
    return bytes(out), count


def build_data_stream(pattern_name: str, records_blob: bytes) -> bytes:
    return put_pcb_string(pattern_name) + records_blob + b"\x00"


def build_parameters(pattern_name: str, height: str = "336.2205mil") -> bytes:
    text = (
        f"|PATTERN={pattern_name}|HEIGHT={height}|DESCRIPTION=|"
        f"ITEMGUID=|REVISIONGUID="
    )
    return put_param_block(text)


def build_unique_ids(data_stream: bytes) -> tuple[bytes, bytes]:
    """Rebuild UniqueIDPrimitiveInformation for all pads in Data."""
    # skip name
    pos = 0
    _, pos = take_block(data_stream, pos)
    entries = []
    index = 0
    while pos < len(data_stream):
        rid = data_stream[pos]
        pos += 1
        if rid == 0:
            break
        payload_start = pos
        if rid == 2:
            for _ in range(6):
                _, pos = take_block(data_stream, pos)
            uid = random_unique_id()
            entry = f"|PRIMITIVEINDEX={index}|PRIMITIVEOBJECTID=Pad|UNIQUEID={uid}"
            entries.append(entry)
        elif rid in (1, 3, 4, 6, 11):
            _, pos = take_block(data_stream, pos)
        elif rid == 5:
            _, pos = take_block(data_stream, pos)
            _, pos = take_block(data_stream, pos)
        elif rid == 12:
            _, pos = take_block(data_stream, pos)
        else:
            break
        index += 1

    if not entries:
        header = write_u32(0)
        data = write_u32(1) + b"\x00"
        return header, data

    # Multiple entries: each as param block? Source has single entry as one sized block.
    # Look at multi-pad UID format — for one pad it's one block.
    # For multiple, concatenate as separate length-prefixed parameter blocks.
    blob = bytearray()
    for e in entries:
        payload = e.encode("ascii") + b"\x00"
        blob += write_u32(len(payload)) + payload
    header = write_u32(len(entries))
    return header, bytes(blob)


def parse_height_from_params(params: bytes) -> str:
    m = re.search(br"HEIGHT=([^|]*)", params)
    if m:
        return m.group(1).decode("windows-1252", errors="replace")
    return "336.2205mil"


# ---------------------------------------------------------------------------
# Library TOC update
# ---------------------------------------------------------------------------

def update_library_data(old: bytes, new_name: str) -> bytes:
    pos = 0
    plen, pos = read_u32(old, pos)
    params = old[pos : pos + plen]
    pos += plen
    count, pos = read_u32(old, pos)
    names = []
    for _ in range(count):
        block, pos = take_block(old, pos)
        nlen = block[0]
        names.append(block[1 : 1 + nlen].decode("windows-1252", errors="replace"))
    if new_name not in names:
        names.append(new_name)
    out = write_u32(len(params)) + params + write_u32(len(names))
    for n in names:
        enc = n.encode("windows-1252")
        inner = bytes([len(enc)]) + enc
        out += put_block(inner)
    return out


def update_component_params_toc(old: bytes, new_name: str, pad_count: int, height: str) -> bytes:
    # old: u32 len + text lines Name=...|\r\n
    plen, pos = read_u32(old, 0)
    text = old[4 : 4 + plen].decode("windows-1252", errors="replace")
    lines = [ln for ln in text.split("\r\n") if ln.strip()]
    # remove existing same name
    lines = [ln for ln in lines if not ln.startswith(f"Name={new_name}|")]
    lines.append(
        f"Name={new_name}|Pad Count={pad_count}|Height={height.replace('.', ',')}|Description="
    )
    new_text = "\r\n".join(lines) + "\r\n"
    payload = new_text.encode("windows-1252") + b"\x00"
    return write_u32(len(payload)) + payload, len(lines)


# ---------------------------------------------------------------------------
# OLE helpers — full rewrite via CompoundFileWriter (Editor loses footprints)
# ---------------------------------------------------------------------------

def _ole_read_all(src: Path) -> dict[str, bytes | dict]:
    """
    Read entire OLE tree into nested dict:
      { 'FileHeader': bytes, 'Library': { 'Data': bytes, 'Models': { '0': bytes, ... } }, ... }
    """
    ole = olefile.OleFileIO(str(src))

    def ensure(tree: dict, parts: list[str]) -> dict:
        node = tree
        for p in parts:
            node = node.setdefault(p, {})
        return node

    root: dict = {}
    for entry in ole.listdir(streams=True, storages=False):
        data = ole.openstream(entry).read()
        if len(entry) == 1:
            root[entry[0]] = data
        else:
            parent = ensure(root, list(entry[:-1]))
            parent[entry[-1]] = data
    # ensure storage keys exist even if only nested
    for entry in ole.listdir(streams=False, storages=True):
        ensure(root, list(entry))
    ole.close()
    return root


def _writer_add_tree(writer: CompoundFileWriter, parent, tree: dict) -> None:
    for name, value in tree.items():
        if isinstance(value, dict):
            storage = writer.create_storage(parent, name)
            _writer_add_tree(writer, storage, value)
        else:
            writer.create_stream(parent, name, value)


def list_footprint_storages(tree: dict) -> list[str]:
    return sorted(
        name
        for name, value in tree.items()
        if isinstance(value, dict) and "Data" in value
    )


def parse_fragment_role(name: str) -> tuple[str, str] | None:
    """Return (stem, ROLE) if name is *BASE / *BEGIN / *PIN / *END."""
    upper = name.upper()
    for role in FRAGMENT_ROLES:
        for sep in ("-", "_"):
            suffix = sep + role
            if upper.endswith(suffix):
                return name[: -len(suffix)], role
        if upper == role:
            return "", role
    return None


def _group_fragment_sets(tree: dict) -> dict[str, dict[str, str]]:
    groups: dict[str, dict[str, str]] = {}
    for name in list_footprint_storages(tree):
        parsed = parse_fragment_role(name)
        if not parsed:
            continue
        stem, role = parsed
        groups.setdefault(stem.upper(), {})[role] = name
    return groups


def _complete_fragment_sets(groups: dict[str, dict[str, str]]) -> list[dict[str, str]]:
    return [
        roles
        for roles in groups.values()
        if all(r in roles for r in FRAGMENT_ROLES)
    ]


def _fragment_stem(roles: dict[str, str]) -> str:
    parsed = parse_fragment_role(roles["BASE"])
    return parsed[0] if parsed else ""


def _stem_match_score(stem: str, name_prefix: str) -> int:
    """0 = exact match to prefix stem, 99 = no match."""
    expected = expected_fragment_stem(name_prefix).upper()
    stem_u = stem.upper()
    if stem_u == expected:
        return 0
    return 99


def discover_fragments(
    tree: dict,
    name_prefix: str | None = None,
) -> dict[str, str]:
    """
    Find one consistent set of BASE/BEGIN/PIN/END footprints.

    When name_prefix is given (e.g. PLS-2.54), pick that set.
    Otherwise auto-pick one complete set.
    """
    complete = _complete_fragment_sets(_group_fragment_sets(tree))
    if not complete:
        found = list_footprint_storages(tree)
        raise ValueError(
            "В библиотеке нужны 4 фрагмента с окончаниями BASE, BEGIN, PIN, END "
            f"(префикс любой). Найдено: {', '.join(found) if found else '(пусто)'}"
        )

    if name_prefix is not None:
        filtered = [
            roles
            for roles in complete
            if _stem_match_score(_fragment_stem(roles), name_prefix) < 99
        ]
        if not filtered:
            stems = sorted({_fragment_stem(r) for r in complete})
            exp = expected_fragment_stem(name_prefix)
            raise ValueError(
                f"Нет фрагментов для префикса «{normalize_prefix(name_prefix)}». "
                f"Ожидается: {exp}-BASE, {exp}-BEGIN, {exp}-PIN, {exp}-END. "
                f"Доступные наборы: {', '.join(stems)}"
            )
        filtered.sort(
            key=lambda roles: (
                _stem_match_score(_fragment_stem(roles), name_prefix),
                -len(roles["BASE"]),
                roles["BASE"].upper(),
            )
        )
        return {role: filtered[0][role] for role in FRAGMENT_ROLES}

    complete.sort(
        key=lambda roles: (
            -len(roles["BASE"]),
            roles["BASE"].upper(),
        )
    )
    return {role: complete[0][role] for role in FRAGMENT_ROLES}


def _load_fragments(tree: dict, name_prefix: str) -> dict:
    names = discover_fragments(tree, name_prefix)
    begin_data = tree[names["BEGIN"]]["Data"]
    pin_data = tree[names["PIN"]]["Data"]
    end_data = tree[names["END"]]["Data"]
    base_node = tree[names["BASE"]]

    body_begin = extract_first_body(begin_data)
    body_pin = extract_first_body(pin_data)
    body_end = extract_first_body(end_data)
    missing = []
    if body_begin is None:
        missing.append(names["BEGIN"])
    if body_pin is None:
        missing.append(names["PIN"])
    if body_end is None:
        missing.append(names["END"])
    if missing:
        raise ValueError(
            "Нет ComponentBody (3D) в: " + ", ".join(missing)
        )

    return {
        "names": names,
        "base_data": base_node["Data"],
        "begin_data": begin_data,
        "pin_data": pin_data,
        "end_data": end_data,
        "base_wide": base_node.get("WideStrings", write_u32(1) + b"\x00"),
        "height": parse_height_from_params(base_node.get("Parameters", b"")),
        "body_begin": body_begin,
        "body_pin": body_pin,
        "body_end": body_end,
        "body_models": {
            "BEGIN": body_model_name(body_begin),
            "PIN": body_model_name(body_pin),
            "END": body_model_name(body_end),
        },
    }


def compose_one(
    tree: dict,
    frag: dict,
    cols: int,
    rows: int,
    mode: str,
    pitch_mm: float,
    name_prefix: str,
) -> dict:
    """Add one composed footprint into tree (mutates tree)."""
    mode = mode.upper()
    pitch_internal = mm_to_internal(pitch_mm)
    name = target_name(cols, rows, name_prefix)
    half = ((cols - 1) * pitch_internal) // 2
    pin_copies = cols - 2

    stats = {"pads_assigned": 0, "pads_skipped": 0}
    records = bytearray()
    total_prims = 0
    body_sources: list[str] = []

    blob, n = transform_footprint_data(
        frag["base_data"], 0, None, 0, cols, rows, mode, stats
    )
    records += blob
    total_prims += n

    blob, n = transform_footprint_data(
        frag["begin_data"], -half, "begin", 1, cols, rows, mode, stats
    )
    records += blob
    total_prims += n

    for i in range(pin_copies):
        dx = -half + (i + 1) * pitch_internal
        blob, n = transform_footprint_data(
            frag["pin_data"], dx, "pin", i + 2, cols, rows, mode, stats
        )
        records += blob
        total_prims += n

    blob, n = transform_footprint_data(
        frag["end_data"], half, "end", cols, cols, rows, mode, stats
    )
    records += blob
    total_prims += n

    for col in range(1, cols + 1):
        dx = -half + (col - 1) * pitch_internal
        if col == 1:
            tpl, src_name = frag["body_begin"], "BEGIN"
        elif col == cols:
            tpl, src_name = frag["body_end"], "END"
        else:
            tpl, src_name = frag["body_pin"], "PIN"
        records += body_record_bytes(place_body(tpl, dx))
        total_prims += 1
        body_sources.append(src_name)

    data_stream = build_data_stream(name, bytes(records))
    uid_header, uid_data = build_unique_ids(data_stream)
    tree[name] = {
        "Header": write_u32(total_prims),
        "Data": data_stream,
        "Parameters": build_parameters(name, frag["height"]),
        "WideStrings": frag["base_wide"],
        "UniqueIDPrimitiveInformation": {
            "Header": uid_header,
            "Data": uid_data,
        },
    }

    tree["Library"]["Data"] = update_library_data(tree["Library"]["Data"], name)
    toc = tree["Library"].get("ComponentParamsTOC")
    if isinstance(toc, dict) and "Data" in toc:
        toc_new, nlines = update_component_params_toc(
            toc["Data"], name, stats["pads_assigned"], frag["height"]
        )
        toc["Data"] = toc_new
        toc["Header"] = write_u32(nlines)

    expected = cols * rows
    return {
        "name": name,
        "cols": cols,
        "rows": rows,
        "mode": mode,
        "pitch_mm": pitch_mm,
        "name_prefix": name_prefix,
        "pads_assigned": stats["pads_assigned"],
        "pads_skipped": stats["pads_skipped"],
        "expected_pads": expected,
        "primitives": total_prims,
        "bodies": cols,
        "body_sources": body_sources,
        "body_models": frag["body_models"],
    }


def write_library(tree: dict, dst: Path) -> None:
    if dst.exists():
        raise ValueError(f"Файл уже существует: {dst}")
    with CompoundFileWriter(str(dst)) as writer:
        _writer_add_tree(writer, writer.root, tree)


def compose(
    src: Path,
    dst: Path,
    cols: int,
    rows: int,
    mode: str,
    pitch_mm: float,
    name_prefix: str,
) -> dict:
    if cols < 2:
        raise ValueError("Число колонок должно быть >= 2 (BEGIN + END)")
    if rows < 1:
        raise ValueError("Число рядов должно быть >= 1")
    if pitch_mm <= 0:
        raise ValueError("Шаг (pitch) должен быть > 0 mm")
    mode = mode.upper()
    if mode not in ("H", "V"):
        raise ValueError("Режим нумерации: H или V")

    tree = _ole_read_all(src)
    frag = _load_fragments(tree, name_prefix)
    info = compose_one(tree, frag, cols, rows, mode, pitch_mm, name_prefix)
    write_library(tree, dst)
    info["output"] = str(dst)
    info["fragments"] = frag["names"]
    return info


def compose_range(
    src: Path,
    dst: Path,
    min_cols: int,
    max_cols: int,
    rows: int,
    mode: str,
    pitch_mm: float,
    name_prefix: str,
) -> dict:
    if min_cols < 2:
        raise ValueError("min колонок должно быть >= 2")
    if max_cols < min_cols:
        raise ValueError("max колонок должно быть >= min")
    if pitch_mm <= 0:
        raise ValueError("Шаг (pitch) должен быть > 0 mm")
    mode = mode.upper()
    if mode not in ("H", "V"):
        raise ValueError("Режим нумерации: H или V")
    if rows < 1:
        raise ValueError("Число рядов должно быть >= 1")

    tree = _ole_read_all(src)
    frag = _load_fragments(tree, name_prefix)
    created: list[dict] = []

    for cols in range(min_cols, max_cols + 1):
        created.append(
            compose_one(tree, frag, cols, rows, mode, pitch_mm, name_prefix)
        )

    write_library(tree, dst)
    fp_prefix = footprint_name_base(name_prefix)
    return {
        "min_cols": min_cols,
        "max_cols": max_cols,
        "rows": rows,
        "mode": mode,
        "pitch_mm": pitch_mm,
        "name_prefix": name_prefix,
        "footprint_prefix": fp_prefix,
        "count": len(created),
        "names": [c["name"] for c in created],
        "created": created,
        "body_models": frag["body_models"],
        "fragments": frag["names"],
        "output": str(dst),
    }


DEFAULT_SOURCE = Path(
    r"c:\Users\murashkindv\Desktop\Components\Mechanical_Connectors_Rectangular_2.54_PL.PcbLib"
)


def find_default_master() -> Path | None:
    """First existing .PcbLib near exe / cwd / default path."""
    dirs: list[Path] = []
    if getattr(sys, "frozen", False):
        exe_dir = Path(sys.executable).resolve().parent
        dirs.extend([exe_dir, exe_dir.parent])
    dirs.append(Path.cwd())
    dirs.append(DEFAULT_SOURCE.parent)

    seen: set[Path] = set()
    for folder in dirs:
        folder = folder.resolve()
        if folder in seen or not folder.is_dir():
            continue
        seen.add(folder)
        preferred = folder / DEFAULT_SOURCE.name
        if preferred.is_file():
            return preferred
        libs = sorted(folder.glob("*.PcbLib"))
        if libs:
            return libs[0].resolve()
    if DEFAULT_SOURCE.is_file():
        return DEFAULT_SOURCE.resolve()
    return None


def peek_fragments(src: Path, name_prefix: str) -> dict[str, str]:
    tree = _ole_read_all(src)
    return discover_fragments(tree, name_prefix)


def _unique_output_in_dir(out_dir: Path, stem: str) -> Path:
    """Pick a free .PcbLib name in out_dir (never overwrite)."""
    base = out_dir / f"{stem}.PcbLib"
    if not base.exists():
        return base
    for n in range(2, 1000):
        candidate = out_dir / f"{stem}_{n}.PcbLib"
        if not candidate.exists():
            return candidate
    raise SystemExit(f"Cannot find free output name in {out_dir}")


def default_output_path(src: Path, cols: int, rows: int, name_prefix: str) -> Path:
    """New uniquely named .PcbLib next to the source library."""
    src = src.resolve()
    fp_name = target_name(cols, rows, name_prefix)
    safe = fp_name.replace("/", "_")
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    return _unique_output_in_dir(src.parent, f"{src.stem}__{safe}_{stamp}")


def default_batch_output_path(
    src: Path, min_cols: int, max_cols: int, name_prefix: str
) -> Path:
    """New .PcbLib with footprint range in one file, next to the source library."""
    src = src.resolve()
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    prefix = normalize_prefix(name_prefix)
    label = f"{prefix}-{min_cols}-{max_cols}"
    return _unique_output_in_dir(src.parent, f"{src.stem}__{label}_{stamp}")


def preview_batch_output_path(
    src: Path, min_cols: int, max_cols: int, name_prefix: str
) -> str:
    """Human-readable output path hint for the GUI."""
    src = src.resolve()
    prefix = normalize_prefix(name_prefix)
    label = f"{prefix}-{min_cols}-{max_cols}"
    return str(src.parent / f"{src.stem}__{label}_<timestamp>.PcbLib")


def print_single_result(info: dict) -> None:
    print(f"Created: {info['name']}")
    print(f"Output:  {info['output']}")
    if info.get("fragments"):
        fr = info["fragments"]
        print(
            "Fragments: "
            f"BASE={fr['BASE']}, BEGIN={fr['BEGIN']}, "
            f"PIN={fr['PIN']}, END={fr['END']}"
        )
    print(
        f"Cols={info['cols']} Rows={info['rows']} Mode={info['mode']} "
        f"Pitch={info['pitch_mm']}mm"
    )
    print(
        f"Pads: {info['pads_assigned']}/{info['expected_pads']} "
        f"(skipped {info['pads_skipped']}), primitives={info['primitives']}, "
        f"3D bodies={info['bodies']}"
    )


def print_batch_result(info: dict) -> None:
    print(f"Output:  {info['output']}")
    if info.get("fragments"):
        fr = info["fragments"]
        print(
            "Fragments: "
            f"BASE={fr['BASE']}, BEGIN={fr['BEGIN']}, "
            f"PIN={fr['PIN']}, END={fr['END']}"
        )
    print(f"Pitch={info['pitch_mm']}mm  prefix={info['footprint_prefix']}")
    print(
        f"Range: {info['footprint_prefix']}{info['min_cols']} .. "
        f"{info['footprint_prefix']}{info['max_cols']} "
        f"({info['count']} footprints), rows={info['rows']}, mode={info['mode']}"
    )
    print("Footprints:", ", ".join(info["names"]))


# ---------------------------------------------------------------------------
# GUI
# ---------------------------------------------------------------------------

def run_gui() -> int:
    import tkinter as tk
    from tkinter import filedialog, messagebox, ttk

    root = tk.Tk()
    root.title(APP_NAME)
    root.minsize(560, 700)
    root.geometry("680x780")

    master_var = tk.StringVar(value=str(find_default_master() or ""))
    pitch_var = tk.StringVar(value=str(DEFAULT_PITCH_MM))
    prefix_var = tk.StringVar(value=DEFAULT_NAME_PREFIX)
    min_var = tk.StringVar(value="2")
    max_var = tk.StringVar(value="10")
    rows_var = tk.StringVar(value="1")
    mode_var = tk.StringVar(value="V")
    frag_var = tk.StringVar(value="Фрагменты: (выберите .PcbLib)")
    output_var = tk.StringVar(value="Выходной файл: (рядом с библиотекой)")
    status_var = tk.StringVar(value="Готово к работе")

    main = ttk.Frame(root, padding=12)
    main.pack(fill=tk.BOTH, expand=True)

    ttk.Label(main, text=APP_NAME, font=("Segoe UI", 14, "bold")).pack(anchor=tk.W)

    req = tk.Label(
        main,
        text=REQUIREMENTS_TEXT,
        justify=tk.LEFT,
        anchor=tk.W,
        bg="#f4f4f0",
        relief=tk.SOLID,
        bd=1,
        padx=8,
        pady=8,
        font=("Segoe UI", 9),
    )
    req.pack(fill=tk.X, pady=(8, 12))

    lib_row = ttk.Frame(main)
    lib_row.pack(fill=tk.X, pady=2)
    ttk.Label(lib_row, text="Библиотека (.PcbLib):", width=22).pack(side=tk.LEFT)
    ttk.Entry(lib_row, textvariable=master_var).pack(
        side=tk.LEFT, fill=tk.X, expand=True, padx=(0, 6)
    )

    def browse_lib() -> None:
        path = filedialog.askopenfilename(
            title="Выберите мастер .PcbLib",
            filetypes=[("Altium PCB Library", "*.PcbLib"), ("All files", "*.*")],
        )
        if path:
            master_var.set(path)
            refresh_fragments()

    ttk.Button(lib_row, text="Обзор…", command=browse_lib).pack(side=tk.LEFT)

    frag_lbl = tk.Label(
        main,
        textvariable=frag_var,
        foreground="#333333",
        justify=tk.LEFT,
        anchor=tk.W,
        wraplength=600,
    )
    frag_lbl.pack(anchor=tk.W, fill=tk.X, pady=(4, 2))
    ttk.Label(main, textvariable=output_var, foreground="#555").pack(
        anchor=tk.W, pady=(0, 10)
    )

    form = ttk.Frame(main)
    form.pack(fill=tk.X)

    def add_field(row: int, label: str, var: tk.StringVar, width: int = 12) -> None:
        ttk.Label(form, text=label, width=22).grid(
            row=row, column=0, sticky=tk.W, pady=3
        )
        ttk.Entry(form, textvariable=var, width=width).grid(
            row=row, column=1, sticky=tk.W, pady=3
        )

    add_field(0, "Префикс:", prefix_var)
    add_field(1, "Шаг копирования, mm:", pitch_var)
    add_field(2, "Колонки от (min):", min_var)
    add_field(3, "Колонки до (max):", max_var)
    add_field(4, "Рядов:", rows_var)

    ttk.Label(form, text="Нумерация:", width=22).grid(
        row=5, column=0, sticky=tk.W, pady=3
    )
    ttk.Combobox(
        form,
        textvariable=mode_var,
        values=("V", "H"),
        width=10,
        state="readonly",
    ).grid(row=5, column=1, sticky=tk.W, pady=3)

    # Bottom bar first (side=BOTTOM), so buttons stay visible
    bottom = ttk.Frame(main)
    bottom.pack(side=tk.BOTTOM, fill=tk.X, pady=(8, 0))

    btn_row = ttk.Frame(bottom)
    btn_row.pack(fill=tk.X)

    status_lbl = ttk.Label(bottom, textvariable=status_var)
    status_lbl.pack(anchor=tk.W, pady=(6, 0))

    log = tk.Text(main, height=14, wrap=tk.WORD, font=("Consolas", 9))
    log.pack(fill=tk.BOTH, expand=True, pady=(12, 6))

    def log_line(msg: str) -> None:
        log.insert(tk.END, msg + "\n")
        log.see(tk.END)
        root.update_idletasks()

    def refresh_output_hint() -> None:
        path = Path(master_var.get().strip())
        if not path.is_file():
            output_var.set("Выходной файл: (рядом с библиотекой)")
            return
        try:
            prefix = prefix_var.get().strip() or DEFAULT_NAME_PREFIX
            min_cols = int(min_var.get())
            max_cols = int(max_var.get())
            hint = preview_batch_output_path(path, min_cols, max_cols, prefix)
            output_var.set(f"Выходной файл: {hint}")
        except (ValueError, OSError):
            output_var.set(
                f"Выходной файл: {path.resolve().parent}\\…_<timestamp>.PcbLib"
            )

    def refresh_fragments() -> None:
        path = Path(master_var.get().strip())
        prefix = prefix_var.get().strip() or DEFAULT_NAME_PREFIX
        frag_var.set(f"Фрагменты: поиск «{prefix}»…")
        status_var.set("Проверка…")
        root.update_idletasks()

        if not path.is_file():
            frag_var.set("Фрагменты: файл библиотеки не найден")
            status_var.set("Файл не найден")
            refresh_output_hint()
            return
        try:
            names = peek_fragments(path.resolve(), prefix)
            exp = expected_fragment_stem(prefix)
            line = (
                f"Фрагменты ({exp}): "
                f"BASE={names['BASE']} | BEGIN={names['BEGIN']} | "
                f"PIN={names['PIN']} | END={names['END']}"
            )
            frag_var.set(line)
            status_var.set(f"Фрагменты найдены: {exp}")
            log_line(f"OK prefix={prefix}: {line}")
            refresh_output_hint()
        except Exception as exc:
            frag_var.set(f"Фрагменты не найдены (префикс «{prefix}»): {exc}")
            status_var.set("Фрагменты не найдены")
            log_line(f"CHECK FAIL prefix={prefix}: {exc}")
            refresh_output_hint()

    # Prefix/min/max affect names & fragment lookup; pitch is spacing only
    for var in (prefix_var, min_var, max_var):
        var.trace_add("write", lambda *_: refresh_fragments())
    pitch_var.trace_add("write", lambda *_: refresh_output_hint())

    def on_resize(_event=None) -> None:
        frag_lbl.configure(wraplength=max(400, root.winfo_width() - 40))

    root.bind("<Configure>", on_resize)

    def run_generate() -> None:
        try:
            src = Path(master_var.get().strip()).resolve()
            if not src.is_file():
                raise ValueError("Укажите существующий файл .PcbLib")
            pitch = float(pitch_var.get().replace(",", "."))
            prefix = prefix_var.get().strip() or DEFAULT_NAME_PREFIX
            min_cols = int(min_var.get())
            max_cols = int(max_var.get())
            rows = int(rows_var.get())
            mode = mode_var.get().upper()
            dst = default_batch_output_path(src, min_cols, max_cols, prefix)
            status_var.set("Генерация…")
            log_line(f"Master: {src}")
            log_line(f"Prefix: {prefix}  Pitch (spacing): {pitch} mm")
            log_line(f"Output: {dst}")
            info = compose_range(
                src, dst, min_cols, max_cols, rows, mode, pitch, prefix
            )
            fr = info["fragments"]
            log_line(
                f"Fragments: BASE={fr['BASE']}, BEGIN={fr['BEGIN']}, "
                f"PIN={fr['PIN']}, END={fr['END']}"
            )
            log_line(
                f"Created {info['count']} footprints: "
                + ", ".join(info["names"])
            )
            log_line("OK")
            status_var.set("Готово")
            messagebox.showinfo(
                APP_NAME,
                f"Создано {info['count']} footprint'ов\n\n{info['output']}",
            )
        except Exception as exc:
            status_var.set("Ошибка")
            log_line(f"ERROR: {exc}")
            messagebox.showerror(APP_NAME, str(exc))

    ttk.Button(btn_row, text="Проверить фрагменты", command=refresh_fragments).pack(
        side=tk.LEFT, padx=(0, 8)
    )
    gen_btn = ttk.Button(btn_row, text="Сгенерировать", command=run_generate)
    gen_btn.pack(side=tk.RIGHT, ipadx=16, ipady=4)

    if master_var.get():
        refresh_fragments()

    root.mainloop()
    return 0


def main(argv: list[str] | None = None) -> int:
    if argv is None:
        argv = sys.argv[1:]

    # Default: GUI when no CLI flags (exe double-click or bare python launch)
    if not argv or argv == ["--gui"]:
        return run_gui()

    p = argparse.ArgumentParser(
        description="Linear connector footprint generator (any .PcbLib with BASE/BEGIN/PIN/END)."
    )
    p.add_argument("--gui", action="store_true", help="Open GUI")
    p.add_argument(
        "library",
        type=Path,
        nargs="?",
        default=None,
        help="Master .PcbLib (any prefix; needs *BASE *BEGIN *PIN *END)",
    )
    g = p.add_mutually_exclusive_group()
    g.add_argument(
        "-c", "--cols", type=int, help="Single footprint column count (>=2)"
    )
    g.add_argument(
        "--max", type=int, metavar="N", help="Batch: create footprints for 2..N columns"
    )
    p.add_argument("--min", type=int, default=2, help="Batch start column count")
    p.add_argument(
        "-p",
        "--pitch",
        type=float,
        default=DEFAULT_PITCH_MM,
        help="Column spacing when copying pads (mm); not used in names/lookup",
    )
    p.add_argument(
        "--prefix",
        default=DEFAULT_NAME_PREFIX,
        help="Fragment stem and output name base, e.g. PLS-2.54",
    )
    p.add_argument("-r", "--rows", type=int, default=1)
    p.add_argument(
        "-m",
        "--mode",
        default="V",
        choices=["H", "V", "h", "v"],
        help="Pad numbering: V=column-major (default), H=row-major",
    )
    p.add_argument("-o", "--output", type=Path, default=None)
    args = p.parse_args(argv)

    if args.gui:
        return run_gui()

    src = (args.library or find_default_master())
    if src is None or not Path(src).is_file():
        print("Master library not found. Pass path or use --gui.", file=sys.stderr)
        return 1
    src = Path(src).resolve()

    try:
        if args.max is not None:
            if args.max < args.min:
                raise ValueError("--max must be >= --min")
            dst = (
                args.output.resolve()
                if args.output
                else default_batch_output_path(src, args.min, args.max, args.prefix)
            )
            if dst.resolve() == src.resolve():
                raise ValueError("Refusing to overwrite the master library")
            info = compose_range(
                src,
                dst,
                args.min,
                args.max,
                args.rows,
                args.mode.upper(),
                args.pitch,
                args.prefix,
            )
            print_batch_result(info)
            return 0

        if args.cols is None:
            p.error("Specify -c COLS, --max N, or --gui")

        dst = (
            args.output.resolve()
            if args.output
            else default_output_path(src, args.cols, args.rows, args.prefix)
        )
        if dst.resolve() == src.resolve():
            raise ValueError("Refusing to overwrite the master library")
        info = compose(
            src,
            dst,
            args.cols,
            args.rows,
            args.mode.upper(),
            args.pitch,
            args.prefix,
        )
        print_single_result(info)
        if info["pads_assigned"] != info["expected_pads"]:
            print("WARNING: pad count mismatch", file=sys.stderr)
            return 2
        return 0
    except Exception as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
