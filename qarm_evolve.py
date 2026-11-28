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


def qdrift_tables(e, p, rng, sequences):
    """qDRIFT's random sequences as qarm_vqe.Problem inputs, one circuit each with theta[0] = t / N: yields
    (read, h_addr, a_addr) per sequence (terms drawn with probability |c| / lambda, scale sgn(c) lambda)"""
    terms = [(x, z, c) for x, z, c in e.h.terms if (x or z) and c]
    lam = norm(e.h)
    for _ in range(sequences):
        ops = []
        for x, z, c in (rng.choices(terms, [abs(c) for _, _, c in terms], k=p.steps) if terms else []):
            a = lam if c > 0 else -lam
            if ops and ops[-1][1] == x and ops[-1][2] == z:
                ops[-1][0] += a
            else:
                ops.append([a, x, z])
        yield _tables(e, ops)


def _tables(e, ops):
    o = e.o
    blob = bytearray(struct.pack("<IIdQ", o.qubits, len(o.terms), o.constant, 24))
    for x, z, c in o.terms:
        blob += struct.pack("<QQd", x, z, c)
    a_addr = len(blob)
    blob += struct.pack("<IIIIQQ", o.qubits, 1, len(ops), 0, e.init, a_addr + 32)
    for a, x, z in ops:
        blob += struct.pack("<BBBBIdQQ", 7, 0, 0, 0, 0, a, x, z)
    data = bytes(blob)
    return (lambda addr, n: data[addr:addr + n]), 0, a_addr



# ---------------------------------------------------------------- QLINDBLAD (docs/qarm_isa.md 6.7): as qarm_sv.h's
DM_QUBITS, TRAJECTORIES = 10, 100
LB_LAYOUT = struct.Struct("<QQQIIIIQdIIQ")              # qarm_lindblad: qarm_evolution, then the jumps: 72 bytes


class Lindblad:
    def __init__(self, e, jumps):
        self.e, self.jumps = e, jumps


class LbPlan:
    def __init__(self, method, order=0, steps=0, trajectories=0, dm=True, bound=0.0, work=0.0):
        self.method, self.order, self.steps, self.trajectories = method, order, steps, trajectories
        self.dm, self.bound, self.work = dm, bound, work
        self.sequences = 0


def read_lindblad(read, addr):
    f = LB_LAYOUT.unpack(read(addr, LB_LAYOUT.size))
    h, o, t, k, method, order, shots, init, eps, steps, reserved, j = f
    if not 1 <= k <= MAX_K:
        raise ValueError(f"k = {k}: 1 to 4096")
    if not h or not o or not j:
        raise ValueError("a Hamiltonian or the jump operators are missing")
    times = list(struct.unpack(f"<{k}d", read(t, 8 * k)))
    e = Evolution(read_ham(read, h), read_ham(read, o), times, method, order, shots, init, eps, steps, reserved)
    return Lindblad(e, read_ham(read, j))


def lb_check(l):
    e, j = l.e, l.jumps
    why = check(e)
    if why:
        return why
    if j.qubits != e.h.qubits:
        return "the jump operators' qubits differ from the generator's"
    for x, z, c in j.terms:
        if not _mask_ok(x | z, j.qubits):
            return "a Pauli term has a qubit at or above qubits"
        if not math.isfinite(c):
            return "a coefficient is not finite"
    if j.constant != 0:
        return "the jump operators' constant must be 0"
    if any(not c >= 0 for _, _, c in j.terms):
        return "a rate is negative"
    if e.method == QDRIFT:
        return "qDRIFT is for closed systems (QEVOLVE)"
    if e.method == PRODUCT and e.order > 2:
        return "orders 4 and 6 are not channels: order 1, 2 or 0"
    if any(t < 0 for t in e.t):
        return "a time is negative: open systems evolve forward"
    return None


def lb_rates(j):
    return sum(c for x, z, c in j.terms if x or z)


def lb_comm(h, j):
    if len(h.terms) + len(j.terms) > 4096:
        return math.inf
    c = 0.0
    for a, p in enumerate(h.terms):
        for q in h.terms[a + 1:]:
            if _anti(p, q):
                c += 2 * (2 * abs(p[2])) * (2 * abs(q[2]))
        for q in j.terms:
            if _anti(p, q):
                c += 2 * (2 * abs(p[2])) * (2 * q[2])
    return c


def lb_product_d(Lam, comm, p, t, r):
    if Lam == 0 or t == 0:
        return 0.0
    d = 2 * r * tail(Lam * t / r, p)
    if comm < math.inf and p == 1:
        d = min(d, t * t / (2 * r) * comm * (1 + 1e-12))
    return d


