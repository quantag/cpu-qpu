"""x86-64 front end: circuit against the instruction model, and against the CPU (Unicorn) after every cycle.

  1. Decoder table: the kind patterns are mutually exclusive (exhaustive over opcode, next byte, 0F
     escape and REX.W), and every instruction of a reference list is decoded with the length the
     disassembler reports.
  2. Circuit == model on random byte strings, one cycle each: random bytes at a random PC, random
     registers, flags and memory. The circuit must do exactly what x86.decode() + x86.model() say:
     same registers, flags, memory cells and next PC for a valid instruction; trap and no change for
     anything else. This covers encodings no assembler emits (redundant prefixes, both operand
     orders, every ModRM / SIB form) and the whole space of invalid ones.
  3. Trap: a named list of unsupported instructions traps in the circuit and changes nothing.
  4. Superposition of a valid and an invalid instruction: the valid branch computes, the other traps.
  5. Boundary cases: every implemented instruction on operands at the edges of the W-bit model,
     against the CPU. Never an unflagged mismatch; equal to the CPU wherever the operands and the
     results are representable; when the circuit equals the CPU the model check must not claim otherwise.
  6. Differential fuzzing with random programs against the CPU.
  7. The output stub in qcpu/loader.py equals the assembled docs/output_stub_x86.s.
Run: python3 -m tests.test_x86 [N programs per width] [seed] [widths, e.g. 8,12,16]"""
import os, random, subprocess, sys, tempfile
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
from qcpu import isa as isas, x86
from qcpu.bitsim import value
from qcpu.loader import OUTPUT_STUB_X86
from qcpu.sparsesim import SparseState
from qcpu.x86_machine import X86Config, QuantumX86
from tests.common import assemble, run_case

ISA = isas.get("x86_64")
INTEL = [".intel_syntax noprefix"]
CC = ["o", "no", "b", "ae", "e", "ne", "be", "a", "s", "ns", "l", "ge", "le", "g"]
_asm = {}


def asm(lines):
    key = tuple(lines)
    if key not in _asm:
        _asm[key] = assemble(INTEL + list(lines), ISA)
    return _asm[key]


_machines = {}


def machine(W, steps=48, slots=128, mem=32):
    """One cycle template per configuration. The simulator replays the template STEPS times, so the
    tests use a machine with just enough cycles for each program."""
    key = (W, steps, slots, mem)
    if key not in _machines:
        _machines[key] = QuantumX86(X86Config(W=W, SLOTS=slots, STEPS=steps, MEM=mem), defer=True)
    return _machines[key]


# ------------------------------------------------------------------ 1. decoder table
REFERENCE = """add eax, esi|add rax, rsi|add al, sil|or ecx, [rdi + 4]|and [rsp + 8], edx|sub r8, r9|xor eax, eax|cmp r10d, r11d
add eax, 1000|sub ecx, 1000|and rdx, -8|cmp ebx, 127|or al, 3|xor cl, 5|add dword ptr [rax + 4*rcx + 8], 1|cmp qword ptr [rbp - 16], 0
test eax, eax|test al, 1|test ecx, 255|test rdi, rdi|test eax, 0x1000|mov eax, edi|mov rax, [rdi]|mov [rdi + rsi*4], eax|mov al, cl
mov dword ptr [rsp], 5|mov qword ptr [rbp - 8], -1|mov eax, 5|mov r10, 0x123456789|mov cl, 7|mov rax, -1|lea rax, [rip + 16]
lea ecx, [rdi + 2*rsi - 3]|lea rdx, [4*rcx]|movsxd rax, edi|movzx eax, dil|movsx ecx, al|movsx rdx, cl|not eax|neg rcx|div ecx|idiv rsi
inc eax|dec qword ptr [rdi]|call rax|jmp rcx|push qword ptr [rdi + 8]|imul eax, esi|imul rcx, [rdi]|imul edx, ecx, 10|imul rax, rdi, 1000
cdq|cqo|cdqe|shl eax, 3|shr rcx, 1|sar edx, cl|shl dword ptr [rdi], 2|sete al|setl r8b|cmovg eax, esi|cmovb rcx, [rdi]|push rbp|pop r12
push 5|push 1000|leave|ret|nop|nop dword ptr [rax]|nop dword ptr [rax + rax + 0]|hlt|mov eax, [0x10000040]|mov [r12], eax|mov [r13 + 0], ecx
mov eax, [rsp + 4]|mov rax, [rbp]|mov ecx, [r9 + 4*r8 + 100000]""".replace("\n", "|").split("|")


