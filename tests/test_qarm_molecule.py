# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Quantag IT Solutions GmbH
"""Molecules for Q-ARM's VQE: tools/qarm_molecule.py, QGRAD, include/qarm_opt.h (docs/qarm_v2.md, section 6).

  1. (PySCF) The generator, run again for H2 and LiH: its own checks pass (the qubit Hamiltonian's ground energy
     equals CASCI, the Hartree-Fock state's equals RHF) and the committed headers have the same energies and sizes.
  2. examples/qarm/molecule_vqe.c (L-BFGS, UCCSD) for LiH (10 qubits, 24 parameters) and H2 on the plain CPU: the
     exact energy in the active space (CASCI) to 1e-6 Ha, far inside chemical accuracy (1.6 mHa).
  3. QGRAD against central finite differences (LiH, 24 parameters) on the plain CPU and, with libqarm (exact), natively.
  4. The arm64 build under qarm_exec.py: the same output as the plain CPU (QGRAD by adjoint differentiation in Python).
  5. QGRAD by the parameter-shift rule on measured backends, H2: libqarm's simulator and (Qiskit) qarm_exec.py with Aer,
     against the exact gradient.
Run: python3 -m tests.test_qarm_molecule"""
import json, os, re, subprocess, sys, tempfile
from tests.test_libqarm import compiler, has_curl, sh as host_sh, INC, LIB, EXE
from tests.test_qarm_exec import ROOT, EXEC, toolchain, sh

EX = os.path.join(ROOT, "examples", "qarm")
MOLECULES = {"h2": ("H 0 0 0; H 0 0 0.735", None), "lih": ("Li 0 0 0; H 0 0 1.5949", "2,5")}

GRAD = r"""
#include <stdio.h>
#include <math.h>
#include MOLECULE
int main(void) {
    qarm_vqe v;
    if (qarm_vqe_load(&molecule_h, &molecule_ansatz, &v) < 0) return 2;
    double th[QARM_MOLECULE_PARAMS], g[QARM_MOLECULE_PARAMS], worst = 0;
    for (int i = 0; i < QARM_MOLECULE_PARAMS; i++) th[i] = 0.1 * sin(1.7 * i + 0.3);
    double e = qarm_grad(&v, th, g);
    for (int i = 0; i < QARM_MOLECULE_PARAMS && FD; i++) {
        double h = 1e-5, t = th[i];
        th[i] = t + h; double ep = qarm_expect(&v, th, 0);
        th[i] = t - h; double em = qarm_expect(&v, th, 0); th[i] = t;
        if (fabs((ep - em) / (2 * h) - g[i]) > worst) worst = fabs((ep - em) / (2 * h) - g[i]);
    }
    printf("%.12f %.3e\n", e, worst);
    for (int i = 0; i < QARM_MOLECULE_PARAMS; i++) printf("%.9f\n", g[i]);
    return 0;
}
"""


def macro(text, name):
    return float(re.search(rf"#define {name} (\S+)", text).group(1))


def result(out):
    e = float(re.search(r"VQE:\s+(-?\d+\.\d+) Ha", out).group(1))
    ref = float(re.search(r"exact \(CASCI\):\s+(-?\d+\.\d+) Ha", out).group(1))
    return e, ref


