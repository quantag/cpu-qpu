#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Quantag IT Solutions GmbH
"""A molecule as a C header for Q-ARM's VQE (docs/qarm_v2.md, section 6): build time only, the program needs no Python.

    python3 tools/qarm_molecule.py --name lih --atom "Li 0 0 0; H 0 0 1.5949" --basis sto-3g --active 2,5 \\
        -o examples/qarm/lih.h

PySCF computes the molecular integrals (RHF, then the active space: --active ELECTRONS,ORBITALS, the other
orbitals frozen). The electronic Hamiltonian is mapped to qubits with Jordan-Wigner, spin orbitals in blocks
(alpha 0..m-1, beta m..2m-1). The ansatz is UCCSD from the Hartree-Fock state: each single and double excitation
T - T^dagger is a sum of commuting Pauli strings, so exp(theta (T - T^dagger)) is exactly a product of Pauli
rotations, one parameter per excitation. The header holds:
    molecule_h, molecule_ansatz      the qarm_hamiltonian and qarm_ansatz tables of include/qarm.h
    QARM_MOLECULE_E_HF, _E_REF       the Hartree-Fock energy and the exact energy in the active space (CASCI)
Checks before writing: the qubit Hamiltonian's lowest energy among states with the molecule's electron counts equals
CASCI (to 1e-8), its energy in the Hartree-Fock state equals RHF's, and each excitation's Pauli strings commute."""
import argparse
import itertools
import sys

import numpy as np

# Pauli operators as {(x, z): coefficient} in the X^x Z^z form; converted to qarm.h's form (Y = both bits) at the end


def pc(v):
    return bin(v).count("1")


def mul(a, b):
    out = {}
    for (x1, z1), c1 in a.items():
        for (x2, z2), c2 in b.items():
            key = (x1 ^ x2, z1 ^ z2)
            c = c1 * c2 * (-1 if pc(z1 & x2) & 1 else 1)       # Z^z1 X^x2 = (-1)^(z1.x2) X^x2 Z^z1
            out[key] = out.get(key, 0) + c
    return out


def add(acc, op, scale=1.0):
    for k, c in op.items():
        acc[k] = acc.get(k, 0) + scale * c


def ladder(j, dagger):
    """a_j^dagger = Z_<j (X_j - iY_j)/2 = (X^ej Z^m + X^ej Z^(m|ej))/2; a_j: the second term negated."""
    e, m = 1 << j, (1 << j) - 1
    return {(e, m): 0.5, (e, m | e): 0.5 if dagger else -0.5}


def to_y_form(op, tol=1e-12):
    """{(x, z): c} in qarm.h's form P(x, z) = i^popcount(x&z) X^x Z^z."""
    out = {}
    for (x, z), c in op.items():
        c = c * (-1j) ** (pc(x & z) & 3)
        if abs(c) > tol:
            out[(x, z)] = c
    return out


def hamiltonian(h1, h2, ecore, m):
    """Jordan-Wigner of sum h_pq a+_p a_q + 1/2 sum (pq|rs) a+_p a+_r a_s a_q over spins; real coefficients."""
    up = {(p, s): ladder(p + s * m, True) for p in range(m) for s in (0, 1)}
    dn = {(p, s): ladder(p + s * m, False) for p in range(m) for s in (0, 1)}
    H = {(0, 0): ecore}
    for s in (0, 1):
        for p, q in itertools.product(range(m), repeat=2):
            if abs(h1[p, q]) > 1e-12:
                add(H, mul(up[p, s], dn[q, s]), h1[p, q])
    for s, t in itertools.product((0, 1), repeat=2):
        for p, q, r, u in itertools.product(range(m), repeat=4):
            v = h2[p, q, r, u]
            if abs(v) < 1e-12 or (s == t and p == r) or (s == t and q == u):
                continue
            add(H, mul(mul(up[p, s], up[r, t]), mul(dn[u, t], dn[q, s])), 0.5 * v)
    H = to_y_form(H)
    for k, c in H.items():
        if abs(c.imag) > 1e-9:
            raise SystemExit(f"internal error: a complex Hamiltonian coefficient {c} for {k}")
    return {k: c.real for k, c in H.items() if abs(c.real) > 1e-10}