def decoder_table():
    for e in (0, 1):
        for w in (0, 1):
            for key in range(1 << 16):
                hits = [n for n, ps in x86.KINDS.items() if any(ee == e and key & m == v and (q is None or q == w) for ee, m, v, q in ps)]
                assert len(hits) <= 1, (e, w, hex(key), hits)
    from capstone import Cs, CS_ARCH_X86, CS_MODE_64
    md = Cs(CS_ARCH_X86, CS_MODE_64)
    code = bytes(asm(REFERENCE + ["1:", "jne 1b", "jmp 1b", "call 1b", "jl 1b + 1000", "jmp 1b + 1000"]))
    i = n = 0
    kinds = set()
    while i < len(code):
        d = x86.decode(code[i:i + 15])
        ins = next(md.disasm(code[i:i + 15], i, 1))
        assert d["ok"], (ins.mnemonic, ins.op_str, d["why"])
        assert d["length"] == ins.size, (ins.mnemonic, ins.op_str, d["length"], ins.size)
        kinds.add(d["kind"])
        i += ins.size
        n += 1
    assert kinds == set(x86.KINDS), set(x86.KINDS) - kinds
    return n


# ------------------------------------------------------------------ 2. circuit == model, one cycle
def random_instruction(r):
    """15 bytes: usually a plausible instruction of a randomly chosen kind (not necessarily a valid one)."""
    if r.random() < 0.05:
        return [r.getrandbits(8) for _ in range(15)]
    e, m, v, q = r.choice(x86.KINDS[r.choice(list(x86.KINDS))])
    key = (r.getrandbits(16) & ~m | v) if r.random() < 0.95 else r.getrandbits(16)
    b = []
    if r.random() < 0.5 or q:
        rex = 0x40 | r.getrandbits(4)
        if q is not None and r.random() < 0.9:
            rex = rex & 0xF7 | q << 3
        b.append(rex)
    if e if r.random() < 0.97 else not e:
        b.append(0x0F)
    b += [key & 0xFF, key >> 8]
    b.append(r.getrandbits(8) if r.random() < 0.7 else r.choice([0x24, 0x25, 0x20, 0x65, 0x2D]))     # SIB
    b += [r.choice([0, 4, 8, 0xFC, 0xF8, r.getrandbits(8)]) for _ in range(12)]
    return b[:15]


def circuit_vs_model(W, N, seed, SLOTS=32, MEM=8):
    r = random.Random(seed)
    m = QuantumX86(X86Config(W=W, SLOTS=SLOTS, STEPS=1, MEM=MEM), defer=True)
    kinds, traps = {}, 0
    for _ in range(N):
        pc = r.randrange(SLOTS)
        prog = [r.getrandbits(8) for _ in range(SLOTS)]
        for i, v in enumerate(random_instruction(r)):
            prog[(pc + i) % SLOTS] = v
        win = [prog[(pc + i) % SLOTS] for i in range(15)]
        small = r.random() < 0.5                       # small values: addresses inside data memory, small shift counts
        regs = {k: r.choice([r.randrange(4 * MEM), r.getrandbits(W)]) if small else r.getrandbits(W) for k in range(16)}
        if r.random() < 0.3:
            regs[r.randrange(16)] = 0                  # zero divisors, zero counts
        flags = [r.getrandbits(1) for _ in range(4)]
        ram = [r.getrandbits(W) if r.random() < 0.8 else 0 for _ in range(MEM)]
        out = m.run_fast(m.load_bits(prog, regs, ram, nzcv=sum(f << i for i, f in enumerate(flags)), pc=pc))
        st = out["state"]
        got = ({k: value(st, m.X[k]) for k in range(16)}, [st[x] for x in m.nzcv], [value(st, c) for c in m.ram],
               out["trace"][-1], int(bool(out["trap"])), out["run"])
        e_regs, e_flags, e_ram, e_pc, e_trap, e_run = dict(regs), list(flags), list(ram), pc, 0, 1
        d = x86.decode(win)
        mdl = x86.model(d, W, lambda k: regs[k], flags, lambda i: ram[i % MEM], pc, SLOTS) if d["ok"] else None
        if mdl is None or mdl["trap"]:
            e_trap, e_run = 1, 0
            traps += 1
        else:
            e_regs.update(mdl["regs"])
            for i, v in mdl["flags"].items():
                e_flags[i] = v
            for c, v in mdl["stores"]:
                e_ram[c % MEM] = v
            e_pc, e_run = mdl["npc"], int(d["kind"] != "hlt")
            kinds[d["kind"]] = kinds.get(d["kind"], 0) + 1
        want = (e_regs, e_flags, e_ram, e_pc, e_trap, e_run)
        assert got == want, (f"W={W} bytes {bytes(win).hex()} ({d['kind']}) pc={pc}: " +
                             "; ".join(f"{n}: circuit {a} model {b}" for n, a, b in
                                       zip(("regs", "flags", "memory", "next pc", "trap", "running"), got, want) if a != b))
    assert set(kinds) == set(x86.KINDS), set(x86.KINDS) - set(kinds)
    return dict(valid=N - traps, trap=traps, least_tested_kind=min(kinds.values()))


