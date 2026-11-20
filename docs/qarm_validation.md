# Q-ARM v1 validation

Validated on 2026-11-20 against base commit
`f07baf89a171e5d468d7a9c753b5711df54503fd`.

Environment: Python 3.12, Qiskit 2.6.2, Aer 0.18.4, Unicorn 2.1.4,
NumPy 2.3.5, pytest 9.2.0. AArch64 assembly used Zig 0.17.1's Clang
and objcopy frontends, exposed under the tool names expected by the tests.

## New feature checks

All 23 cases in `tests/test_qarm.py` passed (run in three groups: 21 core
cases, one assembly round-trip case, one complete-interpreter Aer case).

- Bell state: exact sparse probabilities 00=0.5, 11=0.5.
- Seeded CLI example: 1024 shots, seed 2026, 00=474 and 11=550.
- Complete interpreter export: Aer MPS produced only 00/11 in 128 shots.
- Quantum operations agree with independently built Qiskit states, including
  custom gates, broadcasts, negative rotations and phase-sensitive circuits.
- Inverse restores quantum data, ARM data, program memory, history and scratch.
- Superposed program operands and phase angles stay coherent.
- ARM conditional branches and counted loops select quantum operations correctly.
- CPU compatibility: Unicorn executes the same mixed instruction bytes, returns
  W0=8, and preserves X1, LR and NZCV while skipping quantum packets.
- Generated assembly reproduces the compiler's raw bytes. Assembler macros
  prepare a Bell pair while the mixed ARM example returns 8.
- Invalid packets and unsupported dynamic QASM operations are rejected.

## Existing regression checks

- `python -m tests.test_fuzz 30 7 8,12,16`: 90 generated cases; zero
  unflagged mismatches. The existing model-limit classifications and assembler
  rejections are retained rather than counted as successful exact matches.
- `python -m tests.test_simulators 10 5`: 10/10 full-circuit vs fast replay
  matches; all three Aer vs fast-replay comparisons match.
- Existing Shor simulator helpers: `sparse_vs_dense()` passes 200 random circuits;
  `fast_vs_plain()` passes 50; QFT-vs-DFT and classical post-processing pass.
- `git diff --check` and Python bytecode compilation pass.

This is simulation/emulation validation. No physical ARM board or physical QPU
was used. Full C-program, Grover and end-to-end Shor suites were not rerun.
