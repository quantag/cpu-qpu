// SPDX-License-Identifier: Apache-2.0
// Copyright 2026 Quantag IT Solutions GmbH
//
// Ordinary C, with no quantum calls: a count and an average written as plain loops. The QArmAuto compiler pass
// (compiler/, docs/qarm_auto.md) gives them to the quantum coprocessor as estimates, QCOUNT (quantum counting) and
// QAMPEST (amplitude estimation); QARM_ESTIMATE is the only change to the source, and qarm_auto_error() the
// bound of each estimate. On a CPU without the coprocessor every loop runs as written: exact, bound 0.
//
//   qarm-cc --auto examples/qarm/estimate.c -o estimate     (or clang -O1 -fpass-plugin=QArmAuto.so ... compiler/qarm_auto.c)
//   ./estimate                                            a plain CPU: exact counts, bound 0
//   qarm-run ./estimate                                   Q-ARM: [qarm: QCOUNT ...] and [qarm: QAMPEST ...] on stderr
#include <stdio.h>
#include "qarm_auto.h"

#define INLINE static inline __attribute__((always_inline))

INLINE int peg(int code, int k) { return (code >> (2 * k)) & 3; }    // 2 bits per peg: R G B Y = 0 1 2 3

INLINE int answer(int guess, int secret)                           // black * 8 + white
{
    int black = 0, common = 0;
    for (int k = 0; k < 4; k++) black += peg(guess, k) == peg(secret, k);
    for (int c = 0; c < 4; c++) {
        int a = 0, b = 0;
        for (int k = 0; k < 4; k++) { a += peg(guess, k) == c; b += peg(secret, k) == c; }
        common += a < b ? a : b;
    }
    return black * 8 + (common - black);
}

// the first clue of a game: RRGG got black 1 white 1
static int fits(int secret) { return answer(0x50, secret) == 9; }

// a partition of eight weights: how far apart the two sides are, at most 61, so it fits in a byte
static const int weight[8] = {13, 7, 9, 4, 11, 6, 3, 8};
static unsigned char imbalance(int mask)
{
    int s = 0;
    for (int i = 0; i < 8; i++) s += (mask >> i & 1) ? weight[i] : -weight[i];
    return (unsigned char)(s < 0 ? -s : s);
}

QARM_ESTIMATE static long codes_left(void)
{
    long n = 0;
    for (int code = 0; code < 256; code++)
        if (fits(code)) n++;
    return n;
}

QARM_ESTIMATE static double mean_imbalance(void)
{
    double s = 0;
    for (int m = 0; m < 256; m++) s += imbalance(m);
    return s / 256;
}

int main(void)
{
    long n = codes_left();
    printf("codes that fit RRGG -> black 1 white 1: %ld (bound %.2f)\n", n, qarm_auto_error());
    double mean = mean_imbalance();
    printf("weights 13 7 9 4 11 6 3 8, a random split: difference %.3f on average (bound %.3f)\n", mean, qarm_auto_error() / 256);
    return 0;
}
