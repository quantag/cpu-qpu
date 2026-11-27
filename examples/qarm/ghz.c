/* SPDX-License-Identifier: Apache-2.0
 * Copyright 2026 Quantag IT Solutions GmbH
 *
 * QCIRC, QRAND, QCAPS: a circuit given as a C table, sampled where the program runs.
 *   - a 5-qubit GHZ state: only 00000 and 11111 come out, half each;
 *   - a Toffoli truth table: each input prepared with X gates, CCX, measured;
 *   - 16 random bytes (quantum on a real device, pseudo-random elsewhere), and what the device is.
 * Plain build: the C state vector in qarm.h. arm64: packets (qarm_exec.py, the QEMU plugin). -DQARM_RUNTIME: libqarm,
 * which sends the circuits to IBM Quantum when QARM_CONFIG says backend ibm. */
#include <stdio.h>
#include "qarm.h"

#define SHOTS 2000
static uint64_t samples[SHOTS];

static const qarm_gate ghz_gates[] = {
    {QARM_G_H, 0, 0, 0, 0, 0},  {QARM_G_CX, 0, 1, 0, 0, 0}, {QARM_G_CX, 1, 2, 0, 0, 0},
    {QARM_G_CX, 2, 3, 0, 0, 0}, {QARM_G_CX, 3, 4, 0, 0, 0},
};
static const qarm_circuit ghz = {5, 5, ghz_gates};

int main(void)
{
    qarm_capabilities caps;
    qarm_caps(&caps);
    printf("device: %s, %u qubits, flags %u%s\n", caps.backend, caps.qubits, caps.flags,
           caps.flags & QARM_CAPS_HARDWARE ? " (hardware)" : "");

    int where = qarm_run(&ghz, SHOTS, samples);
    if (where < 0) { printf("GHZ refused\n"); return 1; }
    int zeros = 0, ones = 0;
    for (int s = 0; s < SHOTS; s++) zeros += samples[s] == 0, ones += samples[s] == 31;
    printf("GHZ(5): 00000 %d, 11111 %d, other %d of %d shots (%s)\n", zeros, ones, SHOTS - zeros - ones, SHOTS,
           where == 1 ? "on the coprocessor" : "simulated here");

    printf("Toffoli:");
    int good = 0;
    for (int in = 0; in < 4; in++) {
        qarm_gate g[3];
        int n = 0;
        if (in & 1) g[n++] = (qarm_gate){QARM_G_X, 0, 0, 0, 0, 0};
        if (in & 2) g[n++] = (qarm_gate){QARM_G_X, 1, 0, 0, 0, 0};
        g[n++] = (qarm_gate){QARM_G_CCX, 0, 1, 2, 0, 0};
        qarm_circuit c = {3, (uint32_t)n, g};
        qarm_run(&c, 200, samples);
        int hits = 0;
        for (int s = 0; s < 200; s++) hits += samples[s] == (uint64_t)(in | (in == 3 ? 4 : 0));
        printf(" %d%d->%d %d%%", in & 1, in >> 1, in == 3, hits / 2);
        good += hits;
    }
    printf("\n");

    unsigned char r[16];
    int q = qarm_rand(r, 16);
    printf("random (%s):", q == 1 ? "quantum hardware" : "pseudo-random");
    for (int i = 0; i < 16; i++) printf(" %02x", r[i]);
    printf("\n");
    return zeros + ones < SHOTS / 2 || good < 400;
}
