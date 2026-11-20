"""Instruction-set registry: what the tools need to know about one architecture.

Each entry bundles the quantum front end (Machine + configuration), the classical side (class table,
static check, disassembler), the CPU reference, the calling convention and the compiler settings.
The command-line tool, the loader and the tests work through this interface only, so adding an
architecture means adding one entry here.

    isa = get("riscv64")            # or isa.detect("prog.o") -> name
    m = isa.Machine(isa.config(W=12, used={10, 11, 1}, SLOTS=64, STEPS=40, MEM=16))

What differs between architectures and is therefore part of the interface:
  unit        bytes per program-memory slot (4 for fixed-length instruction words, 1 for x86 bytes)
  listing     code image -> one entry per instruction (slot index, bytes, assembly text, static check)
  entry       initial state for "call the entry function": return address in a link register or on the
              stack, stack pointer
  always_mem  True if every run needs data memory (x86-64: the return address is on the stack)
  min_slots   smallest program memory the front end accepts
"""
import platform
from types import SimpleNamespace

NAMES = ("arm64", "riscv64", "riscv32", "x86_64")
ALIASES = {"aarch64": "arm64", "arm": "arm64", "riscv": "riscv64", "rv64": "riscv64", "rv32": "riscv32",
           "x86-64": "x86_64", "x86": "x86_64", "amd64": "x86_64", "x64": "x86_64"}
_cache = {}


def _words(check, disassemble, link, sp):
    """Listing and entry state for an instruction set with 32-bit instruction words and a link register."""
    def listing(code, slots, base=0):
        asm = disassemble(b"".join(w.to_bytes(4, "little") for w in code), base)
        return [dict(index=i, size=1, word=f"{w:08x}", asm=asm[i], check=check(w, i, slots)) for i, w in enumerate(code)]

    def entry(init, cells, slots, sp_off, has_sp):
        init = dict(init)
        init[link] = 4 * (slots - 1)                 # the entry function returns to the halt slot
        if has_sp:
            init[sp] = sp_off
        return init, cells
    return dict(unit=4, min_slots=2, listing=listing, entry=entry, always_mem=False, entry_regs=(link,),
                image=lambda code: b"".join(w.to_bytes(4, "little") for w in code))


def _arm64():
    from . import a64, machine, reference

    def config(W, used, SLOTS, STEPS, MEM, classes=None, registers="auto"):
        used = set(used)
        regs = tuple(range(31)) if registers == "all" else tuple(sorted(used - {31})) if registers == "auto" \
            else tuple(sorted(set(registers) | {30}))
        return machine.Config(W=W, regs=regs, sp=31 in used, SLOTS=SLOTS, STEPS=STEPS, MEM=MEM,
                              classes=classes or a64.ALL_CLASSES)
    return SimpleNamespace(
        name="arm64", title="AArch64", xlen=64, Machine=machine.QuantumARM, config=config,
        ALL_CLASSES=a64.ALL_CLASSES, MEMOPS=a64.MEMOPS, check=a64.check, disassemble=a64.disassemble,
        layout=reference.layout, run_reference=reference.run_reference,
        arg_regs=tuple(range(8)), ret_reg=0, link_reg=30, sp_reg=31,
        ret_label="X0", halt_class="hlt", flag_names="NZCV",
        reg_name=lambda k: "sp" if k == 31 else f"x{k}",
        clang=["--target=aarch64-linux-gnu"], gcc_flags=[],
        gcc=["aarch64-linux-gnu-gcc", "aarch64-none-linux-gnu-gcc", "aarch64-none-elf-gcc", "aarch64-elf-gcc"],
        **_words(a64.check, a64.disassemble, 30, 31))


