# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Quantag IT Solutions GmbH
"""Q-ARM v2 VQE instructions (QVLOAD, QEXPECT, QEXPECTN, QSESSION, QVFREE; docs/qarm_v2.md section 6).

  1. examples/qarm/h2_vqe.c: on the plain CPU (the C state vector of include/qarm.h) and under qarm_exec.py (the
     runtime's exact simulator) the same output, and the ground state of H2, -1.137306 Ha, to 1e-6.
  2. Every ansatz operation (X, H, CX, CZ, RY, RZ, Pauli rotations) and Y terms, 3 qubits: the C path and the
     runtime agree to 1e-9; with qiskit-aer, the measured energies agree with the exact ones within 5 standard errors
     (the Qiskit translation of the tables, with its basis changes).
  3. Errors: inconsistent tables, too many qubits for the exact simulator: QVLOAD refused, the reason on stderr.
  4. (qiskit, qiskit-aer, qiskit-ibm-runtime) h2_vqe on IBM's noise model of Fez, with readout correction, within
     0.1 Ha; through the "ibm" backend's local channel: a session and one job per batch.
Run: python3 -m tests.test_qarm_vqe   (needs an arm64 C compiler: the host's on arm64, else aarch64-linux-gnu-gcc)"""
import json, os, re, subprocess, sys, tempfile, textwrap
from tests.test_qarm_exec import ROOT, EXEC, toolchain, sh

H2 = os.path.join(ROOT, "examples", "qarm", "h2_vqe.c")
EXACT = -1.1373060357534

ALL_OPS = r"""
#include <stdio.h>
#include "qarm.h"
static const qarm_pauli terms[] = {
    {0, 1, 0.31}, {2, 0, -0.42}, {4, 4, 0.27}, {3, 3, 0.19}, {5, 2, -0.23}, {7, 0, 0.11}, {0, 0, 0.5},
};
static const qarm_hamiltonian h = {3, 7, -0.75, terms};
static const qarm_op ops[] = {
    {QARM_H, 0, 0, 0, 0, 0, 0, 0},       {QARM_RY, 1, 0, 0, 0, 1.0, 0, 0},
    {QARM_CX, 0, 2, 0, 0, 0, 0, 0},      {QARM_RZ, 2, 0, 1, 0, 0.5, 0, 0},
    {QARM_CZ, 1, 2, 0, 0, 0, 0, 0},      {QARM_PAULI_ROT, 0, 0, 2, 0, 1.0, 3, 1},   /* Y0 X1 */
    {QARM_X, 1, 0, 0, 0, 0, 0, 0},       {QARM_PAULI_ROT, 0, 0, 0, 0, -0.5, 4, 6},  /* Z1 Y2 */
    {QARM_RY, 2, 0, 1, 0, 2.0, 0, 0},
};
static const qarm_ansatz a = {3, 3, 9, 0, 1, ops};
int main(void) {
    qarm_vqe v;
    if (qarm_vqe_load(&h, &a, &v) < 0) return 2;
    double th[4][3] = {{0.3, -1.1, 0.7}, {2.0, 0.4, -0.9}, {-0.6, 1.7, 2.5}, {0, 0, 0}}, e[4], err;
    qarm_expect_n(&v, &th[0][0], 4, e);
    for (int i = 0; i < 4; i++) printf("%.12f\n", e[i]);
    double one = qarm_expect(&v, th[1], &err);
    printf("%.12f %.6f\n", one, err);
    qarm_vqe_free(&v);
    return 0;
}
"""


def build(cc, src, exe):
    sh(cc + ["-O1", "-iquote", os.path.join(ROOT, "include"), src, "-o", exe, "-lm"])


