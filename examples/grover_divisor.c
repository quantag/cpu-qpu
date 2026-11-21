/* SPDX-License-Identifier: Apache-2.0
 * Copyright 2026 Quantag IT Solutions GmbH */
/* Grover search example: which 4-bit numbers x divide 91?
 *
 * The predicate is ordinary C. It is compiled to an AArch64 object file, and the same machine
 * code is used as the oracle of Grover's algorithm on the quantum ARM machine:
 *
 *     python3 grover.py examples/grover_divisor.c --entry divides91 --bits 4
 *
 * Over x = 0..15 the solutions are 7 and 13.
 */
int divides91(int x)
{
    return x > 1 && 91 % x == 0;
}
