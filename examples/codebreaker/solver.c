// SPDX-License-Identifier: Apache-2.0
// Copyright 2026 Quantag IT Solutions GmbH
//
// Code Breaker solver: which secret codes fit the answers of a game so far?
//
// consistent(secret) is 1 when every guess below would have got the answer it got. Built with
//   qcpucc --qcpu-mode qoracle --qcpu-export oracle solver.c -o solver
// the predicate becomes a quantum circuit, exported for other toolkits (OpenQASM 3, CUDA-Q) as a phase
// oracle: |secret> -> (-1)^consistent(secret) |secret>. Grover's search with it finds the secret code
// among all 256 in about 12 oracle calls (grover_solver.py). The program itself lists the
// consistent codes classically, by calling consistent() for each of the 256 codes.
#include <stdio.h>
#include <qcpu.h>

static int peg(int code, int k) { return (code >> (2 * k)) & 3; }    // 2 bits per peg: R G B Y = 0 1 2 3

// the answer to a guess, as in codebreaker.c: black * 8 + white
static int answer(int guess, int secret)
{
    int black = 0, common = 0;
    for (int k = 0; k < 4; k++)
        black += peg(guess, k) == peg(secret, k);
    for (int c = 0; c < 4; c++) {
        int a = 0, b = 0;
        for (int k = 0; k < 4; k++) {
            a += peg(guess, k) == c;
            b += peg(secret, k) == c;
        }
        common += a < b ? a : b;
    }
    return black * 8 + (common - black);
}

// the game so far: RRGG got black 1 white 1, RGBY black 0 white 4, YRGB black 0 white 4
#define RRGG 0x50
#define RGBY 0xe4
#define YRGB 0x93

__qcpu__ int QCPU_RESULT_BITS(1) consistent(int QCPU_BITS(8) secret)
{
    return answer(RRGG, secret) == 1 * 8 + 1 && answer(RGBY, secret) == 0 * 8 + 4 && answer(YRGB, secret) == 0 * 8 + 4;
}

int main(void)
{
    const char *colours = "RGBY";
    printf("codes consistent with RRGG 1/1, RGBY 0/4, YRGB 0/4:");
    for (int s = 0; s < 256; s++)
        if (consistent(s))
            printf(" %c%c%c%c", colours[peg(s, 0)], colours[peg(s, 1)], colours[peg(s, 2)], colours[peg(s, 3)]);
    printf("\n");
    return 0;
}