def main():
    try:
        import unicorn, numpy, lief  # noqa: F401
    except ImportError as e:
        print(f"skipped: {e.name} is not installed")
        return
    tc = toolchain()
    if not tc:
        print("skipped: no arm64 C compiler (cc on arm64, or aarch64-linux-gnu-gcc)")
        return
    cc, native = tc
    with tempfile.TemporaryDirectory() as d:
        exe = os.path.join(d, "h2_vqe")
        build(cc, H2, exe)
        p = sh(EXEC + ["-q", exe])
        assert p.stdout.startswith("H2 VQE, energies from the quantum coprocessor (Q-ARM)\n"), p.stdout
        e = float(re.search(r"\) = (-?\d+\.\d+) Ha", p.stdout).group(1))
        assert abs(e - EXACT) < 1e-6, p.stdout
        if native is not None:
            q = sh(native + [exe])
            assert q.stdout == p.stdout.replace("energies from the quantum coprocessor (Q-ARM)",
                                                "exact energies on this CPU"), (q.stdout, p.stdout)
        print(f"1) h2_vqe: E = {e:.6f} Ha (exact {EXACT:.6f}) under qarm_exec.py"
              + ("; the same output on the plain CPU, from the C state vector" if native is not None else ""))

        src = os.path.join(d, "all_ops.c")
        open(src, "w").write(ALL_OPS)
        exe2 = os.path.join(d, "all_ops")
        build(cc, src, exe2)
        p = sh(EXEC + ["-q", exe2])
        runtime = [float(v) for v in p.stdout.split()]
        assert len(runtime) == 6 and runtime[1] == runtime[4] and runtime[5] == 0, p.stdout
        if native is not None:
            c = [float(v) for v in sh(native + [exe2]).stdout.split()]
            assert all(abs(a - b) < 1e-9 for a, b in zip(c, runtime)), (c, runtime)
        how = ""
        try:
            import qiskit, qiskit_aer, qiskit_ibm_runtime  # noqa: F401
        except ImportError as err:
            how = f"; Aer skipped ({err.name} is not installed)"
        else:
            cfg = os.path.join(d, "aer.json")
            json.dump({"backend": "simulator", "mode": "sampler", "shots": 40000}, open(cfg, "w"))
            p = sh(EXEC + ["-q", "--config", cfg, exe2])
            measured = [float(v) for v in p.stdout.split()]
            stderr = measured[5]
            assert 0 < stderr < 0.02, p.stdout
            worst = max(abs(a - b) for a, b in zip(measured[:5], runtime[:5]))
            assert worst < 5 * stderr, (measured, runtime)
            how = f"; measured with qiskit-aer: within {worst / stderr:.1f} standard errors"
        print("2) all operations, Y terms, 3 qubits: the C path and the runtime agree to 1e-9" + how)

        bad = os.path.join(d, "bad.c")
        open(bad, "w").write(textwrap.dedent("""\
            #include <stdio.h>
            #include "qarm.h"
            static const qarm_pauli t[] = {{0, 1, 1.0}};
            static const qarm_hamiltonian h = {QUBITS, 1, 0, t};
            static const qarm_op ops[] = {{QARM_RY, OPQ, 0, 0, 0, 1.0, 0, 0}};
            static const qarm_ansatz a = {QUBITS, 1, 1, 0, 0, ops};
            int main(void) { qarm_vqe v; printf("%d\\n", qarm_vqe_load(&h, &a, &v)); return 0; }
            """))
        for qubits, opq, reason in ((2, 5, r"ansatz operation 0 \(ry\): qubits 5, 0 of 2"),
                                    (30, 0, r"30 qubits: the exact simulator takes up to 20")):
            sh(cc + ["-O1", f"-DQUBITS={qubits}", f"-DOPQ={opq}", "-iquote", os.path.join(ROOT, "include"), bad,
                     "-o", exe2, "-lm"])
            p = sh(EXEC + [exe2])
            assert p.stdout == "-1\n" and re.search("VQE instruction refused: " + reason, p.stderr), p.stderr
        print("3) errors: an operation on a qubit beyond the Hamiltonian, 30 qubits on the exact simulator: QVLOAD "
              "refused with the reason")

        if how.startswith("; Aer skipped"):
            print("4) skipped: Qiskit is not installed")
        else:
            cfg = os.path.join(d, "preview.json")
            json.dump({"backend": "preview", "mode": "sampler", "shots": 4000, "options": {"device": "fez"}},
                      open(cfg, "w"))
            p = sh(EXEC + ["--config", cfg, exe])
            e = float(re.search(r"\) = (-?\d+\.\d+) Ha", p.stdout).group(1))
            assert "readout correction" in p.stderr and abs(e - EXACT) < 0.1, (p.stdout, p.stderr)
            assert len(re.findall(r"QEXPECTN: 17 energies \(job ", p.stderr)) == 4, p.stderr
            cfg = os.path.join(d, "local.json")
            json.dump({"backend": "ibm", "mode": "sampler", "shots": 2000,
                       "options": {"channel": "local", "device": "fake_torino"}}, open(cfg, "w"))
            q = sh(EXEC + ["--config", cfg, exe])
            jobs = re.findall(r"job (\S+) sent to fake_torino", q.stderr)
            assert "QSESSION: session" in q.stderr and len(jobs) == 5, q.stderr
            e2 = float(re.search(r"\) = (-?\d+\.\d+) Ha", q.stdout).group(1))
            print(f"4) h2_vqe on the noise model of Fez: E = {e:.4f} Ha ({(e - EXACT) * 1000:+.0f} mHa), readout "
                  f"corrected; ibm, local channel (fake_torino): a session, {len(jobs)} jobs, E = {e2:.4f} Ha")
    print("PASS")


if __name__ == "__main__":
    main()
