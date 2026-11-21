# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Quantag IT Solutions GmbH
"""Turn the output log in data memory into text (C printf semantics), on the host after a run.

Log layout at data offset 0: word 0 = record count; record k (k = 1, 2, ...) at byte 32*k:
    kind (1 = printf, 2 = puts, 3 = putchar), then X0..X6 (32-bit each).
Strings (format strings, %s arguments) are read from the program's data image: constant data
such as string literals. Text that the program builds at run time is not visible this way.
"""
import re

RECORD = 32
SPEC = re.compile(r"%([-+ #0]*)(\*|\d+)?(?:\.(\*|\d+))?(hh|h|ll|l|j|z|t|L)?([diouxXcspn%])")


def capacity(log_bytes):
    return log_bytes // RECORD - 1


def _cstr(image, offset, limit=4096):
    if offset is None or not 0 <= offset < len(image):
        return None
    end = image.find(b"\0", offset, offset + limit)
    return image[offset:end if end >= 0 else offset + limit].decode("latin-1")


def c_format(fmt, args, image, ptr_off):
    """Format like C printf. args: 32-bit ints; ptr_off(v) -> offset in image for a pointer value."""
    out, ai = [], 0

    def nxt():
        nonlocal ai
        v = args[ai] if ai < len(args) else 0
        ai += 1
        return v
    pos = 0
    for m in SPEC.finditer(fmt):
        out.append(fmt[pos:m.start()])
        pos = m.end()
        flags, width, prec, length, conv = m.groups()
        if conv == "%":
            out.append("%"); continue
        if width == "*": width = str(nxt())
        if prec == "*": prec = str(nxt())
        py = "%" + flags + (width or "") + ("." + prec if prec is not None else "")
        v = nxt()
        u = v & 0xFFFFFFFF
        s = u - (1 << 32) if u >> 31 else u
        if conv in "di":
            out.append((py + "d") % s)
        elif conv in "ouxX":
            out.append((py + conv) % u)
        elif conv == "c":
            out.append((py + "c") % chr(u & 0xFF))
        elif conv == "s":
            txt = _cstr(image, ptr_off(u))
            out.append((py + "s") % (txt if txt is not None else "(?)"))
        elif conv == "p":
            out.append("0x%x" % (0x10000000 + (ptr_off(u) or 0)))
        else:
            out.append(m.group(0))
    out.append(fmt[pos:])
    return "".join(out)


def render(cells, image, ptr_off, n_records):
    """cells(i) -> 32-bit value of data word i. Returns (text, truncated)."""
    count = cells(0)
    text = []
    for k in range(1, min(count, n_records) + 1):
        base = k * RECORD // 4
        kind = cells(base)
        x = [cells(base + 1 + j) for j in range(7)]
        if kind == 1:
            fmt = _cstr(image, ptr_off(x[0]))
            text.append(c_format(fmt, x[1:], image, ptr_off) if fmt is not None else "(?)")
        elif kind == 2:
            s = _cstr(image, ptr_off(x[0]))
            text.append((s if s is not None else "(?)") + "\n")
        elif kind == 3:
            text.append(chr(x[0] & 0xFF))
    return "".join(text), count > n_records
