"""Instruction-set registry: what the tools need to know about one architecture.

Each entry bundles the quantum front end (Machine + configuration), the classical side (class table,
static check, disassembler), the CPU reference, the calling convention and the compiler settings.
The command-line tool, the loader and the tests work through this interface only, so adding an
architecture means adding one entry here.

    isa = get("riscv64")            # or isa.detect("prog.o") -> name
    m = isa.Machine(isa.config(W=12, used={10, 11, 1}, SLOTS=64, STEPS=40, MEM=16))
"""
from types import SimpleNamespace

NAMES = ("arm64", "riscv64", "riscv32")
ALIASES = {"aarch64": "arm64", "arm": "arm64", "riscv": "riscv64", "rv64": "riscv64", "rv32": "riscv32"}
_cache = {}


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
        ret_label="X0", halt_class="hlt",
        reg_name=lambda k: "sp" if k == 31 else f"x{k}",
        clang=["--target=aarch64-linux-gnu"], gcc_flags=[],
        gcc=["aarch64-linux-gnu-gcc", "aarch64-none-linux-gnu-gcc", "aarch64-none-elf-gcc", "aarch64-elf-gcc"])


def _riscv(xlen):
    from . import rv, rv_machine, rv_reference
    ALL = tuple(rv.classes(xlen))

    def config(W, used, SLOTS, STEPS, MEM, classes=None, registers="auto"):
        regs = tuple(range(1, 32)) if registers == "all" else tuple(sorted(set(used) - {0})) if registers == "auto" \
            else tuple(sorted(set(registers) | {1}))
        return rv_machine.RVConfig(W=W, regs=regs, SLOTS=SLOTS, STEPS=STEPS, MEM=MEM, classes=classes or ALL, xlen=xlen)
    # no compressed instructions, no linker relaxation, absolute addressing (no GOT)
    flags = [f"-march=rv{xlen}im", "-mabi=lp64" if xlen == 64 else "-mabi=ilp32", "-mno-relax", "-fno-pic"]
    return SimpleNamespace(
        name=f"riscv{xlen}", title=f"RISC-V RV{xlen}IM", xlen=xlen, Machine=rv_machine.QuantumRISCV, config=config,
        ALL_CLASSES=ALL, MEMOPS=rv.MEMOPS,
        check=lambda w, i, n, enabled=None: rv.check(w, i, n, xlen, enabled),
        disassemble=lambda code, base=0: rv.disassemble(code, base, xlen),
        layout=rv_reference.layout,
        run_reference=lambda *a, **k: rv_reference.run_reference(*a, xlen=xlen, **k),
        arg_regs=tuple(range(10, 18)), ret_reg=10, link_reg=1, sp_reg=2,
        ret_label="a0", halt_class="ebreak",
        reg_name=lambda k: rv.ABI[k],
        clang=[f"--target=riscv{xlen}-unknown-{'linux-gnu' if xlen == 64 else 'elf'}"] + flags, gcc_flags=flags,
        gcc=["riscv64-linux-gnu-gcc", "riscv64-unknown-elf-gcc", "riscv64-elf-gcc"] if xlen == 64 else
            ["riscv64-unknown-elf-gcc", "riscv32-unknown-elf-gcc", "riscv64-elf-gcc"])


def get(name):
    name = ALIASES.get(name, name)
    if name not in _cache:
        if name == "arm64":
            _cache[name] = _arm64()
        elif name in ("riscv64", "riscv32"):
            _cache[name] = _riscv(int(name[5:]))
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
        if mach in (62, 3):
            raise ValueError("x86 binaries are not supported yet (planned; see docs/x86_plan.md)")
        raise ValueError(f"ELF machine type {mach} is not supported (supported: AArch64, RISC-V)")
    if d[:4] in (b"\xcf\xfa\xed\xfe", b"\xca\xfe\xba\xbe", b"\xbe\xba\xfe\xca"):
        return "arm64"
    return None
