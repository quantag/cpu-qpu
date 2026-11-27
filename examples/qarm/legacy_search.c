/* SPDX-License-Identifier: Apache-2.0
 * Copyright 2026 Quantag IT Solutions GmbH
 *
 * An ordinary program with no Q-ARM in it: no qarm.h, no packets. find_key() tries every 12-bit key until
 * valid_key() accepts one; cheapest_route() tries every route for the least route_cost(), as brute-force code
 * does. tools/qarm_patch.py rewrites the built binary so that these functions first ask a Q-ARM runtime for
 * QSEARCH and QMIN on their predicate's and cost's machine code:
 *
 *   aarch64-linux-gnu-gcc -O1 examples/qarm/legacy_search.c -o legacy_search
 *   python3 tools/qarm_patch.py legacy_search --search find_key=valid_key:12 \
 *                                             --min cheapest_route=route_cost:12 -o legacy_search.qarm
 *   python3 qarm_exec.py legacy_search.qarm      # Grover's search and Durr-Hoyer: O(64) oracle calls, not 4096
 *   ./legacy_search.qarm                         # a plain CPU: the original loops, the same answers
 *
 * valid_key mixes the key's 12 bits with a bijection (xor-shifts and an add), so exactly one key is valid;
 * route_cost has one least route. */
#include <stdio.h>

__attribute__((noinline)) int valid_key(int k)
{
    unsigned v = (unsigned)k & 0xFFF;
    v ^= (v << 3) & 0xFFF;
    v ^= v >> 5;
    v = (v + 0x5A5) & 0xFFF;
    v ^= (v << 7) & 0xFFF;
    return v == 0x9C3;
}

__attribute__((noinline)) int find_key(void)
{
    for (int k = 0; k < 4096; k++)
        if (valid_key(k)) return k;
    return -1;
}

__attribute__((noinline)) int route_cost(int r)
{
    int d = ((r ^ 0x1B7) & 0xFFF) - 1234;
    return d < 0 ? -d : d;
}

__attribute__((noinline)) int cheapest_route(void)
{
    int best = 0, cost = route_cost(0);
    for (int r = 1; r < 4096; r++)
        if (route_cost(r) < cost) {
            best = r;
            cost = route_cost(r);
        }
    return best;
}

int main(void)
{
    int k = find_key(), r = cheapest_route();
    printf("key: %d (%s)\n", k, k >= 0 && valid_key(k) ? "valid" : "none");
    printf("cheapest route: %d (cost %d)\n", r, route_cost(r));
    return k < 0;
}
