"""Q-ARM v1: coherent quantum operations in CPU-skippable AArch64 packets.

All decoding depends on program-memory qubits, never on the loaded program.
The quantum data bank is mutated in place OUTSIDE the ARM compute/copy/uncompute
block. Only classical decoder scratch is uncomputed.
"""
from dataclasses import dataclass
import math
from .machine import Config, QuantumARM
from .a64 import check

WORDS = 8
BRANCH = 0x14000008                 # B .+32; ordinary AArch64 executes only this
MAGIC = 0x4D524151                  # bytes "QARM" in a little-endian image
VERSION = 1
U, CX, PHASE = 1, 2, 3
ANGLE_UNIT = math.pi / (1 << 29)   # signed 32-bit angle, period 8*pi
RET = 0xD65F03C0
HLT = 0xD4400000


def angle_word(angle):
    angle = float(angle)
    if not math.isfinite(angle):
        raise ValueError("angles must be finite")
    # 8*pi preserves U's 4*pi theta periodicity and conditional global phase.
    return round(math.remainder(angle, 8 * math.pi) / ANGLE_UNIT) & 0xFFFFFFFF


def angle_value(word):
    return (word if word < 0x80000000 else word - 0x100000000) * ANGLE_UNIT


def packet(op, a=0, b=0, theta=0, phi=0, lam=0):
    if not (isinstance(a, int) and isinstance(b, int) and 0 <= a < 256 and 0 <= b < 256):
        raise ValueError("qubit indices must be integers in 0..255")
    if op not in (U, CX, PHASE):
        raise ValueError("unknown Q-ARM opcode")
    if op == CX and (a == b or any((theta, phi, lam))):
        raise ValueError("CX requires distinct qubits and no angles")
    if op == U and b:
        raise ValueError("U has no second operand")
    if op == PHASE and (a or b or phi or lam):
        raise ValueError("PHASE has only a theta operand")
    return [BRANCH, MAGIC, VERSION << 24 | op << 16 | b << 8 | a,
            angle_word(theta), angle_word(phi), angle_word(lam), 0, 0]


def decode_packet(words, at, qubits):
    """Return (op,a,b,theta,phi,lambda), None for ordinary ARM, reject malformed packets."""
    if words[at] != BRANCH or at + 1 >= len(words) or words[at + 1] != MAGIC:
        return None
    if at + WORDS > len(words):
        raise ValueError(f"truncated Q-ARM packet at word {at}")
    desc, t, p, l, r0, r1 = words[at + 2:at + WORDS]
    a, b, op, version = desc & 255, (desc >> 8) & 255, (desc >> 16) & 255, desc >> 24
    if version != VERSION or r0 or r1:
        raise ValueError(f"invalid Q-ARM version/reserved fields at word {at}")
    if op == U:
        valid = a < qubits and b == 0
    elif op == CX:
        valid = a < qubits and b < qubits and a != b and not (t or p or l)
    elif op == PHASE:
        valid = not (a or b or p or l)
    else:
        valid = False
    if not valid:
        raise ValueError(f"invalid Q-ARM opcode/operands at word {at}")
    return op, a, b, angle_value(t), angle_value(p), angle_value(l)


def validate_program(words, qubits):
    """Validate a flat A64 image, excluding data inside recognized packets.

    Direct branches must target instruction boundaries. Indirect branch targets
    remain the caller's responsibility, just as in ordinary mixed code/data ARM.
    """
    if not 1 <= qubits <= 256:
        raise ValueError("quantum bank size must be in 1..256")
    if not words or any(not isinstance(w, int) or not 0 <= w <= 0xFFFFFFFF for w in words):
        raise ValueError("program must contain unsigned 32-bit words")
    starts, packets, checks = set(), {}, {}
    i = 0
    while i < len(words):
        starts.add(i)
        decoded = decode_packet(words, i, qubits)
        if decoded is not None:
            packets[i] = decoded
            checks[i] = check(words[i], i, len(words))
            i += WORDS
        else:
            checks[i] = check(words[i], i, len(words))
            i += 1
    for i, ck in checks.items():
        if not ck.ok:
            raise ValueError(f"unsupported ARM instruction at word {i}: {ck.reason}")
        if ck.cls in ("b", "bl", "bcond", "cbz", "tbz"):
            lo, n = {"b": (0, 26), "bl": (0, 26), "tbz": (5, 14)}.get(ck.cls, (5, 19))
            off = (words[i] >> lo) & ((1 << n) - 1)
            if off & (1 << (n - 1)):
                off -= 1 << n
            if i + off not in starts:
                raise ValueError(f"branch at word {i} enters packet data")
    return packets, checks


