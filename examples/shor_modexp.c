/* SPDX-License-Identifier: Apache-2.0
 * Copyright 2026 Quantag IT Solutions GmbH */
/* Modular exponentiation a^x mod n by square-and-multiply.
 *
 * This is the classical core of Shor's factoring algorithm. It is ordinary C, compiled to AArch64
 * machine code; shor.py runs that code on the quantum ARM machine with the exponent x in
 * superposition:
 *
 *     python3 shor.py --N 15 --a 7
 *     python3 shor.py --N 21 --a 2
 *
 * The same binary also runs as a normal program:
 *
 *     python3 arm_qpu.py examples/shor_modexp.c --entry modexp --args 7 5 15      ->  7
 *
 * The loop runs once per bit of x, so different exponents take different numbers of machine cycles.
 */
unsigned modexp(unsigned a, unsigned x, unsigned n)
{
    unsigned r = 1;
    while (x) {
        if (x & 1)
            r = r * a % n;
        a = a * a % n;
        x >>= 1;
    }
    return r;
}
