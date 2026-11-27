#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Quantag IT Solutions GmbH
"""The VQE instructions of Q-ARM v2 (docs/qarm_v2.md, section 6), used by qarm_exec.py.

A program describes its problem with two tables in its own memory (include/qarm.h):
    qarm_hamiltonian   H = constant + sum c_j P_j, each Pauli string P_j as bit masks x, z (Y = both)
    qarm_ansatz        |psi(theta)> = ops applied to the basis state init
QVLOAD checks them; QEXPECT and QEXPECTN return <psi(theta)|H|psi(theta)>, exactly (the simulator) or measured on a
backend of qcpu_run.py (simulator, aer, preview, ibm): the terms are grouped into qubit-wise commuting measurement
bases, and each batch of parameter vectors is one Sampler job, so a QPU queues once per batch."""
import math
import struct
import time

KINDS = {1: "x", 2: "h", 3: "cx", 4: "cz", 5: "ry", 6: "rz", 7: "pauli_rot"}
PARAM_KINDS = ("ry", "rz", "pauli_rot")
TWO_QUBIT = ("cx", "cz")
MAX_EXACT_QUBITS = 20


class VqeError(Exception):
    pass


def label(n, x, z):
    """The Pauli string of masks (x, z) in Qiskit's order: qubit 0 is the last character."""
    return "".join("IXZY"[((x >> q) & 1) + 2 * ((z >> q) & 1)] for q in reversed(range(n)))


