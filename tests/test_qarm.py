"""Q-ARM contract tests. Run: python -m pytest -q tests/test_qarm.py."""
import math
import struct
import numpy as np
import pytest
from qiskit import QuantumCircuit, qasm2
from qiskit.quantum_info import Statevector
from qcpu.qarm import (BRANCH, MAGIC, WORDS, U, CX, PHASE, RET, HLT, ANGLE_UNIT,
                       packet, compile_qasm, validate_program, make_machine, QuantumQARM)
from qcpu.machine import Config, QuantumARM
from qcpu.sparsesim import SparseState

HEADER = 'OPENQASM 2.0; include "qelib1.inc"; qreg q[2]; '


def execute(words, qubits=2, steps=None):
    m, ones = make_machine(words, qubits, steps)
    state = SparseState(m.n, ones)
    state.apply(m.g)
    assert state.probabilities(m.run[-1:]) == pytest.approx({0: 1})
    assert state.probabilities(m.trap) == pytest.approx({0: 1})
    assert not state.bits[:, m.pool].any()
    return m, ones, state


def quantum_density(state, qubits):
    rho = np.zeros((1 << len(qubits),) * 2, complex)
    for part in state.split(qubits):
        v = np.zeros(1 << len(qubits), complex)
        for index, amp in zip(part.values(range(len(qubits))), part.amp):
            v[index] += amp
        rho += np.outer(v, v.conj())
    return rho


def test_bell_and_inverse():
    p = compile_qasm(HEADER + 'creg c[2]; h q[0]; cx q[0],q[1]; measure q -> c;')
    assert p.measurements == [(0,0), (1,1)]
    m, ones, s = execute(p.words)
    assert s.probabilities(m.Q) == pytest.approx({0: .5, 3: .5}, abs=1e-8)
    s.apply(m.g, inverse=True)
    assert len(s.amp) == 1
    assert s.amp[0] == pytest.approx(1)
    assert set(s.bits[0].nonzero()[0]) == set(ones)


@pytest.mark.parametrize('body', [
    'U(-0.71,1.3,-2.1) q[0]; CX q[0],q[1];',
    'h q[0]; t q[0]; h q[0];',
    'gate custom(t) a,b { ry(t) a; cx a,b; rz(t/3) b; } custom(pi/3) q[0],q[1];',
    'gate swap a,b { cx a,b; cx b,a; cx a,b; } h q; cz q[0],q[1]; sdg q[0]; swap q[0],q[1];',
    'rx(-3*pi/2) q[1]; ry(pi/7) q[0]; crz(pi/9) q[0],q[1];',
])
def test_against_qiskit(body):
    source = HEADER + body
    expected = Statevector.from_instruction(qasm2.loads(source))
    p = compile_qasm(source)
    m, _, s = execute(p.words)
    assert quantum_density(s, m.Q) == pytest.approx(np.outer(expected.data, expected.data.conj()), abs=8e-8)


def test_arm_cpu_skips_and_preserves_flags():
    from unicorn import Uc, UC_ARCH_ARM64, UC_MODE_ARM
    from unicorn.arm64_const import UC_ARM64_REG_X0, UC_ARM64_REG_X1, UC_ARM64_REG_X30, UC_ARM64_REG_NZCV
    # MOVZ W0,#5; quantum block; ADD W0,W0,#3; RET.
    words = [0x528000A0] + packet(U, theta=math.pi/2, lam=math.pi) + packet(CX, 0, 1) + [0x11000C00, RET]
    m, _, s = execute(words)
    assert s.probabilities(m.X[0]) == pytest.approx({8: 1})
    cpu = Uc(UC_ARCH_ARM64, UC_MODE_ARM)
    base = 0x10000
    raw = struct.pack(f'<{len(words)}I', *words)
    cpu.mem_map(base, 0x1000); cpu.mem_write(base, raw)
    cpu.reg_write(UC_ARM64_REG_X30, base + len(raw))
    cpu.reg_write(UC_ARM64_REG_X1, 12345)
    cpu.reg_write(UC_ARM64_REG_NZCV, 0xA0000000)
    cpu.emu_start(base, base + len(raw), count=20)
    assert cpu.reg_read(UC_ARM64_REG_X0) == 8
    assert cpu.reg_read(UC_ARM64_REG_X1) == 12345
    assert cpu.reg_read(UC_ARM64_REG_NZCV) == 0xA0000000
    assert cpu.reg_read(UC_ARM64_REG_X30) == base + len(raw)


