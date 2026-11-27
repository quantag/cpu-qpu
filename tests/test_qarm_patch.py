# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Quantag IT Solutions GmbH
"""tools/qarm_patch.py: Q-ARM put into an existing ELF binary built without qarm.h.

  1. examples/qarm/legacy_search.c, an ordinary program: find_key() and cheapest_route() are brute-force loops.
     Patched with --search find_key=valid_key:12 --min cheapest_route=route_cost:12, the binary runs QSEARCH and
     QMIN under qarm_exec.py (oracles from valid_key's and route_cost's machine code) and prints what the original
     prints; on the CPU (natively, or under qemu-aarch64 without the plugin) the patched binary runs the original
     loops, with the same output.
  2. The trampoline disassembles (capstone) as described in the tool: B at the function, the packet, the check, the
     moved instruction, the branch back.
  3. Refused: an unknown symbol; a function whose first instruction is PC-relative (ADRP); a Mach-O file.
Run: python3 -m tests.test_qarm_patch   (needs an arm64 C compiler that makes ELF: aarch64-linux-gnu-gcc, or cc on
Linux arm64)"""
import os, re, struct, subprocess, sys, tempfile
from tests.test_qarm_exec import ROOT, EXEC, toolchain, sh

PATCH = [sys.executable, os.path.join(ROOT, "tools", "qarm_patch.py")]
LEGACY = os.path.join(ROOT, "examples", "qarm", "legacy_search.c")
ADRP_FIRST = r"""
int table[4096];
__attribute__((noinline)) int lookup(void) { return table[7]; }
int pred(int x) { return x == 3; }
int main(void) { return lookup(); }
"""


def main():
    try:
        import lief, capstone, unicorn  # noqa: F401
    except ImportError as e:
        print(f"skipped: {e.name} is not installed")
        return
    tc = toolchain()
    if not tc:
        print("skipped: no arm64 C compiler")
        return
    cc, native = tc
    with tempfile.TemporaryDirectory() as d:
        exe, out = os.path.join(d, "legacy"), os.path.join(d, "legacy.qarm")
        sh(cc + ["-O1", LEGACY, "-o", exe])
        if open(exe, "rb").read(4) != b"\x7fELF":
            print("skipped: the arm64 compiler makes Mach-O; qarm_patch.py patches ELF")
            return
        p = sh(PATCH + [exe, "--search", "find_key=valid_key:12", "--min", "cheapest_route=route_cost:12", "-o", out])
        assert p.stderr.count("moved to the trampoline") == 2, p.stderr
        want = "key: 90 (valid)\ncheapest route: 1381 (cost 0)\n"
        q = sh(EXEC + [out])
        assert q.stdout == want, q.stdout
        assert re.search(r"QSEARCH valid_key, 12 bits: Grover's search .* checked by running valid_key", q.stderr), q.stderr
        assert re.search(r"QMIN route_cost, 12 bits: Durr-Hoyer .* x = 1381, route_cost\(x\) = 0", q.stderr), q.stderr
        calls = re.search(r"after (\d+) oracle calls", q.stderr).group(1)
        how = ""
        if native is not None:
            r = sh(native + [out])
            assert r.stdout == want and sh(native + [exe]).stdout == want, r.stdout
            how = "; on the CPU, the original loops: the same output"
        print(f"1) legacy_search.c, no qarm.h: patched, QSEARCH finds key 90 in {calls} oracle calls (4096 keys) and "
              f"QMIN route 1381 under qarm_exec.py{how}")

        import lief, capstone
        b = lief.parse(out)
        md = capstone.Cs(capstone.CS_ARCH_ARM64, capstone.CS_MODE_ARM)
        find = next(s.value for s in b.symbols if s.name == "find_key")
        first = next(md.disasm(bytes(b.get_content_from_virtual_address(find, 4)), find))
        assert first.mnemonic == "b", first
        tramp = int(first.op_str.lstrip("#"), 16)
        code = bytes(b.get_content_from_virtual_address(tramp, 256))
        words = struct.unpack("<64I", code)
        k = words.index(0x4d524151)
        assert words[k - 1] == 0x14000008 and words[k + 1] == 0x02110000, words[k - 1:k + 2]
        tail = [f"{i.mnemonic} {i.op_str}".strip() for i in md.disasm(code[4 * (k + 7):], tramp + 4 * (k + 7))][:22]
        assert tail[0] == "cmp x0, #1" and tail[1].startswith("b.ne") and any(t.startswith("bl #") for t in tail) and "ret" in tail, tail
        assert "stp x29, x30, [sp, #-0x20]!" in tail and tail[tail.index("stp x29, x30, [sp, #-0x20]!") + 1].startswith("b #"), tail
        print("2) the trampoline: B at find_key; the QSEARCH packet; W0 = 1 then the check by calling valid_key; the "
              "fallback restores the arguments, runs the moved STP and branches back")

        p = sh(PATCH + [exe, "--search", "nosuch=valid_key:12", "-o", out], check=False)
        assert p.returncode == 2 and "no symbol nosuch" in p.stderr, p.stderr
        src, aexe = os.path.join(d, "adrp.c"), os.path.join(d, "adrp")
        open(src, "w").write(ADRP_FIRST)
        sh(cc + ["-O1", src, "-o", aexe])
        p = sh(PATCH + [aexe, "--search", "lookup=pred:4", "-o", out], check=False)
        assert p.returncode == 2 and "PC-relative (adrp" in p.stderr, p.stderr
        macho = os.path.join(d, "fake")
        open(macho, "wb").write(b"\xcf\xfa\xed\xfe" + bytes(60))
        p = sh(PATCH + [macho, "--search", "a=b:4", "-o", out], check=False)
        assert p.returncode == 2 and "not an ELF file" in p.stderr, p.stderr
        print("3) refused: an unknown symbol, a function that starts with ADRP, a file that is not ELF")
    print("PASS")


if __name__ == "__main__":
    main()
