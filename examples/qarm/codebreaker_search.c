// SPDX-License-Identifier: Apache-2.0
// Copyright 2026 Quantag IT Solutions GmbH
//
// One binary, two ways to break the Code Breaker code (Q-ARM v2, docs/qarm_v2.md).
//
//   clang -O2 -I include examples/qarm/codebreaker_search.c -o codebreaker_search     (any arm64 machine)
//   ./codebreaker_search                       a plain CPU: classical search, up to 256 calls of consistent()
//   python3 qarm_exec.py codebreaker_search    the Q-ARM runtime: Grover's search on the quantum coprocessor,
//                                              the oracle built from consistent()'s machine code in this binary
#include <stdio.h>
#include "qarm.h"

// the predicate must be one function without calls: its helpers are always inlined
#define INLINE static inline __attribute__((always_inline))

INLINE int peg(int code, int k) { return (code >> (2 * k)) & 3; }    // 2 bits per peg: R G B Y = 0 1 2 3

// the answer to a guess: black * 8 + white
INLINE int answer(int guess, int secret)
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

// does secret fit the game so far? RRGG got black 1 white 1, RGBY black 0 white 4, YRGB black 0 white 4
static int consistent(int secret)
{
    secret &= 0xff;
    return answer(0x50, secret) == 9 && answer(0xe4, secret) == 4 && answer(0x93, secret) == 4;
}

int main(void)
{
    const char *colours = "RGBY";
    int secret;
    printf("the game so far: RRGG black 1 white 1, RGBY black 0 white 4, YRGB black 0 white 4\n");
    printf("searching %s\n", qarm_available() ? "with Grover's search on the quantum coprocessor (Q-ARM)"
                                              : "classically on this CPU");
    if (qarm_search(consistent, 8, &secret))
        printf("the secret code: %c%c%c%c\n", colours[peg(secret, 0)], colours[peg(secret, 1)],
               colours[peg(secret, 2)], colours[peg(secret, 3)]);
    else
        printf(qarm_features() & QARM_F_SEARCH ? "no code found within the search budget (one may still fit)\n"
                                               : "no code fits these answers\n");
    return 0;
}
