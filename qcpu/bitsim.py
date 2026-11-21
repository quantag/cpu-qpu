# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Quantag IT Solutions GmbH
"""Exact simulator for the interpreter circuit on computational-basis inputs.

The circuit uses only X / CX / CCX / CSWAP, i.e. it is a permutation of basis states, so a
basis input maps to exactly one basis output; tracking bits is an exact simulation of the
quantum circuit for such inputs (superposed inputs need Aer or hardware)."""


def simulate(gates, n, ones=()):
    s = bytearray(n)
    for q in ones:
        s[q] ^= 1
    for g in gates:
        op = g[0]
        if op == "ccx":
            if s[g[1]] and s[g[2]]:
                s[g[3]] ^= 1
        elif op == "cx":
            if s[g[1]]:
                s[g[2]] ^= 1
        elif op == "x":
            s[g[1]] ^= 1
        elif op == "cswap":
            if s[g[1]]:
                s[g[2]], s[g[3]] = s[g[3]], s[g[2]]
        else:
            raise ValueError(op)
    return s


def value(state, qubits):
    return sum(state[q] << i for i, q in enumerate(qubits))


class Streamer:
    """Incremental simulation: feed gate lists as the machine generates them (QuantumARM.build(sink))."""

    def __init__(self, ones=()):
        self.s = bytearray(max(ones, default=-1) + 1)
        for q in ones:
            self.s[q] ^= 1

    def __call__(self, gates, n):
        s = self.s
        if len(s) < n:
            s.extend(bytes(n - len(s)))
        for g in gates:
            op = g[0]
            if op == "ccx":
                if s[g[1]] and s[g[2]]:
                    s[g[3]] ^= 1
            elif op == "cx":
                if s[g[1]]:
                    s[g[2]] ^= 1
            elif op == "x":
                s[g[1]] ^= 1
            elif op == "cswap":
                if s[g[1]]:
                    s[g[2]], s[g[3]] = s[g[3]], s[g[2]]
            else:
                raise ValueError(op)
