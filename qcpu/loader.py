# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Quantag IT Solutions GmbH
"""Load and link machine code for the quantum machine (AArch64, RISC-V and x86-64).

Reads an ELF or Mach-O object/executable (or a raw .bin), merges all executable sections into
one code image, lays out the data sections (.rodata, .data, .bss, string literals) in data
memory, and applies the relocations a linker would.
  AArch64: BL/B (CALL26/JUMP26, BRANCH26), ADRP (ADR_PREL_PG_HI21, PAGE21), :lo12:
           (ADD_ABS_LO12_NC, LDSTxx_ABS_LO12_NC, PAGEOFF12), ABS64.
  RISC-V:  CALL/CALL_PLT (auipc + jalr), JAL, BRANCH, HI20 / LO12_I / LO12_S,
           PCREL_HI20 / PCREL_LO12_I / PCREL_LO12_S, 64 / 32 (pointers in data).
  x86-64:  PLT32 / PC32 (calls, RIP-relative data), 32 / 32S / 64 (absolute addresses).

printf / puts / putchar are bound to an output stub: a few instructions of the same architecture
that run on the machine like any other code and append one record per call to the output log in
data memory (see qcpu/console.py, which turns the log into text after the run).
"""
import os
from .reference import CODE_BASE, DATA_BASE

# A64 machine code of the output stub (assembled once from the listing in docs/output_stub.s)
OUTPUT_STUB = [   # assembled from docs/output_stub.s
    0x5280002c, 0x5280000d, 0x14000007, 0x5280004c, 0x5280000d, 0x14000004,
    0x5280006c, 0x12001c0d, 0x14000001, 0xd2a20009, 0xb940012a, 0x713ffd5f,
    0x54000182, 0x1100054b, 0xb900012b, 0x8b0b152a, 0xb900014c, 0xb9000540,
    0xb9000941, 0xb9000d42, 0xb9001143, 0xb9001544, 0xb9001945, 0xb9001d46,
    0x2a0d03e0, 0xd65f03c0, 0xb94003e1, 0xb9400be2, 0xb94013e3, 0xb9401be4,
    0xb94023e5, 0xb9402be6, 0x17ffffe0,
]
STUB_ENTRY = {"printf": 0, "puts": 3, "putchar": 6, "printf@apple": 26}
STUB_CAPACITY_WORD = 11
# RISC-V machine code of the output stub (assembled from docs/output_stub_riscv.s; the same words for
# RV32 and RV64). Entry points and the capacity word are at the same indices as in the A64 stub.
OUTPUT_STUB_RV = [
    0x00100293, 0x00000313, 0x01c0006f, 0x00200293, 0x00000313, 0x0100006f,
    0x00300293, 0x0ff57313, 0x0040006f, 0x100003b7, 0x0003ae03, 0x7ff00e93,
    0x03de7a63, 0x001e0e13, 0x01c3a023, 0x005e1e13, 0x007e0e33, 0x005e2023,
    0x00ae2223, 0x00be2423, 0x00ce2623, 0x00de2823, 0x00ee2a23, 0x00fe2c23,
    0x010e2e23, 0x00030513, 0x00008067,
]
# x86-64 machine code of the output stub (assembled from docs/output_stub_x86.s). Entry points and the
# position of the capacity immediate are byte offsets.
OUTPUT_STUB_X86 = bytes.fromhex(
    "41ba010000004531dbeb1541ba020000004531dbeb0a41ba03000000440fb6dfb8000000108138ff070000732cff008b00c1e005"
    "050000001044891089780489700889500c8948104489401444894818448b5424084489501c4489d8c3")
STUB_ENTRY_X86 = {"printf": 0, "puts": 11, "putchar": 22}
STUB_CAPACITY_X86 = 39
OUTPUT_FUNCS = ("printf", "puts", "putchar")


class LinkError(Exception):
    pass


def _put(code, off, w):
    code[off:off + 4] = (w & 0xFFFFFFFF).to_bytes(4, "little")


def _get(code, off):
    return int.from_bytes(code[off:off + 4], "little")