def test_branch_skips_quantum_packet():
    words = [0x14000009] + packet(U, theta=math.pi) + [RET]
    m, _, s = execute(words, 1)
    assert s.probabilities(m.Q) == pytest.approx({0: 1})


def test_program_phase_is_coherent():
    # A superposition of PHASE(0) and PHASE(pi), encoded in program memory.
    m, ones = make_machine(packet(PHASE) + [RET], 1)
    bit = m.mem[3][29]     # theta bit 29 encodes pi
    s = SparseState(m.n, ones)
    s.h(bit); s.apply(m.g); s.h(bit)
    assert s.probabilities([bit]) == pytest.approx({1: 1})


def test_quantum_operands_are_decoded_from_program_memory():
    m, ones = make_machine(packet(U, theta=math.pi) + [RET], 2)
    bit = m.mem[2][0]  # U target 0 or 1 in superposition
    s = SparseState(m.n, ones); s.h(bit); s.apply(m.g)
    assert s.probabilities(m.Q) == pytest.approx({1: .5, 2: .5}, abs=1e-8)
    s.apply(m.g, inverse=True); s.h(bit)
    assert len(s.amp) == 1
    assert set(s.bits[0].nonzero()[0]) == set(ones)


def test_invalid_quantum_program_state_traps():
    m, ones = make_machine(packet(U) + [RET], 1)
    # Corrupt version AFTER static validation, modeling an invalid quantum-memory branch.
    ones = list(ones) + [m.mem[2][25]]
    s = SparseState(m.n, ones); s.apply(m.g)
    assert s.probabilities(m.trap).get(0,0) == 0
    assert s.probabilities(m.run[-1:]) == pytest.approx({0: 1})


def test_plain_arm_mode_skips_identical_bytes():
    words = packet(U, theta=math.pi) + [HLT]
    m = QuantumARM(Config(W=8, regs=(), SLOTS=16, STEPS=2, classes=('b','hlt')))
    from qcpu.bitsim import simulate, value
    state = simulate(m.g, m.n, m.load_bits(words))
    assert value(state, m.pc_hist[-1]) == 8
    assert state[m.run[-1]] == 0


@pytest.mark.parametrize('tail', [
    'reset q[0];', 'creg c[1]; measure q[0] -> c[0]; x q[0];',
    'creg c[1]; if(c==0) x q[0];', 'opaque mystery a; mystery q[0];',
])
def test_unsupported_qasm_is_rejected(tail):
    with pytest.raises(Exception):
        compile_qasm(HEADER + tail)


def test_validation():
    with pytest.raises(ValueError, match='truncated'):
        validate_program(packet(U)[:4], 1)
    bad = packet(U); bad[6] = 1
    with pytest.raises(ValueError, match='reserved'):
        validate_program(bad + [RET], 1)
    with pytest.raises(ValueError, match='operands'):
        validate_program(packet(U, 1) + [RET], 1)
    with pytest.raises(ValueError, match='packet data'):
        validate_program([0x14000002] + packet(U) + [RET], 1)
    with pytest.raises(ValueError, match='distinct'):
        packet(CX, 0, 0)
    with pytest.raises(ValueError, match='finite'):
        packet(U, theta=float('nan'))


def test_sparse_cry_matches_qiskit_and_inverse():
    rng = np.random.default_rng(42)
    s = SparseState(3); qc = QuantumCircuit(3)
    gates = [('h',0), ('h',1), ('h',2)]
    for _ in range(12):
        c,t = rng.choice(3, 2, replace=False).tolist()
        gates += [('cry', float(rng.uniform(-7,7)), c,t), ('cp', float(rng.uniform(-7,7)),t,c)]
    for gate in gates:
        getattr(qc,gate[0])(*gate[1:])
    s.apply(gates)
    v = np.zeros(8, complex)
    for index, amp in zip(s.values([0,1,2]),s.amp): v[index] = amp
    assert v == pytest.approx(Statevector.from_instruction(qc).data)
    s.apply(gates,inverse=True)
    assert s.probabilities([0,1,2]) == pytest.approx({0:1})


