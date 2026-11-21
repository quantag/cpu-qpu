/* SPDX-License-Identifier: Apache-2.0
 * Copyright 2026 Quantag IT Solutions GmbH */
/* Smallest Grover example, small enough to also run on Qiskit Aer:
 *     python3 grover.py examples/grover_tiny.c --entry is_two --bits 2 --backend aer
 * One iteration finds x = 2 among 0..3 with certainty. */
int is_two(int x)
{
    return x == 2;
}
