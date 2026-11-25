// SPDX-License-Identifier: Apache-2.0
// Copyright 2026 Quantag IT Solutions GmbH
//
// Code Breaker solver: which secret code fits the answers of a game so far?
//
// consistent(secret) is 1 when every guess below would have got the answer it got. The program asks
// qcpu_search() for such a code:
//
//   qcpucc --qcpu-mode qoracle solver.c -o solver     Grover's search with consistent() as the oracle,
//                                                     in the built-in simulator: about 12 oracle calls
//   qcpucc solver.c -o solver   (or any C compiler)   a classical search: up to 256 calls
//
// With --qcpu-export oracle, the same oracle is also written for Qiskit and CUDA-Q (oracle/consistent_phase.qasm,
// oracle/consistent_cudaq.py); grover_solver.py runs the search from that file.
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
    qcpu_call_info info;
    int secret;
    printf("the game so far: RRGG black 1 white 1, RGBY black 0 white 4, YRGB black 0 white 4\n");
    if (qcpu_search(consistent, 8, &secret)) {
        qcpu_last_call(&info);
        printf("the secret code: %c%c%c%c\n", colours[peg(secret, 0)], colours[peg(secret, 1)],
               colours[peg(secret, 2)], colours[peg(secret, 3)]);
    } else {
        qcpu_last_call(&info);
        printf("no code fits these answers\n");
    }
    printf("[%s]\n", info.message[0] ? info.message : "classical search");
    return 0;
}
