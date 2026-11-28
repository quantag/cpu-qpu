# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Quantag IT Solutions GmbH
"""QEVOLVE for qarm_exec.py (docs/qarm_isa.md 6.6): the plans and error bounds of include/qarm_sv.h, in numpy.

check(e) -> None or the reason a table is refused; plan(e, exact_ok) -> Plan or the reason; run(e, plan, seed) ->
(values, standard error). e is an Evolution, read from the program's memory by read_evolution."""
import math
import random
import struct

import numpy as np

MAX_K, MAX_TERMS, EPS, SEQUENCES, TAYLOR, MAX_STEPS, WORK = 4096, 1000000, 1e-3, 100, 40, 1e12, 2e10
EXACT, PRODUCT, QDRIFT = 1, 2, 3
LAYOUT = struct.Struct("<QQQIIIIQdII")                 # qarm_evolution: 64 bytes


class Ham:
    def __init__(self, qubits, constant, terms):
        self.qubits, self.constant, self.terms = qubits, constant, terms     # terms: [(x, z, c)]


class Evolution:
    def __init__(self, h, o, t, method, order, shots, init, eps, steps, reserved):
        self.h, self.o, self.t, self.method, self.order = h, o, t, method, order
        self.shots, self.init, self.eps, self.steps, self.reserved = shots, init, eps, steps, reserved


class Plan:
    def __init__(self, method, order=0, steps=0, sequences=0, bound=0.0, work=0.0, ops=0.0):
        self.method, self.order, self.steps, self.sequences = method, order, steps, sequences
        self.bound, self.work, self.ops = bound, work, ops


def read_ham(read, addr):
    n, nt = struct.unpack("<II", read(addr, 8))
    c, tp = struct.unpack("<dQ", read(addr + 8, 16))
    if nt > MAX_TERMS:
        raise ValueError(f"{nt} terms")
    raw = read(tp, 24 * nt) if nt else b""
    return Ham(n, c, [struct.unpack_from("<QQd", raw, 24 * i) for i in range(nt)])


def read_evolution(read, addr):
    """the table at addr; ValueError when it, its Hamiltonians or its times cannot be read"""
    h, o, t, k, method, order, shots, init, eps, steps, reserved = LAYOUT.unpack(read(addr, LAYOUT.size))
    if not 1 <= k <= MAX_K:
        raise ValueError(f"k = {k}: 1 to 4096")
    if not h or not o:
        raise ValueError("a Hamiltonian is missing")
    times = list(struct.unpack(f"<{k}d", read(t, 8 * k)))
    return Evolution(read_ham(read, h), read_ham(read, o), times, method, order, shots, init, eps, steps, reserved)


def _mask_ok(m, n):
    return n >= 64 or not m >> n


def check(e):
    n = e.h.qubits
    if not 1 <= n <= 64:
        return "qubits: 1 to 64"
    if e.o.qubits != n:
        return "the observable's qubits differ from the generator's"
    for h in (e.h, e.o):
        if not math.isfinite(h.constant):
            return "a coefficient is not finite"
        for x, z, c in h.terms:
            if not _mask_ok(x | z, n):
                return "a Pauli term has a qubit at or above qubits"
            if not math.isfinite(c):
                return "a coefficient is not finite"
    if not _mask_ok(e.init, n):
        return "init has a bit at or above qubits"
    if not all(math.isfinite(t) for t in e.t):
        return "a time is not finite"
    if e.method > QDRIFT:
        return "unknown method"
    if e.method == PRODUCT:
        if e.order not in (0, 1, 2, 4, 6):
            return "order: 1, 2, 4, 6, or 0 for the runtime's choice"
    elif e.order:
        return "order is for product formulas only"
    if not (e.eps >= 0 and math.isfinite(e.eps)):
        return "eps: finite, 0 or more"
    if e.reserved:
        return "reserved must be 0"
    return None


def tail(x, p):
    """sum over n > p of x^n / n!"""
    term = 1.0
    for n in range(1, p + 2):
        term *= x / n
    s, n = term, p + 2
    while n < 100000:
        term *= x / n
        s += term
        if n > x and term <= s * 1e-17:
            break
        n += 1
    return s * (1 + 1e-12)


def norm(h):
    return sum(abs(c) for x, z, c in h.terms if x or z)


def _odd(v):
    return bin(v).count("1") & 1


def _anti(a, b):
    return _odd((a[0] & b[1]) ^ (a[1] & b[0]))


def comm1(h):
    t = h.terms
    if len(t) > 4096:
        return math.inf
    return sum(2 * abs(t[j][2] * t[k][2]) for j in range(len(t)) for k in range(j + 1, len(t)) if _anti(t[j], t[k]))


def comm2(h):
    t = h.terms
    if len(t) > 400:
        return math.inf, math.inf
    c0 = c1 = 0.0
    for j, a in enumerate(t):
        if not (a[0] or a[1]):
            continue
        for k in range(j + 1, len(t)):
            b = t[k]
            if not _anti(a, b):
                continue
            ab = 2 * abs(a[2] * b[2])
            p = (a[0] ^ b[0], a[1] ^ b[1])
            c1 += 2 * abs(a[2]) * ab
            c0 += sum(2 * abs(t[l][2]) * ab for l in range(j + 1, len(t)) if _anti(t[l], p))
    return c0, c1


