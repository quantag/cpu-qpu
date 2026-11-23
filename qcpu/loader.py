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


def _sext(v, bits):
    return v - (1 << bits) if v >> (bits - 1) & 1 else v


def _xreg(name):
    """x5 / w5 -> x5; other registers unchanged."""
    return "x" + name[1:] if name[:1] in "wx" and name[1:].isdigit() else name


class ObjectImage:
    """Sections, symbols and relocations of one object file, before layout."""

    def __init__(self, path, arch=None, roots=None):
        """arch: architecture of a raw binary; an ELF or Mach-O file carries its own.
        roots: for an ELF object compiled with -ffunction-sections -fdata-sections, keep only the sections
        reachable from these functions (the code of __qcpu__ functions without main(), I/O and the rest)."""
        self.path = path
        self.roots = set(roots) if roots else None
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
        self.adrp_groups = []      # (adrp offset, user offset): must stay on one page after layout
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
        keep = self._reachable(b, secs) if (self.roots and rel) else None
        for i, s in enumerate(secs):
            if not s.has(A) or s.size == 0 or (keep is not None and i not in keep):
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

    def _reachable(self, b, secs):
        """Section indices reachable from the root functions through relocations."""
        index = {(s.name, s.offset): i for i, s in enumerate(secs)}
        defined = {}
        for sym in b.symbols:
            if sym.name and sym.shndx and sym.shndx < len(secs):
                defined.setdefault(sym.name, sym.shndx)
        missing = sorted(r for r in self.roots if r not in defined)
        if missing:
            raise LinkError("function(s) not defined in the object: " + ", ".join(missing))
        edges = {}
        for r in b.relocations:
            sec = getattr(r, "section", None)
            if sec is None:
                continue
            src = index.get((sec.name, sec.offset))
            sym = r.symbol
            dst = sym.shndx if (sym.shndx and sym.shndx < len(secs)) else defined.get(sym.name)
            if src is not None and dst:
                edges.setdefault(src, set()).add(dst)
        todo = [defined[r] for r in self.roots]
        seen = set()
        while todo:
            i = todo.pop()
            if i in seen:
                continue
            seen.add(i)
            todo += edges.get(i, ())
        return seen

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
        if b.header.file_type == lief.MachO.Header.FILE_TYPE.EXECUTE:
            return self._macho_exe(b)
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

    # ---------------------------------------------------------- Mach-O executable
    def _macho_exe(self, b):
        """A linked macOS arm64 executable (as built on an Apple Silicon Mac).

        A linked file has no relocations left, so they are reconstructed: PC-relative references from the
        code to data (adrp + add/ldr/str with :lo12:, and adr, which the linker makes from adrp + add) become
        PAGE21/PAGEOFF12 relocations, calls into the __stubs section become calls to the imported function
        (printf, puts, putchar: the output stub; exit: halt), and the pointers dyld would rebase in data
        (rebase opcodes or chained fixups) become pointer relocations. Then the program is linked for the
        machine's address space like an object file."""
        from capstone import Cs, CS_ARCH_ARM64, CS_MODE_ARM
        md = Cs(CS_ARCH_ARM64, CS_MODE_ARM)
        md.detail = True
        secs = {(s.segment_name, s.name): s for s in b.sections}
        text = secs.get(("__TEXT", "__text"))
        if text is None:
            raise LinkError("Mach-O executable without a __TEXT,__text section")
        t0 = text.virtual_address
        self.code = bytearray(text.content)
        data_names = ("__cstring", "__const", "__data", "__bss", "__common", "__got", "__literal4", "__literal8",
                      "__literal16")
        place = [(t0, t0 + text.size, "code", 0)]
        for s in b.sections:
            if s.segment_name == "__TEXT" and s.name != "__cstring" and s.name != "__const" and not s.name.startswith("__literal"):
                continue
            if s.name not in data_names or s.size == 0:
                continue
            key = s.segment_name + "," + s.name
            content = bytes(s.content) if s.name not in ("__bss", "__common") else bytes(s.size)
            self.data_secs.append((key, content, max(4, 1 << s.alignment)))
            place.append((s.virtual_address, s.virtual_address + s.size, "data", key))

        def target(va):
            for inside in (True, False):           # a section containing va; else one ending at va
                for lo, hi, kind, where in place:
                    if (lo <= va < hi) if inside else (va == hi and kind == "data"):
                        return ("code", va - lo) if kind == "code" else ("data", where, va - lo)
            return None
        # imported functions: GOT / lazy pointer slot -> name, then stub address -> name
        slot = {x.address: (x.symbol.name[1:] if x.symbol.name.startswith("_") else x.symbol.name)
                for x in b.bindings if x.has_symbol}
        stubs = {}
        st = secs.get(("__TEXT", "__stubs"))
        if st is not None:
            raw = bytes(st.content)
            size = 12
            for o in range(0, len(raw) - size + 1, size):
                w0, w1 = (int.from_bytes(raw[o + 4 * k:o + 4 * k + 4], "little") for k in (0, 1))
                if (w0 & 0x9F000000) != 0x90000000:
                    continue
                page = ((st.virtual_address + o) & ~0xFFF) + (_sext(((w0 >> 3) & 0x1FFFFC) | ((w0 >> 29) & 3), 21) << 12)
                off = ((w1 >> 10) & 0xFFF) << ((w1 >> 30) & 3)
                stubs[st.virtual_address + o] = slot.get(page + off, "?")
        # symbols
        for s in b.symbols:
            if (s.raw_type & 0x0E) != 0x0E or not s.name:
                continue
            loc = target(s.value)
            if not loc:
                continue
            name = s.name[1:] if s.name.startswith("_") else s.name
            if loc[0] == "code":
                self.syms[name] = loc
                self.funcs[name] = loc[1]
            else:
                self.syms[name] = loc
        if b.has_main_command and "main" not in self.funcs:
            off = b.main_command.entrypoint - (t0 - b.imagebase if hasattr(b, "imagebase") else 0)
            loc = target(b.imagebase + b.main_command.entrypoint) if hasattr(b, "imagebase") else None
            if loc and loc[0] == "code":
                self.funcs["main"] = loc[1]
                self.syms["main"] = loc
        # code: calls into __stubs, and PC-relative references
        code = bytes(self.code)
        insns = {}
        for k in range(0, len(code), 4):
            got = list(md.disasm(code[k:k + 4], t0 + k))
            insns[k] = got[0] if got else None
        word = lambda k: int.from_bytes(code[k:k + 4], "little")

        def writes(ins, reg):
            if ins is None:
                return False
            try:
                _, wr = ins.regs_access()
            except Exception:
                return True
            return any(_xreg(ins.reg_name(r)) == reg for r in wr)

        def reads_base(ins, reg):
            return ins is not None and any(op.type == 3 and op.mem.base and _xreg(ins.reg_name(op.mem.base)) == reg
                                           for op in ins.operands)
        bounds = sorted(set(self.funcs.values()) | {len(code)})
        fn_end = lambda k: next(x for x in bounds if x > k)
        NOP = 0xD503201F
        for k in range(0, len(code), 4):
            w = word(k)
            P = t0 + k
            if (w & 0x7C000000) == 0x14000000:                      # b / bl
                T = P + (_sext(w & 0x3FFFFFF, 26) << 2)
                if T in stubs:
                    self.relocs.append((k, "CALL26", ("sym", stubs[T]), 0))
                elif not (t0 <= T < t0 + len(code)):
                    raise LinkError(f"branch at 0x{P:x} leaves the code section (0x{T:x})")
                continue
            if (w & 0xBF000000) in (0x18000000, 0x98000000) and k >= 4 and word(k - 4) == NOP:
                # ldr Xt/Wt/ldrsw, literal: the linker's relaxation of adrp + ldr; undo it
                S = P + (_sext((w >> 5) & 0x7FFFF, 19) << 2)
                tgt = target(S)
                if tgt is None:
                    raise LinkError(f"literal load at 0x{P:x} refers outside the program's sections")
                rt = w & 31
                if (w & 0xFF000000) == 0x58000000:
                    ld = 0xF9400000                                     # ldr Xt, [Xt, #lo12]
                elif (w & 0xFF000000) == 0x18000000:
                    ld = 0xB9400000                                     # ldr Wt, [Xt, #lo12]
                else:
                    ld = 0xB9800000                                     # ldrsw Xt, [Xt, #lo12]
                self.code[k - 4:k] = (0x90000000 | rt).to_bytes(4, "little")
                self.code[k:k + 4] = (ld | (rt << 5) | rt).to_bytes(4, "little")
                self.relocs.append((k - 4, "PAGE21", tgt, 0))
                self.relocs.append((k, "PAGEOFF12", tgt, 0))
                continue
            is_adrp = (w & 0x9F000000) == 0x90000000
            is_adr = (w & 0x9F000000) == 0x10000000
            if not (is_adr or is_adrp):
                continue
            imm = _sext(((w >> 3) & 0x1FFFFC) | ((w >> 29) & 3), 21)
            rd = w & 31
            if is_adr:
                S = P + imm
                tgt = target(S)
                if tgt is None:
                    raise LinkError(f"adr at 0x{P:x} refers to 0x{S:x}, outside the program's sections")
                if k + 4 < len(code) and word(k + 4) == NOP:            # adrp + add relaxed by the linker: undo
                    self.code[k + 4:k + 8] = (0x91000000 | (rd << 5) | rd).to_bytes(4, "little")
                    self.code[k:k + 4] = (0x90000000 | rd).to_bytes(4, "little")
                    self.relocs.append((k, "PAGE21", tgt, 0))
                    self.relocs.append((k + 4, "PAGEOFF12", tgt, 0))
                else:
                    self.relocs.append((k, "ADR21", tgt, 0))
                continue
            page = (P & ~0xFFF) + (imm << 12)
            reg = "x%d" % rd
            users = []
            for j in range(k + 4, fn_end(k), 4):
                ins = insns[j]
                uw = word(j)
                if (uw & 0x7FC00000) == 0x11000000 and ((uw >> 5) & 31) == rd:          # add Rd, Rn, #lo12
                    users.append((j, (uw >> 10) & 0xFFF))
                elif (uw & 0x3B000000) == 0x39000000 and ((uw >> 5) & 31) == rd:        # ldr/str [Rn, #lo12]
                    scale = (uw >> 30) & 3
                    if (uw & 0x04800000) == 0x04800000:
                        scale = 4
                    users.append((j, ((uw >> 10) & 0xFFF) << scale))
                elif reads_base(ins, reg):
                    raise LinkError(f"adrp at 0x{P:x}: the page address is used at 0x{t0 + j:x} in a form the "
                                    "loader cannot relocate")
                if writes(ins, reg):
                    break
            if not users:
                raise LinkError(f"adrp at 0x{P:x}: no use of the page address found")
            tg = [target(page + lo) for _, lo in users]
            if any(t is None for t in tg):
                raise LinkError(f"adrp at 0x{P:x} refers outside the program's sections")
            self.relocs.append((k, "PAGE21", tg[0], 0))
            for (j, _), t in zip(users, tg):
                self.relocs.append((j, "PAGEOFF12", t, 0))
                self.adrp_groups.append((k, j))
        # data imports: the stack-protector guard becomes a zero word in data memory
        for va, name in slot.items():
            loc = target(va)
            if name == "__stack_chk_guard" and loc and loc[0] == "data":
                if not any(k == "guard" for k, _, _ in self.data_secs):
                    self.data_secs.append(("guard", bytes(8), 8))
                self.data_relocs.append((loc[1], loc[2], "UNSIGNED", ("data", "guard", 0), 0))
        # pointers in data that dyld would rebase
        for r in b.relocations:
            loc = target(r.address)
            if not loc or loc[0] != "data":
                continue
            key, off = loc[1], loc[2]
            if type(r).__name__ == "RelocationFixup":           # chained fixups: lief decodes the target
                val = r.target
            else:
                content = next(c for kk, c, _ in self.data_secs if kk == key)
                val = int.from_bytes(content[off:off + 8], "little")
            dst = target(val)
            if r.address in slot:
                continue                                   # an import slot, not a pointer into the program
            if dst is None:
                raise LinkError(f"pointer at 0x{r.address:x} refers outside the program (0x{val:x})")
            self.data_relocs.append((key, off, "UNSIGNED", dst, 0))

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
        exit_at = None
        HALTS = ("exit", "_exit", "abort", "__stack_chk_fail")
        if any(e in HALTS for e in externs) and not x86 and not self.arch.startswith("riscv"):
            exit_at = len(code)                         # exit(status), abort(), __stack_chk_fail(): halt
            code += (0xD4400000).to_bytes(4, "little")  # (exit's status stays in w0)
            externs = [e for e in externs if e not in HALTS]
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
            if name in ("exit", "_exit", "abort", "__stack_chk_fail") and exit_at is not None:
                return CODE_BASE + exit_at + addend
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
            elif t == "ADR21":
                d = S - P
                if not -(1 << 20) <= d < (1 << 20):
                    unresolved.append(f"code+{off}: adr out of range for the machine's layout")
                    continue
                _put(code, off, (w & 0x9F00001F) | ((d & 3) << 29) | (((d >> 2) & 0x7FFFF) << 5))
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
        if self.adrp_groups:                     # reconstructed adrp users must share the adrp's page
            S_of = {off: addr(tgt, addend) for off, t, tgt, addend in self.relocs if t in ("PAGE21", "PAGEOFF12")}
            for k, j in self.adrp_groups:
                if S_of.get(k) is not None and S_of.get(j) is not None and (S_of[k] >> 12) != (S_of[j] >> 12):
                    unresolved.append(f"code+{j}: data referenced through one adrp lands on two pages")
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