# ------------------------------------------------------------------ 3./4. traps
BAD = {"adc eax, esi": "11f0", "sbb eax, esi": "19f0", "mul ecx (one operand)": "f7e1", "imul ecx (one operand)": "f7e9",
       "rol eax, 3": "c1c003", "rcr eax, 1": "d1d8", "16-bit: add ax, si": "6601f0", "rep movsb": "f3a4", "lock inc dword [rdi]": "f0ff07",
       "byte store: mov [rdi], al": "8807", "byte load: movzx eax, byte [rdi]": "0fb607", "byte op: add [rdi], al": "0007",
       "mov al, ah (high byte register)": "88e0", "mov ah, 1": "b401", "sete ah": "0f94c4", "movzx eax, bh": "0fb6c7",
       "inc al (FE /0)": "fec0", "syscall": "0f05", "int3": "cc", "cpuid": "0fa2", "pop rsp": "5c", "lea eax, eax (invalid)": "8dc0",
       "xchg eax, ecx": "87c8", "cmpxchg": "0fb1c8", "movaps xmm0, xmm1": "0f28c1", "jp (parity)": "7a00", "setp al": "0f9ac0",
       "cmovnp eax, ecx": "0f4bc1", "movsxd without REX.W": "63c0", "cwde (98 without REX.W)": "98", "push es (invalid in 64-bit)": "06",
       "bt eax, ecx": "0fa3c8", "bswap eax": "0fc8", "movzx eax, cx (16-bit source)": "0fb7c1", "mov eax, fs:[0]": "648b042500000000",
       "two REX prefixes": "484801f0", "test /1 (undefined)": "f7c800000000", "mov /1 (C7 /1, undefined)": "c7c800000000",
       "shl /6 (undefined alias)": "c1f003", "fld st(0)": "d9c0", "ud2": "0f0b", "all zero + invalid modrm: add [rax], al": "0000",
       "enter": "c8100000", "ret imm16": "c20800", "jmp far": "ea", "loop": "e2fe", "address-size prefix": "678b07"}


def traps():
    m = QuantumX86(X86Config(W=8, SLOTS=16, STEPS=3, MEM=4), defer=True)
    for name, hx in BAD.items():
        code = list(bytes.fromhex(hx))
        assert not x86.decode(code)["ok"], name
        init = {k: 3 + 5 * k for k in range(16)}
        ones = m.load_bits(code + [0] * (15 - len(code)) + [x86.HLT], init, [1, 2, 3, 4], nzcv=0b1010)
        out = m.run_fast(ones)
        st = out["state"]
        assert out["trap"] and out["run"] == 0, name                               # trapped and stopped
        assert out["trace"] == [0, 0, 0, 0], name                                  # the PC did not move
        assert all(value(st, m.X[k]) == init[k] for k in m.X), name                # no register written
        assert [value(st, c) for c in m.ram] == [1, 2, 3, 4], name                 # no memory written
        assert [st[x] for x in m.nzcv] == [0, 1, 0, 1], name                       # no flag written
    # division by zero: the CPU raises #DE; the machine traps and writes nothing
    for hx in ("f7f1", "f7f9", "48f7f9"):                                          # div ecx, idiv ecx, idiv rcx
        ones = m.load_bits(list(bytes.fromhex(hx)) + [x86.HLT], {x86.RAX: 77, x86.RDX: 5, x86.RCX: 0}, [0] * 4)
        out = m.run_fast(ones)
        assert out["trap"] and out["run"] == 0 and value(out["state"], m.X[x86.RAX]) == 77 and value(out["state"], m.X[x86.RDX]) == 5, hx
    return len(BAD) + 3