def upsilon(p):
    u = 1.0
    for q in range(4, p + 1, 2):
        u *= 8 / (4 - 4 ** (1.0 / (q - 1))) - 1
    return u


def obs(omega, d):
    if d >= 2:
        return 2 * omega
    return min(omega * (2 + d) * d, 2 * omega)


def product_d(lam, comm, p, t, r):
    t = abs(t)
    if lam == 0 or t == 0:
        return 0.0
    d = 2 * r * tail(upsilon(p) * lam * t / r, p)
    if comm < math.inf and p in (1, 2):
        d = min(d, (t * t / (2 * r) if p == 1 else t * t * t / (r * r)) * comm * (1 + 1e-12))
    return d


def _exp(x):
    """e^x, infinite instead of OverflowError: a bound that large only means more steps are needed"""
    return math.exp(x) if x < 709 else math.inf


def qdrift_d(lam, t, n):
    t = abs(t)
    if lam == 0 or t == 0:
        return 0.0
    x = lam * t / n
    return 4 * x * x * n * _exp(2 * x) * (1 + 1e-12)


def exact_d(lam, t):
    t = abs(t)
    if lam == 0 or t == 0:
        return 0.0, 0
    if lam * t / 2 > MAX_STEPS:                       # also when it overflows to infinity
        return math.inf, int(MAX_STEPS)
    m = math.ceil(lam * t / 2)
    tau = tail(lam * t / m, TAYLOR)
    y = m * math.log1p(tau)
    return (math.expm1(y) if y < 709 else math.inf) * (1 + 1e-12), m


def least(bound, eps):
    hi = 1
    while bound(hi) > eps:
        if hi >= MAX_STEPS:
            return 0
        hi *= 2
    lo = hi // 2
    if lo < 1:
        return 1
    while hi - lo > 1:
        mid = (lo + hi) // 2
        if bound(mid) <= eps:
            hi = mid
        else:
            lo = mid
    return hi


def plan(e, exact_ok, limit=WORK):
    """a Plan, or the reason the evolution is refused (a str)"""
    h = e.h
    N, lam, omega = 2.0 ** h.qubits, norm(h), norm(e.o)
    L = sum(1 for x, z, c in h.terms if x or z)
    tmax = max(abs(t) for t in e.t)
    tpos, tneg = max([t for t in e.t if t > 0], default=0.0), max([-t for t in e.t if t < 0], default=0.0)
    eps = e.eps if e.eps > 0 else EPS
    method = e.method or (EXACT if exact_ok else PRODUCT)
    if method == EXACT:
        if not exact_ok:
            return "exact evolution is not available on this backend"
        (d1, m1), (d2, m2) = exact_d(lam, tpos), exact_d(lam, tneg)
        p = Plan(EXACT, steps=max(m1, m2), bound=obs(omega, max(d1, d2)))
        p.work = ((m1 + m2) * TAYLOR * (L + 1) + len(e.t) * (len(e.o.terms) + 1)) * N
    elif method == PRODUCT:
        mult = {1: 1, 2: 2, 4: 10, 6: 50}
        c1, (a, b) = comm1(h), comm2(h)
        best, p = math.inf, None
        for q in (1, 2, 4, 6):
            if e.order and e.order != q:
                continue
            comm = c1 if q == 1 else a / 12 + b / 24 if q == 2 else math.inf
            bound = lambda r: obs(omega, product_d(lam, comm, q, tmax, r))
            r = e.steps or least(bound, eps)
            if not r:
                continue
            cost = r * mult[q] * max(L, 1)
            if cost < best:
                best, p = cost, Plan(PRODUCT, q, r, bound=bound(r), ops=mult[q] * L)
        if p is None:
            return "no product formula meets eps within 10^12 steps"
        p.work = best * len(e.t) * N
    else:
        bound = lambda n: obs(omega, qdrift_d(lam, tmax, n))
        n = e.steps or least(bound, eps)
        if not n:
            return "qDRIFT cannot meet eps within 10^12 samples"
        p = Plan(QDRIFT, steps=n, sequences=e.shots or SEQUENCES, bound=bound(n))
        p.work = n * p.sequences * len(e.t) * N
    if p.work > limit:
        return "the evolution would take more work than this runtime allows"
    return p


def formula(terms, p, f=1.0):
    """the rotations (x, z, angle per unit dt) of one step of S_p, adjacent equal terms merged"""
    out = []

    def emit(x, z, a):
        if out and out[-1][0] == x and out[-1][1] == z:
            out[-1][2] += a
        else:
            out.append([x, z, a])

    def rec(p, f):
        if p <= 2:
            for half in range(2 if p == 2 else 1):
                for x, z, c in (reversed(terms) if half else terms):
                    if x or z:
                        emit(x, z, (f / 2 if p == 2 else f) * c)
            return
        u = 1 / (4 - 4 ** (1.0 / (p - 1)))
        for c in (u, u, 1 - 4 * u, u, u):
            rec(p - 2, f * c)

    rec(p, f)
    return [tuple(v) for v in out]


