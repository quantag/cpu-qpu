/* SPDX-License-Identifier: Apache-2.0
 * Copyright 2026 Quantag IT Solutions GmbH
 *
 * Q-ARM v2: quantum instructions in an ordinary ARM program (docs/qarm_v2.md).
 *
 * Each function below places a Q-ARM packet in the code: eight 32-bit words that begin with B .+32, an
 * ordinary branch. A plain ARM CPU jumps over the packet and takes the classical path; the Q-ARM runtime
 * (qarm_exec.py) executes the packet instead, on a quantum coprocessor (its simulator, or a backend
 * from config.json). The same binary runs on both.
 *
 *   qarm_available()            1 under the Q-ARM runtime, 0 on a plain CPU (QAVAIL)
 *   qarm_search(f, bits, &x)    an x in 0 .. 2^bits - 1 with f(x) == 1: Grover's search on the coprocessor (QSEARCH),
 *                               with f's oracle built from f's own machine code; on a plain CPU, every x in turn.
 *                               Returns 1 and stores x, or 0 if there is none. f returns 0 or 1, calls no other
 *                               function and writes no memory; bits <= 16.
 *
 * Other compilers and processors get the classical path only. */
#ifndef QARM_H
#define QARM_H

#if defined(__aarch64__) && (defined(__GNUC__) || defined(__clang__))
#define QARM_PACKET(op) \
    ".inst 0x14000008\n\t"            /* B .+32: a plain CPU continues after the packet */ \
    ".inst 0x4d524151\n\t"            /* "QARM" */ \
    ".inst " #op "\n\t"               /* version 2, opcode */ \
    ".inst 0\n\t.inst 0\n\t.inst 0\n\t.inst 0\n\t.inst 0\n\t"
#define QARM_OP_QAVAIL  0x02100000
#define QARM_OP_QSEARCH 0x02110000

static inline int qarm_available(void)
{
    register long x0 __asm__("x0") = 0;                     /* a plain CPU leaves 0 */
    __asm__ volatile(QARM_PACKET(0x02100000) : "+r"(x0) : : "memory");
    return (int)x0;
}

static inline int qarm_search(int (*f)(int), int bits, int *x)
{
    register long x0 __asm__("x0") = 0;                     /* 0: not executed, 1: found, 2: none */
    register int (*x1)(int) __asm__("x1") = f;
    register long x2 __asm__("x2") = bits;
    register long x3 __asm__("x3") = 0;
    __asm__ volatile(QARM_PACKET(0x02110000) : "+r"(x0), "+r"(x3) : "r"(x1), "r"(x2) : "memory");
    if (x0 == 1) { *x = (int)x3; return 1; }
    if (x0 == 2) return 0;
    for (long v = 0; v < (1L << bits); v++)                 /* the classical path */
        if (f((int)v)) { *x = (int)v; return 1; }
    return 0;
}
#else
static inline int qarm_available(void) { return 0; }
static inline int qarm_search(int (*f)(int), int bits, int *x)
{
    for (long v = 0; v < (1L << bits); v++)
        if (f((int)v)) { *x = (int)v; return 1; }
    return 0;
}
#endif
#endif
