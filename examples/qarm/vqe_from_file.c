// SPDX-License-Identifier: Apache-2.0
// Copyright 2026 Quantag IT Solutions GmbH
//
// VQE with the Hamiltonian read from a file (examples/qarm/h2_terms.txt: "constant c", then "x z c" per term), using
// what an ordinary C program uses: fopen, fgets, sscanf, malloc, qsort, snprintf. qarm_exec.py's small C library
// does not have them; under QEMU (docs/qarm_v2.md, section 8) the real one does.
//
//   aarch64-linux-gnu-gcc -O1 -iquote include examples/qarm/vqe_from_file.c -o vqe_from_file -lm
//   qemu-aarch64 -L /usr/aarch64-linux-gnu -plugin libqarm-qemu.so ./vqe_from_file examples/qarm/h2_terms.txt
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include "qarm.h"
static int cmp(const void *a, const void *b) { double x = *(const double *)a, y = *(const double *)b; return (x > y) - (x < y); }
int main(int argc, char **argv) {
    const char *path = argc > 1 ? argv[1] : "examples/qarm/h2_terms.txt";
    FILE *f = fopen(path, "r"); if (!f) { perror(path); return 1; }
    qarm_pauli *t = malloc(64 * sizeof *t); int n = 0; double constant = 0; char line[256];
    while (n < 64 && fgets(line, sizeof line, f)) {
        if (!strncmp(line, "constant", 8)) { constant = strtod(line + 8, NULL); continue; }
        unsigned long long x, z; double c;
        if (sscanf(line, "%llu %llu %lf", &x, &z, &c) == 3) t[n++] = (qarm_pauli){x, z, c};
    }
    fclose(f);
    qarm_hamiltonian h = {2, (uint32_t)n, constant, t};
    static const qarm_op ops[] = {{QARM_RY, 0, 0, 0, 0, 1.0, 0, 0}, {QARM_X, 1, 0, 0, 0, 0, 0, 0}, {QARM_CX, 0, 1, 0, 0, 0, 0, 0}};
    qarm_ansatz a = {2, 1, 3, 0, 0, ops};
    qarm_vqe v; if (qarm_vqe_load(&h, &a, &v) < 0) return 2;
    int k = 64; double *th = malloc(k * sizeof *th), *e = malloc(k * sizeof *e);
    for (int i = 0; i < k; i++) th[i] = -3.14159265 + 6.2831853 * i / k;
    if (qarm_expect_n(&v, th, k, e) < 0) return 3;
    qsort(e, k, sizeof *e, cmp);
    char out[128]; snprintf(out, sizeof out, "%d terms from %s; lowest of %d energies: %.6f Ha", n, path, k, e[0]);
    puts(out); free(t); free(th); free(e); qarm_vqe_free(&v); return 0;
}