class _Sv:
    def __init__(self, n):
        self.n, self.idx, self.sign = n, np.arange(1 << n, dtype=np.uint64), {}

    def s(self, z):
        if z not in self.sign:
            v = self.idx & np.uint64(z)
            par = np.zeros(len(v), dtype=np.uint8)
            while v.any():
                par ^= (v & np.uint64(1)).astype(np.uint8)
                v = v >> np.uint64(1)
            self.sign[z] = 1.0 - 2.0 * par
        return self.sign[z]

    def pauli(self, psi, x, z):               # P psi, P = i^popcount(x & z) X^x Z^z
        return (1j) ** (bin(x & z).count("1") & 3) * (self.s(z) * psi)[self.idx ^ np.uint64(x)]

    def rot(self, psi, x, z, a):              # exp(-i a P) psi
        return math.cos(a) * psi - 1j * math.sin(a) * self.pauli(psi, x, z)

    def hpsi(self, h, psi):                   # (H - constant) psi
        out = np.zeros_like(psi)
        for x, z, c in h.terms:
            if x or z:
                out += c * self.pauli(psi, x, z)
            else:
                out += c * psi
        return out

    def energy(self, h, psi):
        return float(np.real(np.vdot(psi, self.hpsi(h, psi)))) + h.constant


def run(e, p, seed=None):
    """(values, standard error) by the plan"""
    h, o, n = e.h, e.o, e.h.qubits
    sv, k, out, se = _Sv(n), len(e.t), [0.0] * len(e.t), 0.0
    basis = np.zeros(1 << n, dtype=complex)
    basis[e.init] = 1
    if p.method == EXACT:
        lam = norm(h)
        order = sorted(range(k), key=lambda i: e.t[i])
        for sign in (1, -1):
            psi, cur = basis.copy(), 0.0
            for i in (order if sign > 0 else reversed(order)):
                t = e.t[i]
                if (t >= 0) != (sign > 0):
                    continue
                T = t - cur
                if lam and T:
                    m = math.ceil(lam * abs(T) / 2)
                    dt = T / m
                    for _ in range(m):
                        acc, term = psi.copy(), psi
                        for j in range(1, TAYLOR + 1):
                            term = (-1j * dt / j) * sv.hpsi(h, term)
                            acc += term
                        psi = acc
                cur = t
                out[i] = sv.energy(o, psi)
    elif p.method == PRODUCT:
        step = formula(h.terms, p.order)
        for i, t in enumerate(e.t):
            psi = basis.copy()
            if t:
                dt = t / p.steps
                rots = []
                for _ in range(p.steps):
                    for x, z, a in step:
                        if rots and rots[-1][0] == x and rots[-1][1] == z:
                            rots[-1][2] += a * dt
                        else:
                            rots.append([x, z, a * dt])
                for x, z, a in rots:
                    if a:
                        psi = sv.rot(psi, x, z, a)
            out[i] = sv.energy(o, psi)
    else:
        rng = random.Random(seed)
        lam = norm(h)
        terms = [(x, z, c) for x, z, c in h.terms if (x or z) and c]
        weights = [abs(c) for _, _, c in terms]
        for i, t in enumerate(e.t):
            vals = []
            tau = lam * t / p.steps
            for _ in range(p.sequences):
                psi = basis.copy()
                if terms and t:
                    for x, z, c in rng.choices(terms, weights, k=p.steps):
                        psi = sv.rot(psi, x, z, tau if c > 0 else -tau)
                vals.append(sv.energy(o, psi))
            out[i] = float(np.mean(vals))
            if len(vals) > 1:
                se = max(se, float(np.std(vals, ddof=1)) / math.sqrt(len(vals)))
    return out, se


def ansatz_tables(e, p):
    """the plan's product formula as qarm_vqe.Problem's input: (read, h_addr, a_addr) for O and r steps, theta[0] = dt"""
    ops = []
    for _ in range(p.steps):
        for x, z, a in formula(e.h.terms, p.order):
            if ops and ops[-1][1] == x and ops[-1][2] == z:
                ops[-1][0] += a
            else:
                ops.append([a, x, z])
    o = e.o
    blob = bytearray()
    h_addr = 0
    blob += struct.pack("<IIdQ", o.qubits, len(o.terms), o.constant, 24)
    for x, z, c in o.terms:
        blob += struct.pack("<QQd", x, z, c)
    a_addr = len(blob)
    blob += struct.pack("<IIIIQQ", o.qubits, 1, len(ops), 0, e.init, a_addr + 32)
    for a, x, z in ops:
        blob += struct.pack("<BBBBIdQQ", 7, 0, 0, 0, 0, a, x, z)
    data = bytes(blob)
    return (lambda addr, n: data[addr:addr + n]), h_addr, a_addr, len(ops)
