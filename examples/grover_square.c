/* SPDX-License-Identifier: Apache-2.0
 * Copyright 2026 Quantag IT Solutions GmbH */
/* Grover search example: square roots of 9 modulo 16 (x * x mod 16 == 9), x = 0..15.
 * Solutions: 3, 5, 11, 13.
 *
 *     python3 grover.py examples/grover_square.c --entry is_root --bits 4
 */
int is_root(int x)
{
    return ((x * x) & 15) == 9;
}
