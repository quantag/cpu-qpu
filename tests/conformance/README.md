# Q-ARM conformance

`qarm_conformance.c` checks a Q-ARM runtime against [the specification](../../docs/qarm_isa.md),
section 9. It emits the raw packets and checks the status protocol, refusals, side effects (registers
and the bytes around every output buffer), the answers and the feature bits. It never falls back to
the classical paths of `include/qarm.h`, so what it measures is the runtime alone.

Build it for AArch64 at `-O1`, then run it under the runtime:

```
aarch64-linux-gnu-gcc -O1 -iquote include tests/conformance/qarm_conformance.c -o conformance -lm   # or clang on arm64
python3 qarm_exec.py -q conformance                                                      # the Unicorn runtime
qemu-aarch64 -L /usr/aarch64-linux-gnu -plugin build/libqarm-qemu.so,quiet=on ./conformance   # the QEMU plugin
```

Each test prints `PASS`, `FAIL` with the reason, or `SKIP` (a feature the runtime does not claim). The
last line counts them, and the exit status is the number of failures. On a plain CPU the program reports
that there is no runtime.

The statistical checks assume an ideal backend. On a noisy device they show its error rates, not
conformance. CI runs the program under `qarm_exec.py` (`tests/test_qarm_exec.py`, part 5) and under the
QEMU plugin, with and without its oracle helper (`tests/test_qarm_qemu.py`, part 11).