def uccsd(m, na, nb):
    """[(Pauli rotations [(x, z, scale)], label)] per excitation; exp(theta A) = prod exp(-i scale theta P)."""
    occ = [list(range(na)), [m + i for i in range(nb)]]
    vir = [list(range(na, m)), [m + i for i in range(nb, m)]]
    spin = lambda o: o >= m
    excitations = []
    for s in (0, 1):
        for i in occ[s]:
            for a in vir[s]:
                excitations.append(((a,), (i,)))
    all_occ, all_vir = occ[0] + occ[1], vir[0] + vir[1]
    for i, j in itertools.combinations(all_occ, 2):
        for a, b in itertools.combinations(all_vir, 2):
            if sorted((spin(i), spin(j))) == sorted((spin(a), spin(b))):
                excitations.append(((a, b), (j, i)))
    out = []
    for cre, ann in excitations:
        T = {(0, 0): 1.0}
        for c in cre:
            T = mul(T, ladder(c, True))
        for a in ann:
            T = mul(T, ladder(a, False))
        Td = {(0, 0): 1.0}                                   # T^dagger: the reverse product, daggers swapped
        for a in reversed(ann):
            Td = mul(Td, ladder(a, True))
        for c in reversed(cre):
            Td = mul(Td, ladder(c, False))
        A = dict(T)
        add(A, Td, -1.0)
        A = to_y_form(A)
        rots = []
        for (x, z), c in sorted(A.items()):
            if abs(c.real) > 1e-9:
                raise SystemExit(f"internal error: T - T^dagger has a real coefficient {c}")
            rots.append((x, z, -c.imag))                     # exp(i r theta P) = exp(-i (-r) theta P)
        for (x1, z1, _), (x2, z2, _) in itertools.combinations(rots, 2):
            if (pc(x1 & z2) + pc(z1 & x2)) & 1:
                raise SystemExit("internal error: the Pauli strings of an excitation do not commute")
        name = "+".join(map(str, cre)) + "<-" + "+".join(map(str, ann))
        out.append((rots, name))
    return out


def sector_ground(H, n, m, na, nb):
    """The lowest eigenvalue of H among basis states with na alpha and nb beta electrons."""
    import scipy.sparse as sp
    import scipy.sparse.linalg as sla
    amask = (1 << m) - 1
    basis = [b for b in range(1 << n) if pc(b & amask) == na and pc(b >> m) == nb]
    index = {b: i for i, b in enumerate(basis)}
    rows, cols, vals = [], [], []
    for (x, z), c in H.items():
        ph = 1j ** (pc(x & z) & 3)
        for b in basis:
            t = b ^ x
            if t in index:
                rows.append(index[t])
                cols.append(index[b])
                vals.append(c * ph * (-1 if pc(b & z) & 1 else 1))
    M = sp.csr_matrix((vals, (rows, cols)), shape=(len(basis), len(basis)))
    if len(basis) <= 400:
        return float(np.linalg.eigvalsh(M.toarray()).min())
    return float(sla.eigsh(M, k=1, which="SA")[0][0])


def build(args):
    from pyscf import ao2mo, gto, mcscf, scf
    mol = gto.M(atom=args.atom, basis=args.basis, charge=args.charge, spin=args.spin, unit="Angstrom", verbose=0)
    mf = scf.RHF(mol).run() if args.spin == 0 else scf.ROHF(mol).run()
    if args.active:
        ne, m = (int(v) for v in args.active.split(","))
    else:
        ne, m = mol.nelectron, mf.mo_coeff.shape[1]
    mc = mcscf.CASCI(mf, m, ne)
    e_ref = float(mc.kernel()[0])
    h1, ecore = mc.get_h1eff()
    h2 = ao2mo.restore(1, mc.get_h2eff(), m)
    na, nb = (ne + args.spin) // 2, (ne - args.spin) // 2
    n = 2 * m
    if n > 64:
        raise SystemExit(f"{n} qubits: qarm.h's tables take up to 64")
    H = hamiltonian(h1, h2, float(ecore), m)
    init = sum(1 << i for i in range(na)) | sum(1 << (m + i) for i in range(nb))
    e_hf = H.get((0, 0), 0.0) + sum(c * (-1 if pc(init & z) & 1 else 1) for (x, z), c in H.items() if x == 0 and z)
    if abs(e_hf - mf.e_tot) > 1e-7:
        raise SystemExit(f"check failed: the Hartree-Fock state's energy {e_hf} != RHF {mf.e_tot}")
    if n <= args.check_max_qubits:
        e0 = sector_ground(H, n, m, na, nb)
        if abs(e0 - e_ref) > 1e-8:
            raise SystemExit(f"check failed: the qubit Hamiltonian's ground energy {e0} != CASCI {e_ref}")
    exc = uccsd(m, na, nb)
    if len(exc) > 255:
        raise SystemExit(f"{len(exc)} excitations: qarm.h takes up to 255 parameters")
    return dict(mol=mol, mf=mf, m=m, ne=ne, n=n, na=na, nb=nb, H=H, init=init, e_hf=e_hf, e_ref=e_ref, exc=exc,
                checked=n <= args.check_max_qubits)


