// SPDX-License-Identifier: Apache-2.0
// Copyright 2026 Quantag IT Solutions GmbH
//
// VQE for a molecule from tools/qarm_molecule.py: UCCSD from the Hartree-Fock state. The optimizer
// (include/qarm_opt.h): L-BFGS on the energy and its gradient (QEXPECT, QGRAD) when the energies are exact, SPSA
// (one QEXPECTN batch per iteration) when they are measured, which shows as a standard error; or as named:
//   molecule_vqe [lbfgs | spsa [iterations]]
// Default: LiH, 10 qubits, 24 parameters (examples/qarm/lih.h); another molecule: -DMOLECULE='"h2.h"'.
// Docs: docs/qarm_v2.md, section 6.
//
//   clang -O1 -iquote include -iquote examples/qarm examples/qarm/molecule_vqe.c -o molecule_vqe -lm
//   ./molecule_vqe                              a plain CPU: exact, the state vector of include/qarm_sv.h
//   python3 qarm_exec.py molecule_vqe           Q-ARM (arm64 build)
//   qemu-aarch64 -plugin libqarm-qemu.so ./molecule_vqe        Q-ARM under QEMU (docs/qarm_v2.md, section 8)
//   cc -O2 -DQARM_RUNTIME -iquote include -iquote examples/qarm examples/qarm/molecule_vqe.c
//       runtime/libqarm/*.c -o molecule_vqe -lcurl -lm       native, with libqarm (section 7)
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#ifndef MOLECULE
#define MOLECULE "lih.h"
#endif
#include MOLECULE
#include "qarm_opt.h"

static void report(int iteration, double energy, double gradient, void *user)
{
    (void)user;
    printf("iteration %3d: E = %.9f Ha, |gradient| = %.2e\n", iteration, energy, gradient);
}

int main(int argc, char **argv)
{
    static double theta[QARM_MOLECULE_PARAMS];             /* zeros: the Hartree-Fock state */
    qarm_vqe v;
    int where = qarm_vqe_load(&molecule_h, &molecule_ansatz, &v);
    if (where < 0)
        return 2;
    printf("%s: %d qubits, %d parameters (UCCSD), %s\n", QARM_MOLECULE_NAME, QARM_MOLECULE_QUBITS,
           QARM_MOLECULE_PARAMS, where ? "energies from the quantum coprocessor (Q-ARM)" : "exact energies on this CPU");
    printf("Hartree-Fock:  %.9f Ha\n", QARM_MOLECULE_E_HF);
    qarm_session(1);
    double err = 0;
    qarm_expect(&v, theta, &err);                          /* a standard error: measured energies */
    int spsa = argc > 1 ? !strcmp(argv[1], "spsa") : err > 0;
    double e;
    if (spsa) {
        qarm_spsa_options o = QARM_SPSA_DEFAULTS;
        if (argc > 2) o.iterations = atoi(argv[2]);
        printf("optimizer: SPSA, %d iterations, %d random directions each (one batch per iteration)\n",
               o.iterations, o.resamplings);
        e = qarm_spsa(&v, theta, &o, report, 0);
    } else {
        printf("optimizer: L-BFGS with QGRAD\n");
        e = qarm_minimize(&v, theta, 200, 1e-6, report, 0);
    }
    qarm_session(0);
    qarm_vqe_free(&v);
    printf("VQE:           %.9f Ha\n", e);
    printf("exact (CASCI): %.9f Ha\n", QARM_MOLECULE_E_REF);
    double diff = (e - QARM_MOLECULE_E_REF) * 1000;
    if (diff > -5e-7 && diff < 5e-7)
        diff = 0;                                           /* not -0.000000 */
    printf("VQE - exact:   %.6f mHa (chemical accuracy: 1.6 mHa)\n", diff);
    printf("correlation energy recovered: %.2f%%\n",
           100 * (e - QARM_MOLECULE_E_HF) / (QARM_MOLECULE_E_REF - QARM_MOLECULE_E_HF));
    return 0;
}