def lb_plan(l, sim, limit=WORK):
    """an LbPlan, or the reason (a str)"""
    e, h, j = l.e, l.e.h, l.jumps
    n = h.qubits
    N, lam, gam, omega = 2.0 ** n, norm(h), lb_rates(j), norm(e.o)
    Lam = 2 * lam + 2 * gam
    tmax = max(e.t)
    eps = e.eps if e.eps > 0 else EPS
    if not sim:
        return "QLINDBLAD runs on a simulator in this runtime"
    G = sum(1 for x, z, c in h.terms if x or z) + sum(1 for x, z, c in j.terms if x or z)
    dm = n <= DM_QUBITS
    method = e.method or (EXACT if dm else PRODUCT)
    if method == EXACT:
        if not dm:
            return "exact open-system evolution needs the density matrix: at most 10 qubits"
        if Lam * tmax / 2 > MAX_STEPS:
            return "the evolution would take more work than this runtime allows"
        m = math.ceil(Lam * tmax / 2) if Lam * tmax > 0 else 0
        y = m * math.log1p(tail(Lam * tmax / m, TAYLOR)) if m else 0.0
        d = (math.expm1(y) if y < 709 else math.inf) * (1 + 1e-12)
        p = LbPlan(EXACT, steps=m, bound=2 * omega if d >= 2 else omega * d)
        p.work = (m * TAYLOR * (G + 1) * 2 + len(e.t) * (len(e.o.terms) + 1)) * N * N
    else:
        comm, best, p = lb_comm(h, j), math.inf, None
        for q in (1, 2):
            if e.order and e.order != q:
                continue
            c = comm if q == 1 else math.inf

            def bound(r, q=q, c=c):
                d = lb_product_d(Lam, c, q, tmax, r)
                return 2 * omega if d >= 2 else omega * d
            r = e.steps or least(bound, eps)
            if not r:
                continue
            cost = r * q * max(G, 1)
            if cost < best:
                best, p = cost, LbPlan(PRODUCT, q, r, bound=bound(r), dm=dm)
        if p is None:
            return "no product formula meets eps within 10^12 steps"
        if not dm:
            p.trajectories = e.shots or TRAJECTORIES
        p.work = best * len(e.t) * (4 * N * N if dm else p.trajectories * N)
    if p.work > limit:
        return "the evolution would take more work than this runtime allows"
    return p


class _Dm:
    """density-matrix steps on rho, an N x N complex array (qarm_sv.h's qarm_dm_*)"""
    def __init__(self, n):
        self.sv = _Sv(n)
        self.idx = np.arange(1 << n)

    def gen(self, T, x, z, c, g):
        """-i c [P, T] + g (P T P - T)"""
        k = bin(x & z).count("1") & 3
        S = self.sv.s(z)
        perm = self.idx ^ x
        out = np.zeros_like(T)
        if c:
            PT = (1j ** k) * (S[:, None] * T)[perm, :]
            TP = (1j ** k) * T[:, perm] * S[None, :]
            out += -1j * c * (PT - TP)
        if g:
            PTP = (-1) ** k * S[perm][:, None] * T[perm][:, perm] * S[None, :]
            out += g * (PTP - T)
        return out

    def factor(self, rho, jump, x, z, a):
        if not a:
            return rho
        if jump:
            return rho + self.gen(rho, x, z, 0, -math.expm1(-2 * a) / 2)
        s, c = math.sin(a), math.cos(a)
        return rho + self.gen(rho, x, z, s * c, s * s)

    def expect(self, o, rho):
        v = 0j
        for x, z, c in o.terms:
            k = bin(x & z).count("1") & 3
            perm = self.idx ^ x
            v += c * (1j ** k) * np.sum(self.sv.s(z)[perm] * rho[perm, self.idx])
        return float(np.real(v)) + o.constant * float(np.real(np.trace(rho)))


def lb_formula(h, j, p, dt):
    """the generators of S_p(dt): (jump, x, z, a), H's terms then the jumps, Strang's halves palindromic"""
    f = dt / 2 if p == 2 else dt
    seq = [(0, x, z, f * c) for x, z, c in h.terms if x or z] + [(1, x, z, f * c) for x, z, c in j.terms if x or z]
    return seq + seq[::-1] if p == 2 else seq


def lb_run(l, p, seed=None):
    """(values, standard error) by the plan"""
    e, h, j = l.e, l.e.h, l.jumps
    n, k = h.qubits, len(e.t)
    out, se = [0.0] * k, 0.0
    if p.dm:
        d = _Dm(n)
        r0 = np.zeros((1 << n, 1 << n), dtype=complex)
        r0[e.init, e.init] = 1
        if p.method == EXACT:
            Lam = 2 * norm(h) + 2 * lb_rates(j)
            rho, cur = r0, 0.0
            for i in sorted(range(k), key=lambda i: e.t[i]):
                T = e.t[i] - cur
                if Lam and T > 0:
                    m = math.ceil(Lam * T / 2)
                    dt = T / m
                    for _ in range(m):
                        acc, term = rho.copy(), rho
                        for q in range(1, TAYLOR + 1):
                            nxt = np.zeros_like(term)
                            for x, z, c in h.terms:
                                if x or z:
                                    nxt += d.gen(term, x, z, c, 0)
                            for x, z, g in j.terms:
                                if x or z:
                                    nxt += d.gen(term, x, z, 0, g)
                            term = (dt / q) * nxt
                            acc = acc + term
                        rho = acc
                cur = e.t[i]
                out[i] = d.expect(e.o, rho)
        else:
            for i, t in enumerate(e.t):
                rho = r0.copy()
                if t > 0:
                    for _ in range(p.steps):
                        for jump, x, z, a in lb_formula(h, j, p.order, t / p.steps):
                            rho = d.factor(rho, jump, x, z, a)
                out[i] = d.expect(e.o, rho)
        return out, se
    rng = random.Random(seed)
    sv = _Sv(n)
    basis = np.zeros(1 << n, dtype=complex)
    basis[e.init] = 1
    for i, t in enumerate(e.t):
        vals = []
        for _ in range(p.trajectories):
            psi = basis.copy()
            if t > 0:
                for _ in range(p.steps):
                    for jump, x, z, a in lb_formula(h, j, p.order, t / p.steps):
                        if not jump:
                            if a:
                                psi = sv.rot(psi, x, z, a)
                        elif rng.random() < -math.expm1(-2 * a) / 2:
                            psi = sv.pauli(psi, x, z)
            vals.append(sv.energy(e.o, psi))
        out[i] = float(np.mean(vals))
        if len(vals) > 1:
            se = max(se, float(np.std(vals, ddof=1)) / math.sqrt(len(vals)))
    return out, se