def superposed_valid_and_invalid():
    """Program byte 0 is the opcode of 'add eax, esi' (01) with bit 4 in superposition: |0> is ADD,
    |1> is ADC (11), which is not implemented. One run of the circuit does both."""
    m = QuantumX86(X86Config(W=8, regs=(x86.RAX, x86.RSI), SLOTS=16, STEPS=3, MEM=4))
    assert x86.decode([0x01, 0xF0])["ok"] and not x86.decode([0x11, 0xF0])["ok"]
    prog = ISA.layout([0x01, 0xF0, 0xC3], 16)
    init, cells = ISA.entry({x86.RAX: 20, x86.RSI: 22}, [0] * 4, 16, 16, True)
    st = SparseState(m.n, m.load_bits(prog, init, cells))
    st.h(m.mem[0][4])
    st.apply(m.g)
    got = {}
    for row, a in zip(st.bits, st.amp):
        v = lambda q: int(sum(int(row[x]) << i for i, x in enumerate(q)))
        got[int(row[m.mem[0][4]])] = (v(m.X[x86.RAX]), any(row[t] for t in m.trap), round(float(abs(a)) ** 2, 6))
    assert got == {0: (42, False, 0.5), 1: (20, True, 0.5)}, got
    return got


# ------------------------------------------------------------------ 5. boundary cases
def values(W):
    L = 1 << (W - 1)
    return sorted({0, 1, -1, 2, 7, 127, 128, 255, 256, -128, -129, L - 1, -L, L, -L - 1, 0x1F0, L // 2})


def s32(v):
    v &= 0xFFFFFFFF
    return v - (1 << 32) if v >> 31 else v


def edge_cases(W):
    """(group, lines, edi, esi, exact) -- exact: the circuit must equal the CPU after every instruction
    (given that edi and esi are representable). r9 points to data memory."""
    L = 1 << (W - 1)
    fit = lambda *vs: all(-L <= v < L for v in vs)
    vals, out = values(W), []
    few = {0, 1, -1, 2, 127, -128, L - 1, -L, L}
    add = lambda *a: out.append(a)
    for x in vals:
        for y in vals:
            add("add sub cmp", ["mov eax, edi", "add eax, esi", "mov rcx, rdi", "add rcx, rsi"], x, y, fit(x + y))
            add("add sub cmp", ["mov eax, edi", "sub eax, esi", "mov rcx, rdi", "sub rcx, rsi", "cmp edi, esi", "cmp rsi, rdi"],
                x, y, fit(x - y, y - x))
            add("logic", ["mov eax, edi", "and eax, esi", "mov ecx, edi", "or ecx, esi", "mov edx, edi", "xor edx, esi",
                          "test edi, esi", "mov r8, rdi", "and r8, rsi", "test rsi, rdi", "xor r8, r8"], x, y, True)
            add("byte operations", ["mov eax, edi", "add al, sil", "mov ecx, edi", "sub cl, sil", "mov edx, edi", "xor dl, sil",
                                    "cmp dil, sil", "test sil, dil", "mov r8d, edi", "mov r8b, sil", "or r8b, 0x55", "and dl, 0xf0",
                                    "mov bl, 0x80", "cmp al, 100"], x, y, True)
            add("multiply", ["mov eax, edi", "imul eax, esi", "imul rcx, rdi, 7", "imul edx, esi, -300", "mov r8, rsi", "imul r8, rdi"],
                x, y, True)
            if y:
                add("signed division", ["mov eax, edi", "cdq", "idiv esi", "mov ecx, eax", "mov r8d, edx", "mov rax, rdi", "cqo",
                                        "idiv rsi"], x, y, True)
                add("unsigned division", ["mov eax, edi", "xor edx, edx", "div esi", "mov ecx, eax", "mov r8d, edx", "mov rax, rdi",
                                          "xor edx, edx", "div rsi"], x, y, x >= 0 and y > 0)
            if x in few and y in few:          # the flags of every pair are compared in the groups above; here: what the
                for half in (CC[:7], CC[7:]):  # 14 conditions make of them (equal, signed and unsigned order, overflow)
                    add("setcc", ["cmp edi, esi"] + [f"set{c} {reg}" for c, reg in zip(half, ("al", "cl", "dl", "bl", "r8b", "r10b", "r11b"))],
                        x, y, fit(x - y))
                    add("cmov", ["cmp rdi, rsi"] + [f"cmov{c} {reg}, esi" for c, reg in zip(half, ("eax", "ecx", "edx", "ebx", "r8d", "r10d", "r11d"))],
                        x, y, fit(x - y))
                    br = ["xor eax, eax"]
                    for k, c in enumerate(half):
                        br += ["cmp edi, esi", f"j{c} {k + 1}f", f"or eax, {1 << k}", f"{k + 1}:"]
                    add("conditional jumps", br, x, y, fit(x - y))
                add("stack", ["push rdi", "push rsi", "pop rax", "pop rcx", "push 5", "push -129", "pop rdx", "pop r8", "call 1f", "jmp 2f",
                              "1:", "lea r10d, [rdi + rsi]", "ret", "2:", "nop"], x, y, fit(-129))
            add("lea", ["lea eax, [rdi + rsi]", "lea ecx, [rdi + 4*rsi + 5]", "lea rdx, [rsi + 8*rdi - 7]", "lea r8d, [rdi - 128]",
                        "lea r10, [rdi + 2*rdi]", "lea r11d, [4*rsi]", "lea rbx, [rsi + 100000]"], x, y, True)
            add("memory", ["mov [r9], edi", "add [r9], esi", "mov eax, [r9]", "sub eax, [r9]", "cmp [r9], edi", "cmp esi, [r9]"],
                x, y, fit(x + y, -x))
        for imm in (0, 1, -1, 7, 127, 128, -128, -129, 255, L - 1, -L, 2047, -2048, 0x12345):
            add("immediate operands", ["mov eax, edi", f"add eax, {imm}", "mov ecx, edi", f"sub ecx, {imm}", "mov edx, edi", f"cmp edx, {imm}",
                                       "mov r8, rdi", f"add r8, {imm}"], x, 0, fit(imm, x + imm, x - imm))
            add("immediate operands", ["mov eax, edi", f"and eax, {imm}", "mov ecx, edi", f"or ecx, {imm}", "mov edx, edi", f"xor edx, {imm}",
                                       f"test edi, {imm}", "mov r8, rdi", f"and r8, {imm}"], x, 0, fit(imm))
            add("immediate operands", [f"mov eax, {imm}", f"mov rcx, {imm}", f"mov dword ptr [r9], {imm}", f"mov qword ptr [r9 + 8], {imm}",
                                       "mov edx, [r9]", "mov r8, [r9 + 8]", f"mov r10b, {imm & 0xFF}", f"push {imm}", "pop r11"], x, 0, fit(imm))
        add("inc dec neg not", ["mov eax, edi", "inc eax", "mov ecx, edi", "dec ecx", "mov edx, edi", "neg edx", "mov r8d, edi", "not r8d",
                                "mov r10, rdi", "neg r10", "inc r10", "dec r10", "not r10"], x, 0, fit(x + 1, x - 1, -x))
        for k in sorted({0, 1, 2, 5, W - 2, W - 1, W, 31}):
            add("shift by constant", ["mov eax, edi", f"shl eax, {k}", "mov edx, edi", f"sar edx, {k}"], x, 0, fit(s32(x << k)))
            add("shift by constant", ["mov ecx, edi", f"shr ecx, {k}"], x, 0, x >= 0 or k == 0)
        for k in sorted({0, 1, W - 1, W, 32, 33, 63}):
            add("shift by constant", ["mov rax, rdi", f"shl rax, {k}", "mov rdx, rdi", f"sar rdx, {k}"], x, 0, fit(x << k))
            add("shift by constant", ["mov rcx, rdi", f"shr rcx, {k}"], x, 0, x >= 0 or k == 0)
        for k in (0, 1, W - 1, W, 31, 32, 33, 63, 64, 65, -1):
            add("shift by cl", ["mov ecx, esi", "mov eax, edi", "shl eax, cl", "mov r8d, edi", "sar r8d, cl", "mov rdx, rdi", "sar rdx, cl"],
                x, k, fit(s32(x << (k & 31))))
            add("shift by cl", ["mov ecx, esi", "mov rax, rdi", "shl rax, cl"], x, k, fit(x << (k & 63)))
            add("shift by cl", ["mov ecx, esi", "mov edx, edi", "shr edx, cl"], x, k, x >= 0 or k & 31 == 0)
            add("shift by cl", ["mov ecx, esi", "mov rdx, rdi", "shr rdx, cl"], x, k, x >= 0 or k & 63 == 0)
        add("sign and zero extension", ["movzx eax, dil", "movsx ecx, dil", "movsxd r8, edi", "movsx r10, dil", "mov eax, edi", "cdqe", "cdq",
                                        "mov rax, rdi", "cqo"], x, 0, True)
        add("memory", ["mov [r9], edi", "mov [r9 + 12], edi", "mov eax, [r9]", "mov ecx, [r9 + 12]", "mov [r9 + 16], rdi", "mov rdx, [r9 + 16]",
                       "mov r8d, [r9 + 16]", "mov r10d, [r9 + 20]", "mov [r9 + 4], edi", "mov r11, [r9]"], x, 0, True)
        add("memory", ["mov dword ptr [r9 + 4], 77", "inc dword ptr [r9 + 4]", "neg dword ptr [r9 + 4]", "mov qword ptr [r9 + 16], -5",
                       "not qword ptr [r9 + 16]", "shl dword ptr [r9 + 16], 3", "push qword ptr [r9 + 16]", "pop rdx", "mov eax, [r9 + 4]",
                       "imul ecx, [r9 + 16], 3", "mov [r9 + 24], rdi", "movsxd r8, dword ptr [r9 + 24]", "cmovs r10, [r9 + 24]"], x, 0, True)
        add("frame", ["push rbp", "mov rbp, rsp", "sub rsp, 16", "mov [rbp - 4], edi", "mov [rbp - 16], rdi", "mov eax, [rbp - 4]",
                      "mov rcx, [rbp - 16]", "lea rdx, [rbp - 16]", "mov r8d, [rdx + 12]", "mov r10d, [rsp]", "leave"], x, 0, True)
        add("indirect jumps", ["lea rax, [rip + 1f]", "call rax", "lea rcx, [rip + 2f]", "jmp rcx", "1:", "lea r8d, [rdi + 1]", "ret",
                               "2:", "lea rdx, [rip + 3f]", "mov [r9], rdx", "jmp qword ptr [r9]", "hlt", "3:", "nop",
                               "nop dword ptr [rax + rax]"], x, 0, True)
    for x in (0, 1, 2, 3):
        add("memory", ["mov [r9 + 4*rdi + 4], esi", "mov eax, [r9 + 4*rdi + 4]", "lea rcx, [r9 + 4*rdi]", "mov edx, [rcx + 4]",
                       "mov r8d, [4*rdi + 0x10000004]", "mov [0x10000000], edi", "mov r10d, [0x10000000]"], x, 77, True)
    # pointers (r9 = DATA_BASE is encoded as 0), constants wider than W bits used as masks, flags the machine does not model
    for body in (["test r9, r9", "je 1f", "mov eax, 1", "1:"], ["cmp r9, 0", "setne al"], ["lea rax, [r9 + 8]", "cmp r9, rax", "jb 1f", "mov ecx, 1", "1:"],
                 ["lea rax, [r9 + 8]", "sub rax, r9"], ["mov rax, r9", "shr rax, 2"], ["mov eax, r9d", "and eax, 7"],
                 ["mov eax, r9d", "xor edx, edx", "mov ecx, 3", "div ecx"], ["imul eax, r9d, 3"], ["lea rax, [r9 + 12]", "mov [r9], rax", "mov rcx, [r9]"],
                 ["mov edi, -1", "test edi, 0x10000000", "sete al"], ["mov edi, -1", "and edi, 0x10000", "jz 1f", "mov eax, 1", "1:"],
                 ["mov eax, 3", "imul eax, eax", "jo 1f", "mov ecx, 1", "1:"], ["mov eax, 3", "shl eax, 2", "jc 1f", "mov ecx, 1", "1:"],
                 ["mov eax, 6", "cdq", "mov ecx, 3", "idiv ecx", "setz al"], ["mov eax, 3", "shl eax, 2", "sets al", "setz cl"]):
        add("pointers, wide constants, unmodelled flags", body, 0, 0, False)
    return out


def edges(W):
    L = 1 << (W - 1)
    stats = {}
    for group, body, x, y, exact in edge_cases(W):
        code = asm(body + ["ret"])
        assert code, body
        info = {}
        m = machine(W, steps=sum(not l.endswith(":") for l in body) + 3)       # straight-line code: one cycle per line, ret, hlt
        cls, why = run_case(m, code, [x, y, 0, 0, 0, 0], [0] * 8, data_ptr_reg=5, isa=ISA, info=info)
        g = stats.setdefault(group, {})
        g[cls] = g.get(cls, 0) + 1
        assert cls != "UNFLAGGED", (W, body, x, y, why)
        assert cls != "discarded", (W, body, x, y, why)
        if exact and -L <= x < L and -L <= y < L:
            assert info["same"], f"W={W} {body} edi={x} esi={y}: circuit differs from the CPU: {why}"
        if info["same"]:
            bad = [i for i in info["issues"] if "does not fit" not in i and "outside data memory" not in i and "does not model" not in i]
            assert not bad, f"W={W} {body} edi={x} esi={y}: circuit equals the CPU but the model disagrees: {bad[0]}"
    return stats


# ------------------------------------------------------------------ 6. fuzzing
R64 = ["rax", "rcx", "rdx", "rbx", "rsi", "rdi", "r8", "r10", "r11"]
R32 = ["eax", "ecx", "edx", "ebx", "esi", "edi", "r8d", "r10d", "r11d"]
R8 = ["al", "cl", "dl", "bl", "sil", "dil", "r8b", "r10b", "r11b"]
ALU = ["add", "sub", "and", "or", "xor", "cmp", "test", "mov"]


def gen(r, n, W):
    L, out, lab = 1 << (W - 1), [], 0
    imm = lambda: r.randrange(-(1 << 31), 1 << 31) if r.random() < 0.04 else \
        r.choice([r.randrange(-L, L), r.randrange(-8, 8), r.randrange(-128, 128)])
    for _ in range(n):
        k = r.randrange(16)
        i, j = r.randrange(9), r.randrange(9)
        mem = f"[r9 + {4 * r.randrange(7)}]"
        if k == 0:
            out.append(f"{r.choice(ALU)} {R32[i]}, {R32[j]}")
        elif k == 1:
            out.append(f"{r.choice(ALU)} {R64[i]}, {R64[j]}")
        elif k == 2:
            out.append(f"{r.choice(ALU)} {R8[i]}, {r.choice([R8[j], str(r.randrange(-128, 128))])}")
        elif k == 3:
            out.append(f"{r.choice(ALU)} {r.choice([R32, R64])[i]}, {imm()}")
        elif k == 4:
            out.append(f"{r.choice(['inc', 'dec', 'neg', 'not'])} {r.choice([R32, R64])[i]}")
        elif k == 5:
            wide = r.random() < 0.4
            cnt = r.choice([str(r.choice([1, r.randrange(64 if wide else 32), r.randrange(W)])), "cl"])
            out.append(f"{r.choice(['shl', 'shr', 'sar'])} {(R64 if wide else R32)[i]}, {cnt}")
        elif k == 6:
            out.append(r.choice([f"imul {R32[i]}, {R32[j]}", f"imul {R64[i]}, {R64[j]}, {r.randrange(-200, 200)}", f"imul {R32[i]}, {mem}"]))
        elif k == 7:
            lab += 1
            out += [f"cmp {R32[i]}, {r.choice([R32[j], str(r.randrange(-8, 8))])}", f"j{r.choice(CC)} {lab}f",
                    f"{r.choice(['add', 'xor', 'mov'])} {R32[r.randrange(9)]}, {r.randrange(-8, 8)}", f"{lab}:"]
        elif k == 8:
            wide = r.random() < 0.4
            reg = (R64 if wide else R32)[i]
            out.append(r.choice([f"mov {mem}, {reg}", f"mov {reg}, {mem}", f"{r.choice(ALU)} {reg}, {mem}", f"{r.choice(ALU)} {mem}, {reg}",
                                 f"{r.choice(ALU[:6])} {'qword' if wide else 'dword'} ptr {mem}, {r.randrange(-100, 100)}"]))
        elif k == 9:
            out += [f"push {r.choice([R64[i], str(imm()), 'qword ptr ' + mem])}", f"{r.choice(ALU)} {R32[j]}, {R32[i]}", f"pop {R64[r.randrange(9)]}"]
        elif k == 10:
            c = r.choice(CC)
            out.append(r.choice([f"set{c} {R8[i]}", f"cmov{c} {R32[i]}, {R32[j]}", f"cmov{c} {R64[i]}, {mem}", f"movzx {R32[i]}, {R8[j]}",
                                 f"movsx {r.choice([R32, R64])[i]}, {R8[j]}", f"movsxd {R64[i]}, {R32[j]}"]))
        elif k == 11:
            out.append(f"lea {r.choice([R32, R64])[i]}, [{R64[j]} + {r.choice([1, 2, 4, 8])}*{R64[r.randrange(9)]} + {r.randrange(-64, 64)}]")
        elif k == 12:
            signed = r.random() < 0.6
            out += [f"mov ecx, {r.choice([1, 2, 3, 7, -1, -3, 10, r.randrange(1, L)])}", f"mov eax, {R32[i]}",
                    "cdq" if signed else "xor edx, edx", ("idiv" if signed else "div") + " ecx"]
        elif k == 13:
            lab += 2
            out += [f"call {lab - 1}f", f"jmp {lab}f", f"{lab - 1}:", f"{r.choice(['add', 'sub', 'xor'])} {R32[i]}, {R32[j]}", "ret", f"{lab}:"]
        elif k == 14:
            out.append(r.choice([f"mov {R32[i]}, {imm()}", f"mov {R64[i]}, {imm()}", f"mov {R8[i]}, {r.randrange(256)}", "cdqe", "cdq", "cqo"]))
        else:
            out.append(f"{r.choice(['add', 'sub', 'cmp'])} {R32[i]}, {R32[j]}")
    return out + ["ret"]


def fuzz(W, N, seed):
    m = machine(W, steps=40)             # programs that need more cycles are discarded
    r = random.Random(seed)
    L = 1 << (W - 1)
    st = dict(generated=0, discarded=0)
    while st["generated"] < N:
        st["generated"] += 1
        code = assemble(INTEL + gen(r, r.randrange(4, 12), W), ISA)
        assert code
        args = [r.choice([r.randrange(-L, L), r.randrange(-6, 7)]) for _ in range(5)] + [0]
        if r.random() < 0.2:                  # sometimes one value outside the model
            args[r.randrange(5)] = r.choice([L, -L - 1, 1 << 20, -(1 << 31)])
        mem = [r.getrandbits(32) if r.random() < 0.1 else r.choice([r.randrange(-L, L), r.randrange(8)]) for _ in range(8)]
        info = {}
        cls, why = run_case(m, code, args, mem, data_ptr_reg=5, isa=ISA, info=info)
        if cls == "discarded":
            st["discarded"] += 1
            continue
        st[cls] = st.get(cls, 0) + 1
        assert cls != "UNFLAGGED", (W, bytes(code).hex(), args, why)
        if info["same"]:
            bad = [i for i in info["issues"] if "does not fit" not in i and "outside data memory" not in i and "does not model" not in i]
            assert not bad, f"circuit equals the CPU but the model disagrees: {bad[0]}"
    return st


def stub_matches_source():
    with tempfile.TemporaryDirectory() as d:
        o, b = os.path.join(d, "s.o"), os.path.join(d, "s.bin")
        subprocess.run(["clang", "--target=x86_64-linux-gnu", "-c", os.path.join(ROOT, "docs", "output_stub_x86.s"), "-o", o], check=True)
        subprocess.run(["llvm-objcopy", "-O", "binary", "--only-section=.text", o, b], check=True)
        return open(b, "rb").read() == OUTPUT_STUB_X86


if __name__ == "__main__":
    N = int(sys.argv[1]) if len(sys.argv) > 1 else 150
    seed = int(sys.argv[2]) if len(sys.argv) > 2 else 7
    widths = [int(x) for x in sys.argv[3].split(",")] if len(sys.argv) > 3 else [8, 12, 16]
    n = decoder_table()
    print(f"1) decoder patterns: mutually exclusive for all 4 x 65536 keys; {n} reference instructions decoded "
          f"with the disassembler's length, covering all {len(x86.KINDS)} kinds")
    for W in widths:
        print(f"2) circuit == model, one cycle, W={W}: {20 * N} random byte strings {circuit_vs_model(W, 20 * N, seed + W)}")
    print(f"3) trap: {traps()} unsupported instructions trap in the circuit and change no register, flag, memory cell or PC")
    print(f"4) superposition of a valid and an invalid instruction: {superposed_valid_and_invalid()}")
    total = 0
    for W in widths:
        s = edges(W)
        n = sum(sum(v.values()) for v in s.values())
        total += n
        print(f"5) boundary cases W={W}: {n} cases {s}")
    print(f"   {total} boundary cases: no unflagged mismatch; exact wherever operands and results are representable")
    for i, W in enumerate(widths):
        print(f"6) fuzz W={W} seed={seed + 4 * i}: {fuzz(W, N, seed + 4 * i)}")
    assert stub_matches_source()
    print("7) output stub bytes equal the assembled docs/output_stub_x86.s")
    print("PASS")
