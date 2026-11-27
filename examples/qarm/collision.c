/* SPDX-License-Identifier: Apache-2.0
 * Copyright 2026 Quantag IT Solutions GmbH
 *
 * QCOLLISION: two different 14-bit messages with the same 10-bit tag, where the tag is an ordinary C function (a
 * truncated FNV-1a hash of the message's two bytes). Brassard-Hoyer-Tapp (1997): a table of f on N^(1/3) random
 * messages, then Grover's search for a message whose tag is in the table; for a two-to-one f that is O(N^(1/3))
 * calls against O(sqrt N) for a classical birthday search. (A 10-bit tag also collides classically after about
 * 40 tags: this example shows the instruction, not a speedup.)
 * The oracle is built from tag()'s machine code; the answer is checked by calling tag() twice. */
#include <stdio.h>
#include "qarm.h"

__attribute__((noinline)) static int tag(int m)
{
    unsigned h = 2166136261u;
    h = (h ^ (unsigned)(m & 0xFF)) * 16777619u;
    h = (h ^ (unsigned)(m >> 8 & 0x3F)) * 16777619u;
    return (int)((h ^ h >> 10) & 0x3FF);
}

int main(void)
{
    int a, b;
    if (qarm_collision(tag, 14, &a, &b) != 1) { printf("no collision\n"); return 1; }
    printf("tag(0x%04x) = tag(0x%04x) = 0x%03x (%s)\n", a, b, tag(a), qarm_features() & QARM_F_COLLISION
           ? "QCOLLISION on the quantum coprocessor" : "every tag computed and sorted on this CPU");
    return !(a != b && tag(a) == tag(b));
}