@dataclass
class CompiledQASM:
    words: list
    qubits: int
    clbits: int
    measurements: list             # (qubit, classical bit), terminal readout only

    def assembly(self, symbol="qarm_kernel"):
        if not symbol or not symbol.isascii() or not symbol.replace('_', 'a').isalnum() or symbol[0].isdigit():
            raise ValueError("invalid assembly symbol")
        lines = ["// Q-ARM v1. Native AArch64 skips every quantum packet.",
                 ".text", ".balign 4", f".global {symbol}", f"{symbol}:"]
        for i in range(0, len(self.words) - 1, WORDS):
            lines += ["    b 1f", "    .word " + ", ".join(f"0x{w:08x}" for w in self.words[i+1:i+WORDS]), "1:"]
        return "\n".join(lines + ["    ret", ""])


def compile_qasm(source):
    """Lower OpenQASM 2 unitary gates to U/CX/PHASE; retain terminal readout.

    Qiskit's strict parser supplies custom gate definitions, register broadcasting,
    and qelib1.inc. No external include files are searched. Dynamic/opaque operations
    are rejected, never silently dropped.
    """
    from qiskit import QuantumCircuit, qasm2, transpile
    circuit = qasm2.loads(source, include_path=(), strict=True)
    if not 1 <= circuit.num_qubits <= 256:
        raise ValueError("Q-ARM requires 1..256 qubits")
    body = QuantumCircuit(circuit.num_qubits)
    measured, readout = False, []
    for inst in circuit.data:
        op = inst.operation
        qs = [circuit.find_bit(q).index for q in inst.qubits]
        if op.name == "barrier":
            continue
        if op.name == "measure":
            measured = True
            readout.append((qs[0], circuit.find_bit(inst.clbits[0]).index))
            continue
        if measured:
            raise ValueError("only terminal measurements are supported; gate after measurement")
        if op.name == "reset" or inst.clbits or getattr(op, "condition", None) is not None:
            raise ValueError("reset and classical feedback are not supported in coherent Q-ARM v1")
        try:
            op.to_matrix()  # opaque gates without definitions must fail below instead
        except Exception:
            if op.definition is None:
                raise ValueError(f"unsupported/opaque operation: {op.name}") from None
        body.append(op, qs)
    try:
        lowered = transpile(body, basis_gates=["u", "cx"], optimization_level=0)
    except Exception as exc:
        raise ValueError(f"cannot lower QASM to unitary U/CX gates: {exc}") from exc
    words = []
    if float(lowered.global_phase):
        words += packet(PHASE, theta=float(lowered.global_phase))
    for inst in lowered.data:
        qs = [lowered.find_bit(q).index for q in inst.qubits]
        if inst.operation.name == "u":
            t, p, l = map(float, inst.operation.params)
            words += packet(U, qs[0], theta=t, phi=p, lam=l)
        elif inst.operation.name == "cx":
            words += packet(CX, *qs)
        else:
            raise ValueError(f"unexpected lowered gate: {inst.operation.name}")
    return CompiledQASM(words + [RET], circuit.num_qubits, circuit.num_clbits, readout)


