// Q-ARM v1 GNU/LLVM AArch64 assembler macros.
// Angles are signed 32-bit integers in units pi / 2^29, NOT radians.
// The branch executes on both machines; only Q-ARM interprets the skipped data.

.macro QARM_U target, theta, phi, lambda
    .if (\target < 0) || (\target > 255)
        .error "QARM_U target outside 0..255"
    .endif
    b 991f
    .word 0x4d524151, (0x01010000 | \target)
    .word \theta, \phi, \lambda, 0, 0
991:
.endm

.macro QARM_CX control, target
    .if (\control < 0) || (\control > 255) || (\target < 0) || (\target > 255) || (\control == \target)
        .error "QARM_CX needs distinct qubits in 0..255"
    .endif
    b 991f
    .word 0x4d524151, (0x01020000 | (\target << 8) | \control)
    .word 0, 0, 0, 0, 0
991:
.endm

.macro QARM_PHASE theta
    b 991f
    .word 0x4d524151, 0x01030000, \theta, 0, 0, 0, 0
991:
.endm

.macro QARM_H target
    QARM_U \target, 0x10000000, 0, 0x20000000
.endm

.macro QARM_X target
    QARM_U \target, 0x20000000, 0, 0x20000000
.endm

.macro QARM_T target
    QARM_U \target, 0, 0, 0x08000000
.endm