class Problem:
    """A Hamiltonian and an ansatz, read from a program's memory: read(addr, n) -> bytes."""

    def __init__(self, read, h_addr, a_addr):
        try:
            n, nt = struct.unpack("<II", read(h_addr, 8))
            self.constant, tp = struct.unpack("<dQ", read(h_addr + 8, 16))
            if not 1 <= n <= 64 or nt > 100000:
                raise VqeError(f"the Hamiltonian table: {n} qubits, {nt} terms")
            raw = read(tp, 24 * nt) if nt else b""
            self.terms = [struct.unpack_from("<QQd", raw, 24 * i) for i in range(nt)]
            aq, self.params, nops, _ = struct.unpack("<IIII", read(a_addr, 16))
            self.init, op = struct.unpack("<QQ", read(a_addr + 16, 16))
            if aq != n:
                raise VqeError(f"the ansatz has {aq} qubits, the Hamiltonian {n}")
            if nops > 100000 or self.params > 255:
                raise VqeError(f"the ansatz table: {nops} operations, {self.params} parameters")
            raw = read(op, 32 * nops) if nops else b""
        except struct.error:
            raise VqeError("a table could not be read")
        self.n, self.ops = n, []
        full = (1 << n) - 1
        for x, z, c in self.terms:
            if (x | z) & ~full:
                raise VqeError(f"a Pauli term acts on a qubit beyond {n}")
        if self.init & ~full:
            raise VqeError(f"the initial state {self.init:#x} has bits beyond {n} qubits")
        for i in range(nops):
            kind, q0, q1, param = struct.unpack_from("<BBBB", raw, 32 * i)
            scale, = struct.unpack_from("<d", raw, 32 * i + 8)
            x, z = struct.unpack_from("<QQ", raw, 32 * i + 16)
            k = KINDS.get(kind)
            if k is None:
                raise VqeError(f"ansatz operation {i}: unknown kind {kind}")
            if k == "pauli_rot":
                if not (x | z) or (x | z) & ~full:
                    raise VqeError(f"ansatz operation {i}: Pauli masks {x:#x}, {z:#x}")
            elif q0 >= n or (k in TWO_QUBIT and (q1 >= n or q1 == q0)):
                raise VqeError(f"ansatz operation {i} ({k}): qubits {q0}, {q1} of {n}")
            if k in PARAM_KINDS and param >= self.params:
                raise VqeError(f"ansatz operation {i} ({k}): parameter {param} of {self.params}")
            self.ops.append((k, q0, q1, param, scale, x, z))

    def describe(self):
        twoq = sum(1 for o in self.ops if o[0] in TWO_QUBIT)
        return (f"{self.n} qubits, {len(self.terms)} Pauli terms, an ansatz of {len(self.ops)} operations "
                f"({twoq} two-qubit) with {self.params} parameter{'s' if self.params != 1 else ''}")

    # ---------------------------------------------------------------- exact
    # ---------------------------------------------------------------- exact
    def rotations(self):
        """The indices of the ansatz's rotations (RY, RZ, Pauli rotations), in order."""
        return [i for i, o in enumerate(self.ops) if o[0] in PARAM_KINDS]

    def angles(self, theta, shift=None, delta=0.0):
        """Each rotation's angle at theta (scale * theta[param]); rotation number shift moved by delta."""
        out = []
        for r, i in enumerate(self.rotations()):
            k, q0, q1, p, scale, x, z = self.ops[i]
            out.append(scale * theta[p] + (delta if r == shift else 0.0))
        return out

    def _engine(self):
        """Arrays for the state vector: Pauli strings as a permutation and phases; H without its constant."""
        if getattr(self, "_eng", None) is not None:
            return self._eng
        import numpy as np
        if self.n > MAX_EXACT_QUBITS:
            raise VqeError(f"{self.n} qubits: the exact simulator takes up to {MAX_EXACT_QUBITS}")
        N = 1 << self.n
        idx = np.arange(N, dtype=np.int64)
        cache = {}

        def pauli_data(x, z):
            if (x, z) not in cache:
                cache[x, z] = (idx ^ x, (1j ** (bin(x & z).count("1") & 3)) * (1 - 2 * _parity(idx & z)))
            return cache[x, z]

        def pauli(psi, x, z):                     # P psi, P = i^popcount(x&z) X^x Z^z
            perm, f = pauli_data(x, z)
            out = np.empty_like(psi)
            out[perm] = f * psi
            return out
        terms = [(x, z, c) for x, z, c in self.terms if x or z]
        const = self.constant + sum(c for x, z, c in self.terms if not (x or z))
        H = None
        try:
            import scipy.sparse as sp
            if len(terms) * N <= 50_000_000 and terms:
                rows = np.concatenate([pauli_data(x, z)[0] for x, z, c in terms])
                cols = np.tile(idx, len(terms))
                vals = np.concatenate([c * pauli_data(x, z)[1] for x, z, c in terms])
                H = sp.csr_matrix((vals, (rows, cols)), shape=(N, N))
        except ImportError:
            pass

        def hpsi(psi):
            if H is not None:
                return H @ psi
            out = np.zeros_like(psi)
            for x, z, c in terms:
                out += c * pauli(psi, x, z)
            return out
        self._eng = dict(N=N, idx=idx, pauli=pauli, hpsi=hpsi, const=const)
        return self._eng

    def _apply(self, psi, op, ang):
        """One operation at angle ang (RY, RZ: exp(-i ang G/2); a Pauli rotation: exp(-i ang P))."""
        eng = self._engine()
        idx = eng["idx"]
        k, q0, q1, p, scale, x, z = op
        m0, m1 = 1 << q0, 1 << q1
        psi = psi.copy()
        lo = idx[(idx & m0) == 0]
        if k == "x":
            psi[lo], psi[lo | m0] = psi[lo | m0].copy(), psi[lo].copy()
        elif k == "h":
            a, b = psi[lo].copy(), psi[lo | m0].copy()
            psi[lo], psi[lo | m0] = (a + b) / math.sqrt(2), (a - b) / math.sqrt(2)
        elif k == "ry":
            c, s = math.cos(ang / 2), math.sin(ang / 2)
            a, b = psi[lo].copy(), psi[lo | m0].copy()
            psi[lo], psi[lo | m0] = c * a - s * b, s * a + c * b
        elif k == "rz":
            psi[lo] *= complex(math.cos(ang / 2), -math.sin(ang / 2))
            psi[lo | m0] *= complex(math.cos(ang / 2), math.sin(ang / 2))
        elif k == "cx":
            sel = idx[((idx & m0) != 0) & ((idx & m1) == 0)]
            psi[sel], psi[sel | m1] = psi[sel | m1].copy(), psi[sel].copy()
        elif k == "cz":
            psi[((idx & m0) != 0) & ((idx & m1) != 0)] *= -1
        elif k == "pauli_rot":                         # exp(-i a P) = cos a - i sin a P
            psi = math.cos(ang) * psi - 1j * math.sin(ang) * eng["pauli"](psi, x, z)
        return psi

    def _prepare(self, theta):
        import numpy as np
        eng = self._engine()
        psi = np.zeros(eng["N"], dtype=complex)
        psi[self.init] = 1
        for op in self.ops:
            psi = self._apply(psi, op, op[4] * theta[op[3]] if op[0] in PARAM_KINDS else 0.0)
        return psi

    def exact(self, thetas):
        """<psi(theta)|H|psi(theta)> for each parameter vector, from the state vector."""
        import numpy as np
        eng = self._engine()
        out = []
        for theta in thetas:
            psi = self._prepare(theta)
            out.append(eng["const"] + float(np.real(np.vdot(psi, eng["hpsi"](psi)))))
        return out

    def grad(self, theta):
        """(E, dE/dtheta) by adjoint differentiation: one forward pass, one backward pass."""
        import numpy as np
        eng = self._engine()
        psi = self._prepare(theta)
        lam = eng["hpsi"](psi)
        e = eng["const"] + float(np.real(np.vdot(psi, lam)))
        g = [0.0] * self.params
        idx = eng["idx"]
        for op in reversed(self.ops):
            k, q0, q1, p, scale, x, z = op
            ang = scale * theta[p] if k in PARAM_KINDS else 0.0
            if k in PARAM_KINDS:                       # 2 Re <lambda| -i G |phi>
                if k == "pauli_rot":
                    d = 2 * float(np.real(np.vdot(lam, -1j * eng["pauli"](psi, x, z))))
                elif k == "ry":                        # -iY: |0> -> |1>, |1> -> -|0>; G = Y/2
                    m0 = 1 << q0
                    lo = idx[(idx & m0) == 0]
                    v = np.zeros_like(psi)
                    v[lo], v[lo | m0] = -psi[lo | m0], psi[lo]
                    d = float(np.real(np.vdot(lam, v)))
                else:                                  # -iZ; G = Z/2
                    zs = 1 - 2 * ((idx >> q0) & 1)
                    d = float(np.real(np.vdot(lam, -1j * zs * psi)))
                g[p] += scale * d
            psi = self._apply(psi, op, -ang)
            lam = self._apply(lam, op, -ang)
        return e, g

    # ---------------------------------------------------------------- measured
    def groups(self):
        """The terms in qubit-wise commuting groups (greedy): [(basis masks (x, z), [(x, z, c), ...])]."""
        gs = []
        for x, z, c in self.terms:
            if not (x | z):
                continue                                   # the identity: added to the constant
            for g in gs:
                gx, gz, ts = g
                both = (x | z) & (gx | gz)
                if ((x ^ gx) | (z ^ gz)) & both == 0:      # the same Pauli on every shared qubit
                    g[0], g[1] = gx | x, gz | z
                    ts.append((x, z, c))
                    break
            else:
                gs.append([x, z, [(x, z, c)]])
        return gs

    def identity(self):
        return self.constant + sum(c for x, z, c in self.terms if not (x | z))

    def circuits(self):
        """The ansatz with a ParameterVector, and one measured circuit per group."""
        from qiskit import QuantumCircuit
        from qiskit.circuit import ParameterVector
        from qiskit.circuit.library import PauliEvolutionGate
        from qiskit.quantum_info import Pauli
        ang = ParameterVector("angle", len(self.rotations()))     # one parameter per rotation: see angles()
        a = QuantumCircuit(self.n)
        for q in range(self.n):
            if self.init >> q & 1:
                a.x(q)
        r = 0
        for k, q0, q1, p, scale, x, z in self.ops:
            if k in ("x", "h"):
                getattr(a, k)(q0)
            elif k in TWO_QUBIT:
                getattr(a, k)(q0, q1)
            elif k in ("ry", "rz"):
                getattr(a, k)(ang[r], q0)
                r += 1
            else:
                a.append(PauliEvolutionGate(Pauli(label(self.n, x, z)), time=ang[r]), range(self.n))
                r += 1
        out = []
        for gx, gz, ts in self.groups():
            qc = QuantumCircuit(self.n, self.n)
            qc.compose(a, inplace=True)
            for q in range(self.n):
                xb, zb = gx >> q & 1, gz >> q & 1
                if xb and zb:                              # Y: measure after S^dagger H
                    qc.sdg(q)
                    qc.h(q)
                elif xb:
                    qc.h(q)
            qc.measure(range(self.n), range(self.n))
            out.append((qc, ts))
        return a, out