class QuantumQARM(QuantumARM):
    """ARM interpreter with a separate quantum bank and coherent packet decoder."""
    def __init__(self, cfg: Config, qubits, defer=False):
        if not isinstance(qubits, int) or not 1 <= qubits <= 256:
            raise ValueError("qubits must be in 1..256")
        if cfg.SLOTS < 16 or "b" not in cfg.classes:
            raise ValueError("Q-ARM needs at least 16 slots and the b instruction class")
        super().__init__(cfg, defer=True)
        self.Q = self.reg("quantum", qubits)
        if not defer:
            self.build()

    def run_fast(self, *args, **kwargs):
        raise ValueError("Q-ARM requires SparseState or Qiskit; basis-only run_fast is not valid")

    def load_bits(self, words, regs_init=None, ram_init=None, nzcv=0, pc=0):
        _, checks = validate_program(words, len(self.Q))
        disabled = {ck.cls for ck in checks.values()} - set(self.cfg.classes)
        if disabled:
            raise ValueError(f"ARM classes disabled in this machine: {sorted(disabled)}")
        if pc not in checks:
            raise ValueError("entry PC is not an instruction boundary")
        return super().load_bits(words, regs_init, ram_init, nzcv, pc)

    def _step(self, s):
        self._quantum_step(s)
        super()._step(s)

    def _quantum_step(self, s):
        # Decode into clean scratch. No gate below depends on host-side program data.
        self.ptr = 0
        begin = len(self.g)
        fields = [self.alloc(32) for _ in range(WORDS)]
        for slot in range(self.cfg.SLOTS - WORDS + 1):
            def use(sel, slot=slot):
                for k in range(WORDS):
                    self.gated_copy(sel, self.mem[slot+k], fields[k])
            self.local(lambda slot=slot: self.fresh_match(self.field_eq(self.pc, 0, self.cfg.pbits, slot)), use)
        ir, magic, desc, theta, phi, lam, r0, r1 = fields
        marker = self.fresh_match([(self.run[s], 1)] + self.field_eq(ir, 0, 32, BRANCH)
                                  + self.field_eq(magic, 0, 32, MAGIC))
        common = [(marker, 1)] + self.field_eq(desc, 24, 8, VERSION) + [(q, 0) for q in r0+r1]
        controls = []
        for a in range(len(self.Q)):
            flag = self.fresh_match(common + self.field_eq(desc, 16, 8, U)
                                    + self.field_eq(desc, 0, 16, a))
            controls.append((U, flag, a, 0))
            for b in range(len(self.Q)):
                if a == b:
                    continue
                flag = self.fresh_match(common + self.field_eq(desc, 16, 8, CX)
                                        + self.field_eq(desc, 0, 16, a | b << 8)
                                        + [(q, 0) for q in theta+phi+lam])
                controls.append((CX, flag, a, b))
        phase = self.fresh_match(common + self.field_eq(desc, 0, 24, PHASE << 16)
                                 + [(q, 0) for q in phi+lam])
        controls.append((PHASE, phase, 0, 0))
        valid = self.fresh_xor(*[flag for _, flag, _, _ in controls])
        end = len(self.g)
        # Persist a fault flag for the ARM cycle's trap logic. It is history, not scratch.
        self._extension_fault = self.reg(f"qfault{s}", 1)[0]
        self.mcx([(marker, 1), (valid, 0)], self._extension_fault)
        for op, flag, a, b in controls:
            if op == CX:
                self.ccx(flag, self.Q[a], self.Q[b])
            elif op == PHASE:
                for i, bit in enumerate(theta):
                    angle = ANGLE_UNIT * (1 << i) * (-1 if i == 31 else 1)
                    self.g.append(("cp", angle, bit, flag))
            else:
                # U(theta,phi,lambda) = P(phi) RY(theta) P(lambda), rightmost first.
                for word, gate in ((lam, "cp"), (theta, "cry"), (phi, "cp")):
                    for i, bit in enumerate(word):
                        angle = ANGLE_UNIT * (1 << i) * (-1 if i == 31 else 1)
                        self.local(lambda bit=bit, flag=flag: self.fresh_and(flag, bit),
                                   lambda en, angle=angle, gate=gate, a=a:
                                   self.g.append((gate, angle, en, self.Q[a])))
        self.g.extend(reversed(self.g[begin:end]))
        self.ptr = 0


def make_machine(words, qubits, steps=None, width=8):
    """Convenient flat-image runner setup; raw image must end in RET or HLT."""
    image = list(words)
    if not image or image[-1] not in (RET, HLT):
        raise ValueError("flat image must end in RET or HLT")
    # A RET reaches an interpreter-only HLT sentinel, outside the CPU function.
    if image[-1] == RET:
        image.append(HLT)
    _, checks = validate_program(image, qubits)
    slots = max(16, 1 << (len(image) - 1).bit_length())
    width = max(width, (slots - 1).bit_length() + 2)
    if width > 16:
        raise ValueError("program exceeds the existing ARM model's address width")
    regs = {30}
    for ck in checks.values():
        regs.update(ck.reads); regs.update(ck.writes)
    uses_sp = 31 in regs
    regs.discard(31)
    cfg = Config(W=width, regs=tuple(sorted(regs)), sp=uses_sp, SLOTS=slots,
                 STEPS=steps if steps is not None else len(checks),
                 classes=tuple({ck.cls for ck in checks.values()} | {"b"}))
    if cfg.STEPS <= 0:
        raise ValueError("steps must be positive")
    machine = QuantumQARM(cfg, qubits)
    ones = machine.load_bits(image, {30: 4 * (len(image)-1)})
    return machine, ones
