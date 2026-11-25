// Assemble from the repository root with clang --target=aarch64-linux-gnu.
.include "include/qarm.inc"
.text
.balign 4
.global qarm_mixed
qarm_mixed:
    mov w0, #5
    QARM_H 0
    QARM_CX 0, 1
    add w0, w0, #3
    ret
// Native AArch64: returns 8, quantum payload skipped, NZCV unchanged.
// Q-ARM: returns 8 and prepares a Bell pair in the separate quantum bank.
