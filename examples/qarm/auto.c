// SPDX-License-Identifier: Apache-2.0
// Copyright 2026 Quantag IT Solutions GmbH
//
// Ordinary C, with no quantum calls: a codebreaker's search and a number partition's minimum written as plain
// loops. The QArmAuto compiler pass (compiler/, docs/qarm_auto.md) finds them and gives them to the quantum
// coprocessor, QSEARCH and QMIN; QARM_AUTO is the only change to the source. Without the pass, or on a CPU
// without the coprocessor, the program runs as written.
//
//   qarm-cc --auto examples/qarm/auto.c -o auto     (or clang -O1 -fpass-plugin=QArmAuto.so ... compiler/qarm_auto.c)
//   ./auto                                        a plain CPU: every candidate tried, the same answers
//   qarm-run ./auto                               Q-ARM: [qarm: QSEARCH ...] and [qarm: QMIN ...] on stderr
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

// the game so far: RRGG got black 1 white 1, RGBY black 0 white 4, YRGB black 0 white 4
static int consistent(int secret) { return answer(0x50, secret) == 9 && answer(0xe4, secret) == 4 && answer(0x93, secret) == 4; }
// a clue no code can satisfy: four blacks for RRGG and none for RRGG
static int impossible(int secret) { return answer(0x50, secret) == 32 && answer(0x50, secret) == 0; }

static const int weight[8] = {13, 7, 9, 4, 11, 6, 3, 8};
static int imbalance(int mask)
{
    int s = 0;
    for (int i = 0; i < 8; i++) s += (mask >> i & 1) ? weight[i] : -weight[i];
    return s < 0 ? -s : s;
}

QARM_AUTO static int find_code(void)
{
    for (int code = 0; code < 256; code++)
        if (consistent(code)) return code;
    return -1;
}

QARM_AUTO static int no_code(void)
{
    for (int code = 0; code < 256; code++)
        if (impossible(code)) return code;
    return -1;
}

QARM_AUTO static int best_split(int *diff)
{
    int best = 1 << 30, mask = 0;
    for (int m = 0; m < 256; m++) {
        int d = imbalance(m);
        if (d < best) { best = d; mask = m; }
    }
    *diff = best;
    return mask;
}

int main(void)
{
    const char *colours = "RGBY";
    int code = find_code();
    printf("a code that fits the game: ");
    if (code < 0) printf("none\n");
    else printf("%c%c%c%c (fits: %d)\n", colours[peg(code, 0)], colours[peg(code, 1)], colours[peg(code, 2)],
                colours[peg(code, 3)], consistent(code));
    printf("a code with an impossible clue: %s\n", no_code() < 0 ? "none (every code tried)" : "found?");
    int d, mask = best_split(&d);
    printf("weights 13 7 9 4 11 6 3 8: a split with difference %d (checked: %d)\n", d, imbalance(mask) == d);
    return 0;
}
