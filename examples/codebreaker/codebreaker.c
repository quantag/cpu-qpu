// SPDX-License-Identifier: Apache-2.0
// Copyright 2026 Quantag IT Solutions GmbH
//
// Code Breaker (Mastermind) with the scoring on the quantum CPU.
//
// The CPU part is an ordinary program: it picks the secret code, reads your guesses from the keyboard
// and prints the board. score() is marked __qcpu__: every call runs on the quantum CPU (the circuit of
// a complete processor, simulated) and is checked against the same function compiled for this computer.
//
//   qcpucc codebreaker.c -o codebreaker      (or: cmake -B build && cmake --build build)
//   qcpucc --qcpu-mode qoracle codebreaker.c -o codebreaker   score() as its own small circuit (qoracle)
//   ./codebreaker                            play; QCPU_BACKEND=native ./codebreaker runs score() on the CPU
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <time.h>
#include <qcpu.h>

#define PEGS 4
#define TURNS 10
static const char COLOURS[] = "RGBY";

static int peg(int code, int k) { return (code >> (2 * k)) & 3; }    // a code: 2 bits per peg

// The answer to a guess: black (right colour, right place) * 8 + white (right colour, wrong place).
// QCPU_BITS / QCPU_RESULT_BITS: the widths a qoracle circuit needs (qcpucc --qcpu-mode qoracle); a plain compiler
// and the virtual CPU ignore them. Codes are 8 bits; the answer is at most 4 * 8 = 32: 6 bits.
__qcpu__ int QCPU_RESULT_BITS(6) score(int QCPU_BITS(8) guess, int QCPU_BITS(8) secret)
{
    int black = 0, common = 0;
    for (int k = 0; k < PEGS; k++)
        black += peg(guess, k) == peg(secret, k);
    for (int c = 0; c < 4; c++) {
        int a = 0, b = 0;
        for (int k = 0; k < PEGS; k++) {
            a += peg(guess, k) == c;
            b += peg(secret, k) == c;
        }
        common += a < b ? a : b;
    }
    return black * 8 + (common - black);
}

static int parse(const char *s)                  // "RGBY" -> code, or -1
{
    int code = 0, n = 0;
    for (; *s && *s != '\n'; s++) {
        if (*s == ' ') continue;
        const char *c = strchr(COLOURS, *s >= 'a' && *s <= 'z' ? *s - 32 : *s);
        if (!c || !*c || n == PEGS) return -1;
        code |= (int)(c - COLOURS) << (2 * (PEGS - 1 - n++));
    }
    return n == PEGS ? code : -1;
}

static void show(int code)
{
    for (int k = PEGS - 1; k >= 0; k--) putchar(COLOURS[peg(code, k)]);
}

int main(int argc, char **argv)
{
    srand(argc > 1 ? (unsigned)atoi(argv[1]) : (unsigned)time(NULL));
    int secret = rand() & 0xFF;
    char line[64];
    printf("Code Breaker: guess the 4-peg code, colours R G B Y (for example RGBY).\n");
    for (int turn = 1; turn <= TURNS; turn++) {
        printf("turn %d> ", turn);
        fflush(stdout);
        if (!fgets(line, sizeof line, stdin)) break;
        int guess = parse(line);
        if (guess < 0) { printf("  4 letters from R G B Y, please\n"); turn--; continue; }
        int a = score(guess, secret);
        qcpu_call_info info;
        qcpu_last_call(&info);
        printf("  ");
        show(guess);
        printf("  black %d  white %d", a >> 3, a & 7);
        const char *how = strcmp(info.guarantee, "E1") == 0 ? "checked" : info.guarantee;
        if (strcmp(info.mode, "qoracle") == 0)
            printf("    [qoracle circuit: %lld gates on %lld qubits, %s]", info.gates, info.qubits, how);
        else if (info.cycles)
            printf("    [virtual CPU: %ld cycles, %.1fM gates on %lldk qubits, %s]", info.cycles, info.gates / 1e6,
                   info.qubits / 1000, how);
        printf("\n");
        if (a == PEGS * 8) {
            printf("Cracked in %d turn%s!\n", turn, turn > 1 ? "s" : "");
            return 0;
        }
    }
    printf("The code was ");
    show(secret);
    printf(".\n");
    return 1;
}