def _riscv(xlen):
    from . import rv, rv_machine, rv_reference
    ALL = tuple(rv.classes(xlen))

    def config(W, used, SLOTS, STEPS, MEM, classes=None, registers="auto"):
        regs = tuple(range(1, 32)) if registers == "all" else tuple(sorted(set(used) - {0})) if registers == "auto" \
            else tuple(sorted(set(registers) | {1}))
        return rv_machine.RVConfig(W=W, regs=regs, SLOTS=SLOTS, STEPS=STEPS, MEM=MEM, classes=classes or ALL, xlen=xlen)
    # no compressed instructions, no linker relaxation, absolute addressing (no GOT)
    flags = [f"-march=rv{xlen}im", "-mabi=lp64" if xlen == 64 else "-mabi=ilp32", "-mno-relax", "-fno-pic"]
    check = lambda w, i, n, enabled=None: rv.check(w, i, n, xlen, enabled)
    disassemble = lambda code, base=0: rv.disassemble(code, base, xlen)
    return SimpleNamespace(
        name=f"riscv{xlen}", title=f"RISC-V RV{xlen}IM", xlen=xlen, Machine=rv_machine.QuantumRISCV, config=config,
        ALL_CLASSES=ALL, MEMOPS=rv.MEMOPS, check=check, disassemble=disassemble,
        layout=rv_reference.layout,
        run_reference=lambda *a, **k: rv_reference.run_reference(*a, xlen=xlen, **k),
        arg_regs=tuple(range(10, 18)), ret_reg=10, link_reg=1, sp_reg=2,
        ret_label="a0", halt_class="ebreak", flag_names="",
        reg_name=lambda k: rv.ABI[k],
        clang=[f"--target=riscv{xlen}-unknown-{'linux-gnu' if xlen == 64 else 'elf'}"] + flags, gcc_flags=flags,
        gcc=["riscv64-linux-gnu-gcc", "riscv64-unknown-elf-gcc", "riscv64-elf-gcc"] if xlen == 64 else
            ["riscv64-unknown-elf-gcc", "riscv32-unknown-elf-gcc", "riscv64-elf-gcc"],
        **_words(check, disassemble, 1, 2))


def _x86_64():
    from . import x86, x86_machine, x86_reference

    def config(W, used, SLOTS, STEPS, MEM, classes=None, registers="auto"):
        regs = tuple(range(16)) if registers == "all" else tuple(sorted(used)) if registers == "auto" else tuple(registers)
        return x86_machine.X86Config(W=W, regs=regs, SLOTS=SLOTS, STEPS=STEPS, MEM=MEM, classes=classes or x86.ALL_CLASSES)

    def entry(init, cells, slots, sp_off, has_sp):
        cells, rsp = x86_reference.entry_cells(cells, sp_off, slots)      # return address on the stack
        init = dict(init)
        init[x86.RSP] = rsp
        return init, cells
    # integer code only, absolute addressing; one section per function and no loop alignment, so that the
    # code contains no padding (assemblers pad with prefixed multi-byte NOPs, which the machine does not implement)
    flags = ["-fno-pic", "-mno-sse", "-fno-asynchronous-unwind-tables", "-ffunction-sections", "-falign-loops=1"]
    native = ["gcc"] if platform.machine().lower() in ("x86_64", "amd64") else []
    return SimpleNamespace(
        name="x86_64", title="x86-64", xlen=64, Machine=x86_machine.QuantumX86, config=config,
        ALL_CLASSES=x86.ALL_CLASSES, MEMOPS=x86.MEMOPS, check=None, disassemble=None,
        layout=x86_reference.layout, run_reference=x86_reference.run_reference,
        arg_regs=x86_reference.ARGS, ret_reg=x86.RAX, link_reg=None, sp_reg=x86.RSP,
        ret_label="RAX", halt_class="hlt", flag_names="ZF SF CF OF",
        reg_name=lambda k: x86.REG[k],
        clang=["--target=x86_64-linux-gnu"] + flags,
        gcc_flags=flags + ["-falign-functions=1", "-falign-jumps=1", "-falign-labels=1", "-fcf-protection=none"],
        gcc=["x86_64-linux-gnu-gcc"] + native,
        unit=1, min_slots=16, listing=x86.listing, entry=entry, always_mem=True, entry_regs=(x86.RSP,), image=bytes)


def get(name):
    name = ALIASES.get(name, name)
    if name not in _cache:
        if name == "arm64":
            _cache[name] = _arm64()
        elif name in ("riscv64", "riscv32"):
            _cache[name] = _riscv(int(name[5:]))
        elif name == "x86_64":
            _cache[name] = _x86_64()
        else:
            raise ValueError(f"unknown architecture '{name}' (available: {', '.join(NAMES)})")
    return _cache[name]


def detect(path):
    """Architecture name from the header of an ELF or Mach-O file; None for a raw binary."""
    with open(path, "rb") as fh:
        d = fh.read(20)
    if d[:4] == b"\x7fELF":
        mach = int.from_bytes(d[18:20], "little")
        if mach == 183:
            return "arm64"
        if mach == 243:
            return "riscv64" if d[4] == 2 else "riscv32"
        if mach == 62:
            return "x86_64"
        if mach == 3:
            raise ValueError("32-bit x86 binaries are not supported (x86-64 is: compile with -m64)")
        raise ValueError(f"ELF machine type {mach} is not supported (supported: AArch64, RISC-V, x86-64)")
    if d[:4] in (b"\xcf\xfa\xed\xfe", b"\xca\xfe\xba\xbe", b"\xbe\xba\xfe\xca"):
        return "arm64"
    return None
