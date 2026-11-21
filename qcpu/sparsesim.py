# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Quantag IT Solutions GmbH
"""Exact sparse state-vector simulator for circuits made of X, CX, CCX, CSWAP (permutations of basis
states) plus a few H, Z and controlled-phase gates.

The quantum ARM machine is a permutation circuit: it maps each basis state to one basis state. A
superposition over k input qubits therefore stays a superposition of at most 2^k basis states however
many qubits the machine has, and H/Z/phase gates on a few qubits keep it small. The simulator stores
exactly those basis states and their complex amplitudes, so it computes the full quantum state --
including interference between branches -- without approximation. It is checked against Qiskit's
dense Statevector on random circuits and against Qiskit Aer on the interpreter (tests/test_grover.py,
tests/test_shor.py); Aer's matrix-product-state method is much slower when several input qubits are
superposed.
"""
import numpy as np

PERMUTATION = frozenset(("x", "cx", "ccx", "cswap"))


class SparseState:
    def __init__(self, n, ones=()):
        self.n = n
        self.bits = np.zeros((1, n), dtype=np.uint8)      # one row per basis state with nonzero amplitude
        self.bits[0, list(ones)] = 1
        self.amp = np.ones(1, dtype=complex)

    # --- permutation gates: act on every stored basis state at once
    def x(self, t): self.bits[:, t] ^= 1
    def cx(self, c, t): self.bits[:, t] ^= self.bits[:, c]
    def ccx(self, a, b, t): self.bits[:, t] ^= self.bits[:, a] & self.bits[:, b]

    def cswap(self, c, a, b):
        m = self.bits[:, c] == 1
        ta = self.bits[m, a].copy()
        self.bits[m, a] = self.bits[m, b]
        self.bits[m, b] = ta

    # --- phase gates: change amplitudes only
    def z(self, t):
        self.amp = np.where(self.bits[:, t] == 1, -self.amp, self.amp)

    def mcz(self, qubits):
        """Phase -1 on the basis states where all `qubits` are 1."""
        self.amp = np.where(np.all(self.bits[:, qubits] == 1, axis=1), -self.amp, self.amp)

    def cp(self, theta, a, b):
        """Controlled phase: e^(i theta) on the basis states where qubits a and b are both 1."""
        both = (self.bits[:, a] & self.bits[:, b]) == 1
        self.amp = np.where(both, self.amp * np.exp(1j * theta), self.amp)

    def h(self, t):
        b0 = self.bits.copy(); b0[:, t] = 0
        b1 = self.bits.copy(); b1[:, t] = 1
        s = 1 / np.sqrt(2)
        sign = np.where(self.bits[:, t] == 1, -1.0, 1.0)
        self._merge(np.vstack([b0, b1]), np.concatenate([self.amp * s, self.amp * s * sign]))

    def _merge(self, bits, amp):
        """Add the amplitudes of equal basis states (interference) and drop the ones that cancel."""
        packed = np.packbits(bits, axis=1)
        uniq, inv = np.unique(packed, axis=0, return_inverse=True)
        acc = np.zeros(len(uniq), dtype=complex)
        np.add.at(acc, inv.ravel(), amp)
        keep = np.abs(acc) > 1e-12
        self.bits = np.unpackbits(uniq[keep], axis=1, count=self.n).astype(np.uint8)
        self.amp = acc[keep]

    def apply(self, gates, inverse=False, fast=True):
        """gates: list of (name, args...) as in QuantumARM.g, plus ("h", q), ("z", q), ("cp", theta, a, b).
        inverse=True applies the inverse circuit: reversed order, phases negated (the other gates are
        self-inverse). A list of permutation gates only is run by the bit-parallel routine below."""
        if fast and len(gates) > 64 and all(g[0] in PERMUTATION for g in gates):
            return self._permute(reversed(gates) if inverse else gates)
        for g in (reversed(gates) if inverse else gates):
            if g[0] == "cp" and inverse:
                self.cp(-g[1], g[2], g[3])
            else:
                getattr(self, g[0])(*g[1:])

    def _permute(self, gates):
        """The same permutation gates as x/cx/ccx/cswap above, but with each qubit held as one Python
        integer whose bit r is the qubit's value in row r. One gate is then one integer operation on
        all rows. Row order and amplitudes do not change. Checked against the plain methods in
        tests/test_shor.py."""
        rows = len(self.amp)
        nb = (rows + 7) // 8
        cols = np.packbits(self.bits, axis=0, bitorder="little")           # (nb, n)
        s = [int.from_bytes(cols[:, q].tobytes(), "little") for q in range(self.n)]
        ones = (1 << rows) - 1
        for g in gates:
            op = g[0]
            if op == "ccx":
                s[g[3]] ^= s[g[1]] & s[g[2]]
            elif op == "cx":
                s[g[2]] ^= s[g[1]]
            elif op == "x":
                s[g[1]] ^= ones
            else:                                                          # cswap c, a, b
                d = (s[g[2]] ^ s[g[3]]) & s[g[1]]
                s[g[2]] ^= d
                s[g[3]] ^= d
        buf = np.frombuffer(b"".join(v.to_bytes(nb, "little") for v in s), dtype=np.uint8).reshape(self.n, nb)
        self.bits = np.ascontiguousarray(np.unpackbits(buf, axis=1, count=rows, bitorder="little").T)

    # --- reading the state
    def values(self, qubits):
        """Integer read from `qubits` (qubits[0] = bit 0) in every row."""
        w = 1 << np.arange(len(qubits), dtype=np.int64)
        return self.bits[:, qubits].astype(np.int64) @ w

    def probabilities(self, qubits):
        """Exact distribution of the integer read from `qubits`."""
        p = {}
        for v, a in zip(self.values(qubits).tolist(), np.abs(self.amp) ** 2):
            p[v] = p.get(v, 0.0) + float(a)
        return p

    def sample(self, qubits, shots, seed=None):
        rng = np.random.default_rng(seed)
        idx = rng.choice(len(self.amp), size=shots, p=np.abs(self.amp) ** 2 / np.sum(np.abs(self.amp) ** 2))
        w = 1 << np.arange(len(qubits))
        vals = self.bits[idx][:, qubits].astype(np.int64) @ w
        u, c = np.unique(vals, return_counts=True)
        return dict(zip(u.tolist(), c.tolist()))

    def split(self, qubits):
        """Partial trace by cases. Groups the rows by the value of all qubits NOT in `qubits` and returns
        one (unnormalised) SparseState on len(qubits) qubits per group; qubit i of a part is qubits[i].

        If every later gate and measurement acts only on `qubits`, the parts never interfere with each
        other (they differ on untouched qubits), so the outcome distribution is the sum of the parts'
        distributions. This is exact, and avoids carrying thousands of idle qubits through those gates."""
        qubits = list(qubits)
        rest = np.ones(self.n, dtype=bool)
        rest[qubits] = False
        if rest.any():
            packed = np.packbits(self.bits[:, rest], axis=1)
            _, inv = np.unique(packed, axis=0, return_inverse=True)
            inv = inv.ravel()
        else:
            inv = np.zeros(len(self.amp), dtype=np.int64)
        parts = []
        for g in range(inv.max() + 1):
            sel = inv == g
            p = SparseState(len(qubits))
            p.bits = np.ascontiguousarray(self.bits[sel][:, qubits])
            p.amp = self.amp[sel].copy()
            parts.append(p)
        return parts