def header(args, r):
    const = r["H"].get((0, 0), 0.0)
    terms = sorted(((x, z, c) for (x, z), c in r["H"].items() if x or z), key=lambda t: (t[0], t[1]))
    ops = [(x, z, s, p) for p, (rots, _) in enumerate(r["exc"]) for (x, z, s) in rots]
    name = args.name
    lines = [
        "/* SPDX-License-Identifier: Apache-2.0",
        " * Copyright 2026 Quantag IT Solutions GmbH",
        " *",
        f" * {name}: generated by tools/qarm_molecule.py; do not edit. Regenerate with:",
        f" *   python3 tools/qarm_molecule.py --name {name} --atom \"{args.atom}\" --basis {args.basis}"
        + (f" --active {args.active}" if args.active else "") + (f" --charge {args.charge}" if args.charge else "")
        + (f" --spin {args.spin}" if args.spin else ""),
        " *",
        f" * {r['n']} qubits (Jordan-Wigner, alpha spin orbitals 0..{r['m'] - 1}, beta {r['m']}..{r['n'] - 1}), "
        f"{len(terms)} Pauli terms;",
        f" * UCCSD from the Hartree-Fock state: {len(r['exc'])} parameters, {len(ops)} Pauli rotations.",
        f" * Active space: {r['ne']} electrons in {r['m']} orbitals"
        + (f" of {r['mf'].mo_coeff.shape[1]}" if args.active else " (all)") + ".",
        f" * Checked: the energy of the Hartree-Fock state equals RHF's"
        + ("; the lowest energy with these electron counts equals CASCI's" if r["checked"] else "") + ". */",
        "#include \"qarm.h\"",
        "",
        f"#define QARM_MOLECULE_NAME \"{name}\"",
        f"#define QARM_MOLECULE_QUBITS {r['n']}",
        f"#define QARM_MOLECULE_PARAMS {len(r['exc'])}",
        f"#define QARM_MOLECULE_E_HF {r['e_hf']:.12f}      /* Hartree-Fock, Ha */",
        f"#define QARM_MOLECULE_E_REF {r['e_ref']:.12f}     /* exact in the active space (CASCI), Ha */",
        "",
        "static const qarm_pauli molecule_terms[] = {",
    ]
    lines += [f"    {{{x:#x}ULL, {z:#x}ULL, {c:.17g}}}," for x, z, c in terms]
    lines += ["};",
              f"static const qarm_hamiltonian molecule_h = {{{r['n']}, {len(terms)}, {const:.17g}, molecule_terms}};",
              "",
              "/* one Pauli rotation per line: exp(-i scale theta[param] P(x, z)) */",
              "static const qarm_op molecule_ops[] = {"]
    lines += [f"    {{QARM_PAULI_ROT, 0, 0, {p}, 0, {s:.17g}, {x:#x}ULL, {z:#x}ULL}}," for x, z, s, p in ops]
    lines += ["};",
              f"static const qarm_ansatz molecule_ansatz = {{{r['n']}, {len(r['exc'])}, {len(ops)}, 0, {r['init']:#x}ULL,"
              " molecule_ops};", ""]
    return "\n".join(lines)


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--name", required=True, help="a name for the molecule, as in the header")
    ap.add_argument("--atom", required=True, help='PySCF geometry in Angstrom, e.g. "H 0 0 0; H 0 0 0.735"')
    ap.add_argument("--basis", default="sto-3g")
    ap.add_argument("--active", help="ELECTRONS,ORBITALS of the active space (default: all)")
    ap.add_argument("--charge", type=int, default=0)
    ap.add_argument("--spin", type=int, default=0, help="2S: unpaired electrons")
    ap.add_argument("--check-max-qubits", type=int, default=20, help="the CASCI check up to this many qubits")
    ap.add_argument("-o", "--output", help="the header (default: stdout)")
    args = ap.parse_args(argv)
    try:
        import pyscf  # noqa: F401
    except ImportError:
        raise SystemExit("tools/qarm_molecule.py needs PySCF: pip install pyscf")
    r = build(args)
    text = header(args, r)
    if args.output:
        open(args.output, "w").write(text)
    else:
        sys.stdout.write(text)
    print(f"{args.name}: {r['n']} qubits, {len(r['H']) - 1} terms, {len(r['exc'])} parameters; "
          f"E(HF) = {r['e_hf']:.6f}, E(CASCI) = {r['e_ref']:.6f} Ha" + ("" if r["checked"] else " (not checked)"),
          file=sys.stderr)


if __name__ == "__main__":
    main()