class ObjectImage:
    """Sections, symbols and relocations of one object file, before layout."""

    def __init__(self, path, arch=None):
        """arch: architecture of a raw binary; an ELF or Mach-O file carries its own."""
        self.path = path
        self.arch = arch or "arm64"
        data = open(path, "rb").read()
        magic = data[:4]
        self.code = bytearray()
        self.funcs = {}            # name -> byte offset in self.code
        self.data_secs = []        # (key, bytes, align)
        self.syms = {}             # name -> ("code", off) | ("data", key, off)
        self.relocs = []           # (code_off, kind, target, addend)   target: ("sym", name) | ("data", key, off)
        self.data_relocs = []      # (key, off, target, addend) for ABS64 pointers in data
        self.apple = False
        if magic == b"\x7fELF":
            self._elf()
        elif magic in (b"\xcf\xfa\xed\xfe", b"\xca\xfe\xba\xbe", b"\xbe\xba\xfe\xca"):
            self._macho()
        else:
            if len(data) % 4:
                raise LinkError("raw binary length is not a multiple of 4")
            self.code = bytearray(data)
            self.funcs = {os.path.basename(path): 0}

    # ---------------------------------------------------------------- ELF
    def _elf(self):
        import lief
        lief.logging.disable()
        b = lief.parse(self.path)
        if b.header.machine_type == lief.ELF.ARCH.AARCH64:
            self.arch, prefix = "arm64", "AARCH64_"
        elif b.header.machine_type == lief.ELF.ARCH.RISCV:
            self.arch = "riscv64" if b.header.identity_class == lief.ELF.Header.CLASS.ELF64 else "riscv32"
            prefix = "RISCV_"
        elif b.header.machine_type == lief.ELF.ARCH.X86_64:
            self.arch, prefix = "x86_64", "X86_64_"
        else:
            raise LinkError("unsupported ELF machine type (supported: AArch64, RISC-V, x86-64)")
        rel = b.header.file_type == lief.ELF.Header.FILE_TYPE.REL
        secs = list(b.sections)
        X = lief.ELF.Section.FLAGS.EXECINSTR
        A = lief.ELF.Section.FLAGS.ALLOC
        sec_place = {}   # section index -> ("code", base) | ("data", key)
        for i, s in enumerate(secs):
            if not s.has(A) or s.size == 0:
                continue
            if s.has(X):
                while len(self.code) % 4 and self.arch != "x86_64": self.code += b"\0"
                sec_place[i] = ("code", len(self.code), s.virtual_address)
                self.code += bytes(s.content)
            elif s.name.startswith((".rodata", ".data", ".bss", ".sdata", ".sbss", ".srodata")):
                content = bytes(s.content) if s.type != lief.ELF.Section.TYPE.NOBITS else bytes(s.size)
                self.data_secs.append((s.name + f"#{i}", content, max(4, s.alignment)))
                sec_place[i] = ("data", s.name + f"#{i}", s.virtual_address)
        for s in b.symbols:
            if not s.name or s.shndx not in sec_place:
                continue
            kind, where, va = sec_place[s.shndx]
            off = s.value - (0 if rel else va)
            if kind == "code":
                self.syms[s.name] = ("code", where + off)
                if s.type == lief.ELF.Symbol.TYPE.FUNC:
                    self.funcs[s.name] = where + off
            else:
                self.syms[s.name] = ("data", where, off)
        if not rel:
            return
        for r in b.relocations:
            sec = getattr(r, "section", None)
            if sec is None:
                continue
            si = next((i for i, s in enumerate(secs) if s.name == sec.name and s.offset == sec.offset), None)
            if si not in sec_place:
                continue
            kind, where, _ = sec_place[si]
            sym = r.symbol
            if sym.type == lief.ELF.Symbol.TYPE.SECTION or not sym.name:
                if sym.shndx not in sec_place:
                    tgt = ("sym", "<section %d>" % sym.shndx)
                else:
                    k2, w2, _ = sec_place[sym.shndx]
                    tgt = ("code", w2 + sym.value) if k2 == "code" else ("data", w2, sym.value)
            else:
                tgt = ("sym", sym.name)
            t = str(r.type).split(".")[-1].replace(prefix, "")
            if t == "RELAX":                    # RISC-V linker-relaxation hint: nothing to apply
                continue
            if kind == "code":
                self.relocs.append((where + r.address, t, tgt, r.addend))
            else:
                self.data_relocs.append((where, r.address, t, tgt, r.addend))

    # ---------------------------------------------------------------- Mach-O
    def _macho(self):
        import lief
        lief.logging.disable()
        b = lief.parse(self.path)
        if isinstance(b, lief.MachO.FatBinary):
            b = next(x for x in b if x.header.cpu_type == lief.MachO.Header.CPU_TYPE.ARM64)
        if b.header.cpu_type != lief.MachO.Header.CPU_TYPE.ARM64:
            raise LinkError("not an arm64 Mach-O file")
        self.apple = True
        place = []        # (va_lo, va_hi, kind, where)
        for s in b.sections:
            if s.size == 0 or s.segment_name == "__LD" or s.name in ("__compact_unwind", "__eh_frame"):
                continue
            if s.name == "__text":
                place.append((s.virtual_address, s.virtual_address + s.size, "code", len(self.code), s))
                self.code += bytes(s.content)
            elif s.name in ("__cstring", "__const", "__data", "__bss", "__common", "__literal4", "__literal8"):
                key = s.segment_name + "," + s.name
                content = bytes(s.content) if s.name not in ("__bss", "__common") else bytes(s.size)
                self.data_secs.append((key, content, max(4, 1 << s.alignment)))
                place.append((s.virtual_address, s.virtual_address + s.size, "data", key, s))

        def locate(va):
            for lo, hi, kind, where, _ in place:
                if lo <= va < hi or (va == hi and kind == "data"):
                    return (kind, where, va - lo)
            return None
        for s in b.symbols:
            if (s.raw_type & 0x0E) != 0x0E:        # only symbols defined in a section (N_SECT); skip N_UNDF
                continue
            loc = locate(s.value) if s.name else None
            name = s.name[1:] if s.name.startswith("_") else s.name
            if not loc or not s.name or s.name.startswith("ltmp"):
                continue
            if loc[0] == "code":
                self.syms[name] = ("code", loc[1] + loc[2])
                if s.name.startswith("_"):
                    self.funcs[name] = loc[1] + loc[2]
            else:
                self.syms[name] = ("data", loc[1], loc[2])
        for lo, hi, kind, where, s in place:
            pending_addend = 0
            rels = list(s.relocations)
            # LIEF reports Mach-O relocation addresses either section-relative or with the section's
            # file offset added; it is the same for the whole section
            shift = s.offset if rels and min(r.address for r in rels) >= s.offset > 0 else 0
            for r in rels:
                a = r.address - shift
                t = str(r.type).split(".")[-1]
                if t == "ADDEND":
                    pending_addend = r.symbol.value if r.has_symbol else 0
                    continue
                if r.has_symbol:
                    nm = r.symbol.name
                    tgt = ("sym", nm[1:] if nm.startswith("_") else nm)
                elif r.has_section:
                    tgt = ("macho_section_va", r.section.virtual_address)
                else:
                    tgt = ("sym", "?")
                if kind == "code":
                    self.relocs.append((where + a, t, tgt, pending_addend))
                else:
                    self.data_relocs.append((where, a, t, tgt, pending_addend))
                pending_addend = 0

    # ---------------------------------------------------------------- link
    def link(self, entry=None, data_offset=0, log_capacity=0):
        """Place data sections at data_offset (bytes from DATA_BASE) and resolve relocations.
        Returns dict(words, entry, entry_index, functions, data_image, data_end, externs, unresolved)."""
        code = bytearray(self.code)
        x86 = self.arch == "x86_64"
        unit = 1 if x86 else 4                     # bytes per program-memory slot
        while len(code) % unit: code += b"\0"
        externs = sorted({t[1] for _, _, t, _ in self.relocs if t[0] == "sym" and t[1] not in self.syms})
        uses_output = any(e in OUTPUT_FUNCS for e in externs)
        stub_base = None
        riscv = self.arch.startswith("riscv")
        if uses_output:
            stub_base = len(code)
            if x86:
                stub = None                             # cmp dword [rax], capacity: imm32
                code += OUTPUT_STUB_X86[:STUB_CAPACITY_X86] + log_capacity.to_bytes(4, "little") + OUTPUT_STUB_X86[STUB_CAPACITY_X86 + 4:]
            elif riscv:
                stub = list(OUTPUT_STUB_RV)             # li t4, capacity: I-type immediate
                stub[STUB_CAPACITY_WORD] = (stub[STUB_CAPACITY_WORD] & 0x000FFFFF) | ((min(log_capacity, 2047) & 0xFFF) << 20)
            else:
                stub = list(OUTPUT_STUB)                # cmp w10, #capacity: imm12
                stub[STUB_CAPACITY_WORD] = (stub[STUB_CAPACITY_WORD] & ~(0xFFF << 10)) | ((log_capacity & 0xFFF) << 10)
            if stub is not None:
                code += b"".join(w.to_bytes(4, "little") for w in stub)
        # data layout
        place = {}
        image = bytearray()
        for key, content, align in self.data_secs:
            while (data_offset + len(image)) % align: image += b"\0"
            place[key] = data_offset + len(image)
            image += content
        while len(image) % 4: image += b"\0"

        def addr(tgt, addend):
            if tgt[0] == "code":
                return CODE_BASE + tgt[1] + addend
            if tgt[0] == "data":
                return DATA_BASE + place[tgt[1]] + tgt[2] + addend
            if tgt[0] == "macho_section_va":
                return None
            name = tgt[1]
            if name in self.syms:
                sy = self.syms[name]
                if sy[0] == "code":
                    return CODE_BASE + sy[1] + addend
                return DATA_BASE + place[sy[1]] + sy[2] + addend
            if stub_base is not None and name in OUTPUT_FUNCS:
                k = "printf@apple" if (self.apple and name == "printf") else name
                return CODE_BASE + stub_base + (STUB_ENTRY_X86[k] if x86 else 4 * STUB_ENTRY[k]) + addend
            return None

        unresolved = []
        if riscv:
            self._relocate_riscv(code, addr, unresolved)
        for off, t, tgt, addend in (self.relocs if x86 else []):
            S = addr(tgt, addend)                      # S already includes the addend
            if S is None or t not in ("PLT32", "PC32", "32", "32S", "64"):
                unresolved.append(f"code+{off}: {t} {tgt[1]}")
            elif t == "64":
                code[off:off + 8] = (S & (1 << 64) - 1).to_bytes(8, "little")
            else:
                _put(code, off, S - (CODE_BASE + off) if t in ("PLT32", "PC32") else S)
        for off, t, tgt, addend in ([] if riscv or x86 else self.relocs):
            S = addr(tgt, addend)
            if S is None:
                unresolved.append(f"code+{off}: {t} {tgt[1]}")
                continue
            P = CODE_BASE + off
            w = _get(code, off)
            if t in ("CALL26", "JUMP26", "BRANCH26"):
                _put(code, off, (w & 0xFC000000) | (((S - P) >> 2) & 0x3FFFFFF))
            elif t in ("ADR_PREL_PG_HI21", "PAGE21"):
                imm = ((S & ~0xFFF) - (P & ~0xFFF)) >> 12
                _put(code, off, (w & 0x9F00001F) | ((imm & 3) << 29) | (((imm >> 2) & 0x7FFFF) << 5))
            elif t in ("ADD_ABS_LO12_NC", "PAGEOFF12", "LDST8_ABS_LO12_NC", "LDST16_ABS_LO12_NC",
                       "LDST32_ABS_LO12_NC", "LDST64_ABS_LO12_NC", "LDST128_ABS_LO12_NC"):
                lo = S & 0xFFF
                if (w & 0x3B000000) == 0x39000000:          # load/store unsigned offset: scaled imm12
                    scale = (w >> 30) & 3
                    if (w & 0x04800000) == 0x04800000: scale = 4   # 128-bit SIMD
                    lo >>= scale
                _put(code, off, (w & ~(0xFFF << 10)) | ((lo & 0xFFF) << 10))
            else:
                unresolved.append(f"code+{off}: {t} {tgt[1]}")
        for key, off, t, tgt, addend in self.data_relocs:
            S = addr(tgt, addend)
            if S is None or t not in ("ABS64", "UNSIGNED", "64", "32"):
                unresolved.append(f"{key}+{off}: {t} {tgt[1]}")
                continue
            o = place[key] - data_offset + off
            nb = 4 if t == "32" else 8
            image[o:o + nb] = S.to_bytes(nb, "little")
        funcs = dict(self.funcs)
        if not funcs:
            raise LinkError("no function symbols found in the code section")
        if entry is None:
            entry = "main" if "main" in funcs else min(funcs, key=funcs.get)
        if entry not in funcs:
            raise LinkError(f"function '{entry}' not found; available: {', '.join(sorted(funcs, key=funcs.get))}")
        words = list(code) if x86 else [_get(code, i) for i in range(0, len(code), 4)]   # program-memory slots
        functions = {n: o // unit for n, o in sorted(funcs.items(), key=lambda t: t[1])}
        if stub_base is not None:
            functions["<output stub>"] = stub_base // unit
        return dict(words=words, entry=entry, entry_index=funcs[entry] // unit, functions=functions,
                    data_image=bytes(image), data_offset=data_offset, data_end=data_offset + len(image),
                    externs=externs, uses_output=uses_output, unresolved=unresolved,
                    sections={k: place[k] for k in place}, apple=self.apple, arch=self.arch)

    def _relocate_riscv(self, code, addr, unresolved):
        """Apply the RISC-V relocations to the code image."""
        def put_u(off, hi):                                   # U-type: imm[31:12]
            _put(code, off, (_get(code, off) & 0xFFF) | ((hi & 0xFFFFF) << 12))

        def put_i(off, lo):                                   # I-type: imm[11:0]
            _put(code, off, (_get(code, off) & 0x000FFFFF) | ((lo & 0xFFF) << 20))

        def put_s(off, lo):                                   # S-type: imm[11:5|4:0]
            _put(code, off, (_get(code, off) & 0x01FFF07F) | (((lo >> 5) & 0x7F) << 25) | ((lo & 0x1F) << 7))

        def put_b(off, d):                                    # B-type: imm[12|10:5|4:1|11]
            _put(code, off, (_get(code, off) & 0x01FFF07F) | (((d >> 12) & 1) << 31) | (((d >> 5) & 0x3F) << 25)
                 | (((d >> 1) & 0xF) << 8) | (((d >> 11) & 1) << 7))

        def put_j(off, d):                                    # J-type: imm[20|10:1|11|19:12]
            _put(code, off, (_get(code, off) & 0xFFF) | (((d >> 20) & 1) << 31) | (((d >> 1) & 0x3FF) << 21)
                 | (((d >> 11) & 1) << 20) | (((d >> 12) & 0xFF) << 12))
        hi_delta, later = {}, []          # code offset of an auipc with PCREL_HI20 -> target - PC
        for off, t, tgt, addend in self.relocs:
            if t in ("PCREL_LO12_I", "PCREL_LO12_S"):
                later.append((off, t, tgt))
                continue
            S = addr(tgt, addend)
            if S is None:
                unresolved.append(f"code+{off}: {t} {tgt[1]}")
                continue
            P = CODE_BASE + off
            if t in ("CALL", "CALL_PLT"):                     # auipc ra, hi ; jalr ra, lo(ra)
                hi = (S - P + 0x800) >> 12
                put_u(off, hi); put_i(off + 4, S - P - (hi << 12))
            elif t == "JAL":
                put_j(off, S - P)
            elif t == "BRANCH":
                put_b(off, S - P)
            elif t == "HI20":
                put_u(off, (S + 0x800) >> 12)
            elif t == "LO12_I":
                put_i(off, S)
            elif t == "LO12_S":
                put_s(off, S)
            elif t == "PCREL_HI20":
                hi_delta[off] = S - P
                put_u(off, (S - P + 0x800) >> 12)
            else:
                unresolved.append(f"code+{off}: {t} {tgt[1]}")
        for off, t, tgt in later:                             # the symbol is the label of the matching auipc
            A = addr(tgt, 0)
            d = hi_delta.get(A - CODE_BASE) if A is not None else None
            if d is None:
                unresolved.append(f"code+{off}: {t} {tgt[1]}")
                continue
            lo = d - (((d + 0x800) >> 12) << 12)
            (put_i if t.endswith("_I") else put_s)(off, lo)


def load_program(path, entry=None):
    """Convenience: link with data at offset 0 and no output log."""
    return ObjectImage(path).link(entry)