def main():
    try:
        import pyscf  # noqa: F401
        with tempfile.TemporaryDirectory() as d:
            for name, (atom, active) in MOLECULES.items():
                out = os.path.join(d, name + ".h")
                cmd = [sys.executable, os.path.join(ROOT, "tools", "qarm_molecule.py"), "--name", name, "--atom", atom,
                       "-o", out] + (["--active", active] if active else [])
                sh(cmd)
                new, old = open(out).read(), open(os.path.join(EX, name + ".h")).read()
                for m in ("QARM_MOLECULE_QUBITS", "QARM_MOLECULE_PARAMS", "QARM_MOLECULE_E_HF", "QARM_MOLECULE_E_REF"):
                    assert abs(macro(new, m) - macro(old, m)) < 1e-8, (name, m)
                assert new.count("ULL}") == old.count("ULL}") and new.count("QARM_PAULI_ROT") == old.count("QARM_PAULI_ROT")
        print("1) tools/qarm_molecule.py: H2 and LiH regenerated, its checks pass (CASCI, RHF), the committed headers agree")
    except ImportError:
        print("1) skipped: PySCF is not installed (pip install pyscf)")

    cc = compiler()
    if not cc:
        print("skipped: no C compiler")
        return
    with tempfile.TemporaryDirectory() as d:
        inc = ["-iquote", INC, "-iquote", EX]
        lm = [] if os.name == "nt" else ["-lm"]
        outs = {}
        for name in MOLECULES:
            exe = os.path.join(d, f"vqe_{name}{EXE}")
            host_sh(cc + ["-O1"] + inc + [f'-DMOLECULE="{name}.h"', os.path.join(EX, "molecule_vqe.c"), "-o", exe] + lm)
            outs[name] = host_sh([exe]).stdout
            e, ref = result(outs[name])
            assert abs(e - ref) < 1e-6, outs[name]
        iters = len(re.findall(r"^iteration", outs["lih"], re.M))
        print(f"2) molecule_vqe on the plain CPU: LiH (10 qubits, 24 parameters) to the CASCI energy "
              f"{result(outs['lih'])[1]:.6f} Ha in {iters - 1} L-BFGS iterations; H2 likewise")

        src = os.path.join(d, "grad.c")
        open(src, "w").write(GRAD)
        gexe = os.path.join(d, "grad" + EXE)
        host_sh(cc + ["-O1"] + inc + ['-DMOLECULE="lih.h"', "-DFD=1", src, "-o", gexe] + lm)
        worst = float(host_sh([gexe]).stdout.split()[1])
        assert worst < 1e-7, worst
        curl = has_curl(cc, d)
        nexe = os.path.join(d, "grad_native" + EXE)
        host_sh(cc + ["-O1", "-DQARM_RUNTIME"] + ([] if curl else ["-DQARM_NO_CURL"]) + inc
                + ['-DMOLECULE="lih.h"', "-DFD=1", src] + LIB + ["-o", nexe] + (["-lcurl"] if curl else []) + lm)
        nworst = float(host_sh([nexe], {"QARM_QUIET": "1"}).stdout.split()[1])
        assert nworst < 1e-7, nworst
        print(f"3) QGRAD (adjoint) against finite differences, LiH: {worst:.1e} on the plain CPU, {nworst:.1e} natively "
              "with libqarm")

        tc = toolchain()
        if tc:
            acc, _ = tc
            aexe = os.path.join(d, "vqe_lih_arm64")
            sh(acc + ["-O1"] + inc + [os.path.join(EX, "molecule_vqe.c"), "-o", aexe, "-lm"])
            p = sh(EXEC + ["-q", aexe])
            strip = lambda t: t.split("\n", 1)[1]
            assert strip(p.stdout) == strip(outs["lih"]), (p.stdout, outs["lih"])
            print("4) the arm64 build under qarm_exec.py: the same output as the plain CPU (QGRAD in Python, adjoint)")
        else:
            print("4) skipped: no arm64 C compiler")

        # 5: the parameter-shift rule on measured backends, H2 at one point, against the exact gradient
        h2g = os.path.join(d, "grad_h2" + EXE)
        host_sh(cc + ["-O1"] + inc + ['-DMOLECULE="h2.h"', "-DFD=0", src, "-o", h2g] + lm)
        exact = [float(v) for v in host_sh([h2g]).stdout.split()[2:]]
        h2n = os.path.join(d, "grad_h2_native" + EXE)
        host_sh(cc + ["-O1", "-DQARM_RUNTIME"] + ([] if curl else ["-DQARM_NO_CURL"]) + inc
                + ['-DMOLECULE="h2.h"', "-DFD=0", src] + LIB + ["-o", h2n] + (["-lcurl"] if curl else []) + lm)
        cfg = os.path.join(d, "sim.json")
        json.dump({"backend": "simulator", "shots": 200000}, open(cfg, "w"))
        p = host_sh([h2n], {"QARM_CONFIG": cfg, "QARM_SEED": "4"})
        shifted = [float(v) for v in p.stdout.split()[2:]]
        dev = max(abs(a - b) for a, b in zip(shifted, exact))
        assert "parameter-shift rule" in p.stderr and dev < 0.02, (shifted, exact, p.stderr)
        line = f"5) QGRAD by the parameter-shift rule, H2 (3 parameters, 12 rotations, 25 energies in one batch): " \
               f"libqarm's simulator within {dev:.4f} of the exact gradient"
        try:
            import qiskit, qiskit_aer, qiskit_ibm_runtime  # noqa: F401
            if tc:
                aexe = os.path.join(d, "grad_h2_arm64")
                sh(acc + ["-O1"] + inc + ['-DMOLECULE="h2.h"', "-DFD=0", src, "-o", aexe, "-lm"])
                acfg = os.path.join(d, "aer.json")
                json.dump({"backend": "simulator", "mode": "sampler", "shots": 200000}, open(acfg, "w"))
                p = sh(EXEC + ["--config", acfg, aexe])
                aer = [float(v) for v in p.stdout.split()[2:]]
                adev = max(abs(a - b) for a, b in zip(aer, exact))
                assert "parameter-shift rule, 24 shifted energies" in p.stderr and adev < 0.02, (aer, exact, p.stderr)
                line += f"; qarm_exec.py with Aer within {adev:.4f}"
        except ImportError as e:
            line += f" (Aer skipped: {e.name} is not installed)"
        print(line)
    print("PASS")


if __name__ == "__main__":
    main()