def _parity(v):
    import numpy as np
    v = v.copy()
    p = np.zeros_like(v)
    while v.any():
        p ^= v & 1
        v >>= 1
    return p


class Measured:
    """A problem on a backend of qcpu_run.py: transpiled once (QVLOAD), then one Sampler job per batch."""

    def __init__(self, problem, cfg, note):
        import qcpu_run
        from qiskit import transpile
        self.p, self.cfg, self.note, self.session = problem, cfg, note, None
        try:
            self.dev, self.where = qcpu_run.backend_for(cfg, problem.n)
        except qcpu_run.Error as e:
            raise VqeError(str(e))
        _, groups = problem.circuits()
        level = int(cfg["ibm"].get("optimization_level", 1))
        self.isa = transpile([qc for qc, _ in groups], self.dev, optimization_level=level, seed_transpiler=1)
        self.terms = [ts for _, ts in groups]
        self.two_qubit = max((sum(v for k, v in t.count_ops().items() if k in ("cx", "cz", "ecr", "rzz"))
                              for t in self.isa), default=0)
        # readout correction: on a noisy backend each job also measures all-0 and all-1 on the same physical qubits
        backend = cfg["backend"]
        self.mitigate = (backend in ("preview", "ibm") and problem.n <= 16
                         and cfg.get(backend, {}).get("readout_mitigation", True))
        self.layouts, self.cal = [], []
        if self.mitigate:
            from qiskit import QuantumCircuit
            for t in self.isa:
                lay = tuple(t.layout.final_index_layout()[:problem.n])
                if lay not in self.layouts:
                    self.layouts.append(lay)
                    for ones in (False, True):
                        qc = QuantumCircuit(problem.n, problem.n)
                        if ones:
                            qc.x(range(problem.n))
                        qc.measure(range(problem.n), range(problem.n))
                        self.cal.append(transpile(qc, self.dev, initial_layout=list(lay), optimization_level=0))

    def open_session(self):
        if self.cfg["backend"] != "ibm" or self.session is not None:
            return "nothing to reserve" if self.cfg["backend"] != "ibm" else "already open"
        from qiskit_ibm_runtime import Session
        try:
            self.session = Session(backend=self.dev)
            return f"session {getattr(self.session, 'session_id', None) or ''} on {self.dev.name}".strip()
        except Exception as e:                            # the Open Plan has no sessions: jobs then queue one by one
            return f"no session ({type(e).__name__}: {e}); each job queues on its own"

    def close_session(self):
        if self.session is not None:
            try:
                self.session.close()
            except Exception:
                pass
            self.session = None

    def energies(self, angles, shots):
        """(energies, standard errors, job id, seconds) for a batch of angle vectors (Problem.angles): one job."""
        import numpy as np
        from qiskit_ibm_runtime import SamplerV2
        shots = int(shots or self.cfg["shots"])
        thetas = angles
        nrot = len(self.p.rotations())
        values = np.array(angles, dtype=float).reshape(len(angles), nrot)
        pubs = [(t, values) if nrot else (t,) for t in self.isa] + [(c,) for c in self.cal]
        t0 = time.time()
        job = SamplerV2(mode=self.session or self.dev).run(pubs, shots=shots)
        job_id = job.job_id() if hasattr(job, "job_id") else None
        if self.cfg["backend"] == "ibm":
            self.note(f"job {job_id} sent to {self.dev.name} ({len(thetas)} parameter vectors x {len(pubs)} "
                      f"measurement bases), waiting for it")
        results = list(job.result())
        n, N = self.p.n, 1 << self.p.n
        inverse = {}                                      # layout -> per-qubit inverse readout matrices
        for j, lay in enumerate(self.layouts):
            zero, one = (self.probabilities(results[len(self.isa) + 2 * j + o], self.cal[2 * j + o])
                         for o in (0, 1))
            mats = []
            for q in range(n):
                e0 = sum(p for b, p in enumerate(zero) if b >> q & 1)          # read 1, prepared 0
                e1 = sum(p for b, p in enumerate(one) if not b >> q & 1)       # read 0, prepared 1
                a = np.array([[1 - e0, e1], [e0, 1 - e1]])
                mats.append(np.linalg.inv(a) if abs(np.linalg.det(a)) > 0.2 else np.eye(2))
            inverse[lay] = mats
        b = np.arange(N)
        e = np.full(len(thetas), self.p.identity())
        var = np.zeros(len(thetas))
        for t, ts, r in zip(self.isa, self.terms, results):
            lay = tuple(t.layout.final_index_layout()[:n]) if self.mitigate else None
            vals = sum(c * (1 - 2 * _parity(b & (x | z))) for x, z, c in ts)       # per basis outcome
            for i in range(len(thetas)):
                p = self.probabilities(r, t, i if nrot else None)
                if lay in inverse:
                    p = p.reshape([2] * n)
                    for q, m in enumerate(inverse[lay]):
                        ax = n - 1 - q                                         # axis of bit q
                        p = np.moveaxis(np.tensordot(m, p, axes=([1], [ax])), 0, ax)
                    p = p.reshape(N)
                mean = float((p * vals).sum())
                e[i] += mean
                var[i] += max(float((p * vals * vals).sum()) - mean * mean, 0.0) / shots
        return list(e), list(np.sqrt(var)), job_id, round(time.time() - t0, 1)

    def sample(self, angles, shots):
        """(outcomes, job id, seconds): the ansatz at one angle vector, measured in the Z basis (QVSAMPLE)."""
        import random
        import numpy as np
        from qiskit import transpile
        from qiskit_ibm_runtime import SamplerV2
        if not hasattr(self, "zisa"):
            a, _ = self.p.circuits()
            qc = a.copy()
            qc.measure_all()
            level = int(self.cfg["ibm"].get("optimization_level", 1))
            self.zisa = transpile(qc, self.dev, optimization_level=level, seed_transpiler=1)
        nrot = len(self.p.rotations())
        pub = (self.zisa, np.array([angles], dtype=float)) if nrot else (self.zisa,)
        t0 = time.time()
        job = SamplerV2(mode=self.session or self.dev).run([pub], shots=int(shots))
        job_id = job.job_id() if hasattr(job, "job_id") else None
        r = job.result()[0]
        bits = getattr(r.data, self.zisa.cregs[0].name)
        counts = bits.get_counts(loc=0) if nrot else bits.get_counts()
        out = [int(k, 2) for k, c in counts.items() for _ in range(c)]
        random.Random(0).shuffle(out)
        return out[:int(shots)], job_id, round(time.time() - t0, 1)

    def probabilities(self, result, circuit, loc=None):
        """The measured distribution over the 2^n outcomes (bit q = qubit q)."""
        import numpy as np
        bits = getattr(result.data, circuit.cregs[0].name)
        counts = bits.get_counts(loc=loc) if loc is not None else bits.get_counts()
        p = np.zeros(1 << self.p.n)
        for k, w in counts.items():
            p[int(k, 2)] += w
        return p / p.sum()