def test_qiskit_export_contains_real_unitary_gates():
    m, _ = make_machine(packet(U, theta=math.pi/2) + [RET],1)
    qc = m.to_qiskit(measure=['quantum'])
    assert qc.count_ops()['cry'] > 0
    assert qc.count_ops()['cp'] > 0
    assert qc.num_clbits == 1


def test_coherent_arm_branch_and_loop():
    # CBZ W0, after_packet; U(pi) q0; after_packet: RET
    words = [0x34000120] + packet(U, theta=math.pi) + [RET]
    m, ones = make_machine(words, 1)
    bit = m.X[0][0]
    s = SparseState(m.n, ones); s.h(bit); s.apply(m.g)
    assert s.probabilities(m.Q + [bit]) == pytest.approx({0: .5, 3: .5}, abs=1e-8)
    s.apply(m.g, inverse=True); s.h(bit)
    assert set(s.bits[0].nonzero()[0]) == set(ones)
    assert len(s.amp) == 1
    # MOV W0,#2; loop: U(pi); SUBS W0,W0,#1; B.NE loop; RET
    bne = 0x54000001 | (((1-10) & ((1 << 19)-1)) << 5)
    words = [0x52800040] + packet(U, theta=math.pi) + [0x71000400, bne, RET]
    m, _, s = execute(words, 1, steps=9)
    assert s.probabilities(m.Q) == pytest.approx({0: 1}, abs=1e-8)
    assert s.probabilities(m.X[0]) == pytest.approx({0: 1})


def test_no_gate_program_and_angle_periodicity():
    p = compile_qasm(HEADER + 'creg c[2]; measure q -> c;')
    m, _, s = execute(p.words)
    assert s.probabilities(m.Q) == pytest.approx({0: 1})
    # A 2*pi rotation is -I, not I; conditional global phase must survive.
    m, ones = make_machine(packet(U) + [RET], 1)
    bit = m.mem[3][30]
    s = SparseState(m.n, ones); s.h(bit); s.apply(m.g); s.h(bit)
    assert s.probabilities([bit]) == pytest.approx({1: 1})


def test_assembly_matches_binary_when_toolchain_available(tmp_path):
    import shutil, subprocess
    if not shutil.which('clang') or not shutil.which('llvm-objcopy'):
        pytest.skip('AArch64 assembler/objcopy unavailable')
    p = compile_qasm(HEADER + 'h q[0]; cx q[0],q[1];')
    assembly, obj, binary = (tmp_path / name for name in ('bell.s','bell.o','bell.bin'))
    assembly.write_text(p.assembly())
    subprocess.run(['clang','--target=aarch64-linux-gnu','-c',str(assembly),'-o',str(obj)],check=True)
    subprocess.run(['llvm-objcopy','-O','binary','--only-section=.text',str(obj),str(binary)],check=True)
    assert binary.read_bytes() == struct.pack(f'<{len(p.words)}I',*p.words)
    subprocess.run(['clang','--target=aarch64-linux-gnu','-c','examples/qarm/mixed.s','-o',str(obj)],check=True)
    subprocess.run(['llvm-objcopy','-O','binary','--only-section=.text',str(obj),str(binary)],check=True)
    raw = binary.read_bytes()
    words = list(struct.unpack(f'<{len(raw)//4}I',raw))
    m, _, s = execute(words)
    assert s.probabilities(m.Q) == pytest.approx({0: .5,3: .5})
    assert s.probabilities(m.X[0]) == pytest.approx({8:1})


def test_full_interpreter_export_on_aer():
    from qiskit import transpile
    from qiskit_aer import AerSimulator
    p = compile_qasm(HEADER + 'h q[0]; cx q[0],q[1];')
    m, ones = make_machine(p.words,2)
    sim = AerSimulator(method='matrix_product_state')
    qc = transpile(m.to_qiskit(ones, measure=['quantum']),
                   basis_gates=['u','cx','ccx','cswap','cp'], optimization_level=0)
    counts = sim.run(qc,shots=128,seed_simulator=2026).result().get_counts()
    assert set(counts) == {'00','11'}
    assert 35 < counts['00'] < 93
