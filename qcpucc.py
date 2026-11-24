#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Quantag IT Solutions GmbH
"""qcpucc: build a C program whose __qcpu__ functions run on the quantum CPU (docs/qcpu_spec.md).

    qcpucc game.c -o game                     an executable for this computer
    qcpucc lib.c --shared -o libgame.dylib    a library (.dylib / .so)
    qcpucc --qcpu-mode auto game.c -o game    also a small circuit per function (qoracle), where possible
    ./game                                    __qcpu__ calls run on the quantum CPU (simulated), checked
    QCPU_BACKEND=native ./game                the same program, everything on the CPU

What it does with the sources:
  1. QCPU side: compile them for the quantum CPU's instruction set (AArch64 by default), keep only the
     __qcpu__ functions and what they reach (helpers, constants), and build the quantum CPU's circuit
     for that code. main(), I/O and everything else is left out; a __qcpu__ function that reaches I/O
     or another library function is a build error.
  2. Host side: compile them for this computer. Each __qcpu__ function's own body is renamed
     qpu__native_<name> (kept for verification and QCPU_BACKEND=native); calls to <name> go to a
     generated wrapper that runs the circuit (runtime/qpu_engine.c) and checks the result.
  3. Link the host objects and the generated wrapper into one executable or library.
No Python is needed to run the result. Options other than the ones below go to the host compiler
(-I, -D, -O, -W..., -std=...) or the host linker (-l, -L, -Wl,..., -framework).
"""
import argparse, os, re, shlex, shutil, subprocess, sys, tempfile

ROOT = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, ROOT)
import qpu_dll
from qcpu import isa as isas

RT = os.path.join(ROOT, "runtime")
QINC = os.path.join(RT, "qcpu_include")
MARK = re.compile(r"\b__qcpu__\b")


class Error(Exception):
    pass


# ------------------------------------------------------------------ source scanning
def blank(src):
    """The source with comments, strings and character literals replaced by spaces (same length)."""
    out, i, n = list(src), 0, len(src)
    while i < n:
        c = src[i]
        if src.startswith("//", i):
            j = src.find("\n", i)
            j = n if j < 0 else j
        elif src.startswith("/*", i):
            j = src.find("*/", i + 2)
            j = n if j < 0 else j + 2
        elif c in "\"'":
            j = i + 1
            while j < n and src[j] != c:
                j += 2 if src[j] == "\\" else 1
            j = min(j + 1, n)
        else:
            i += 1
            continue
        for k in range(i, j):
            if out[k] != "\n":
                out[k] = " "
        i = j
    return "".join(out)


def match_close(text, i, open_ch, close_ch):
    depth = 0
    for j in range(i, len(text)):
        if text[j] == open_ch:
            depth += 1
        elif text[j] == close_ch:
            depth -= 1
            if depth == 0:
                return j
    raise Error("unbalanced brackets")


BITS_ANNOT = re.compile(r"\bQCPU_(RESULT_BITS|SBITS|BITS)\s*\(\s*([^()]*?)\s*\)")


def split_params(plist):
    depth, cur, parts = 0, "", []
    for ch in plist:
        if ch == "," and depth == 0:
            parts.append(cur); cur = ""
            continue
        depth += {"(": 1, ")": -1}.get(ch, 0)
        cur += ch
    return parts + [cur] if cur.strip() else parts


def bit_annotations(path, proto, name):
    """QCPU_RESULT_BITS(n) before the name, QCPU_BITS(n) / QCPU_SBITS(n) per parameter. Returns
    (prototype without them, result bits or None, [(bits, signed) or None per parameter])."""
    def number(kind, text):
        if not text.isdigit() or not 1 <= int(text) <= 64:
            raise Error(f"{path}: {name}: QCPU_{kind}({text}): give a number of bits, 1 to 64")
        return int(text)
    at = re.search(r"\b" + re.escape(name) + r"\s*\(", proto)
    head, plist = proto[:at.start()], proto[at.end():proto.rindex(")")]
    result = None
    for m in BITS_ANNOT.finditer(head):
        if m.group(1) != "RESULT_BITS":
            raise Error(f"{path}: {name}: QCPU_{m.group(1)} belongs to a parameter; the result takes QCPU_RESULT_BITS")
        result = number("RESULT_BITS", m.group(2))
    params = []
    for p in split_params(plist):
        ms = list(BITS_ANNOT.finditer(p))
        if any(m.group(1) == "RESULT_BITS" for m in ms):
            raise Error(f"{path}: {name}: QCPU_RESULT_BITS belongs before the function name")
        if len(ms) > 1:
            raise Error(f"{path}: {name}: more than one QCPU_BITS / QCPU_SBITS on a parameter")
        params.append((number(ms[0].group(1), ms[0].group(2)), ms[0].group(1) == "SBITS") if ms else None)
    if params == [None] and plist.strip() in ("", "void"):
        params = []
    clean = " ".join(BITS_ANNOT.sub(" ", proto).split()).replace(" (", "(").replace("( ", "(").replace(" )", ")")
    return clean, result, params


def find_qcpu(path):
    """__qcpu__ function definitions of a source file: [dict(name, proto, name_at, body=(start, end), result_bits,
    param_bits)]; proto is without the QCPU_*BITS annotations."""
    src = open(path, encoding="utf-8", errors="surrogateescape").read()
    b = blank(src)
    found = []
    for m in MARK.finditer(b):
        start = m.end()
        par = b.find("(", start)
        while par >= 0 and re.search(r"\bQCPU_(RESULT_BITS|SBITS|BITS)\s*$", b[start:par]):
            par = b.find("(", match_close(b, par, "(", ")") + 1)       # QCPU_RESULT_BITS(n) before the name
        semi = b.find(";", start)
        brace = b.find("{", start)
        if par < 0:
            continue
        head = b[start:par]
        nm = re.search(r"([A-Za-z_]\w*)\s*$", head)
        if not nm:
            raise Error(f"{path}: cannot read the __qcpu__ declaration at offset {m.start()}")
        close = match_close(b, par, "(", ")")
        after = b[close + 1:].lstrip()
        if after.startswith(";"):
            continue                                    # a prototype; the definition carries the marker too
        if not after.startswith("{"):
            raise Error(f"{path}: __qcpu__ {nm.group(1)}: expected a function definition")
        if re.search(r"\bstatic\b", head) or re.search(r"\bstatic\b", b[max(0, m.start() - 40):m.start()].split(";")[-1].split("}")[-1]):
            raise Error(f"{path}: __qcpu__ {nm.group(1)} is static; __qcpu__ functions need external linkage")
        body_open = b.index("{", close)
        body_close = match_close(b, body_open, "{", "}")
        proto = " ".join((src[start:close + 1]).split())
        proto, rbits, pbits = bit_annotations(path, proto, nm.group(1))
        found.append(dict(name=nm.group(1), proto=proto, name_at=start + nm.start(1), body=(body_open, body_close),
                          result_bits=rbits, param_bits=pbits))
        del semi, brace
    return src, found


def cpath(path):
    """An absolute path for a C string literal: forward slashes work on every system, backslashes would be
    escape sequences."""
    return os.path.abspath(path).replace("\\", "/")


def host_os(h):
    """The system the host compiler builds for, from its predefined macros: windows, darwin or linux."""
    return "windows" if "_WIN32" in h else "darwin" if "__APPLE__" in h else "linux" if "__linux__" in h else "other"


def default_cc():
    for cc in ("cc", "clang", "gcc"):
        if shutil.which(cc):
            return cc
    raise Error("no C compiler found for this computer (cc, clang or gcc); use --cc")


def host_copy(path, src, defs, names):
    """The host version of a source: __qcpu__ bodies renamed qpu__native_<name>, a prototype of <name> before
    each, and calls between __qcpu__ functions inside those bodies going to the native versions. Lines are
    unchanged, so compiler messages point at the original file."""
    edits = []                                           # (position, length, replacement)
    pat = re.compile(r"\b(" + "|".join(map(re.escape, names)) + r")\b(?=\s*\()")
    b = blank(src)
    for d in defs:
        edits.append((d["name_at"], len(d["name"]), "qpu__native_" + d["name"]))
        lo, hi = d["body"]
        for m in pat.finditer(b, lo, hi):
            edits.append((m.start(), len(m.group(1)), "qpu__native_" + m.group(1)))
    out = src
    for pos, ln, rep in sorted(edits, reverse=True):
        out = out[:pos] + rep + out[pos + ln:]
    protos = "".join(f"{d['proto']}; " for d in defs)
    # the prototypes go on the first line of the file (keeps line numbers); <qcpu.h> types first
    first_nl = out.find("\n")
    return f'#line 1 "{cpath(path)}"\n#include <qcpu.h>\n' + "#line 1\n" + out[:first_nl] + "\n" + out[first_nl + 1:], protos


# ------------------------------------------------------------------ compilers
def macros(cc, flags):
    p = subprocess.run(cc + flags + ["-dM", "-E", "-x", "c", "-"], input="", capture_output=True, text=True)
    if p.returncode:
        raise Error("cannot run " + " ".join(cc) + ": " + p.stderr.strip())
    out = {}
    for line in p.stdout.splitlines():
        parts = line.split(None, 2)
        if len(parts) >= 2 and parts[0] == "#define":
            out[parts[1]] = parts[2] if len(parts) > 2 else ""
    return out


FACTS = [("__SIZEOF_SHORT__", "sizeof(short)"), ("__SIZEOF_INT__", "sizeof(int)"), ("__SIZEOF_LONG__", "sizeof(long)"),
         ("__SIZEOF_LONG_LONG__", "sizeof(long long)"), ("__SIZEOF_POINTER__", "sizeof(void *)"),
         ("__SIZEOF_SIZE_T__", "sizeof(size_t)"), ("__SIZEOF_WCHAR_T__", "sizeof(wchar_t)"),
         ("__CHAR_UNSIGNED__", "char is unsigned"), ("__BYTE_ORDER__", "byte order")]


def data_model(host_cc, qcpu_cc, h=None):
    """Data-model facts of the two compilers (spec 5.6.1). Returns (flags for the QCPU side, differences)."""
    h = h if h is not None else macros(host_cc, [])
    t = macros(qcpu_cc, [])
    flags = []
    if ("__CHAR_UNSIGNED__" in h) != ("__CHAR_UNSIGNED__" in t):
        flags.append("-funsigned-char" if "__CHAR_UNSIGNED__" in h else "-fsigned-char")
        t = macros(qcpu_cc, flags)
    diffs = []
    for key, what in FACTS:
        if h.get(key) != t.get(key):
            diffs.append(f"{what}: host {h.get(key, 'no')}, quantum CPU {t.get(key, 'no')}")
    return flags, diffs


def split_args(rest):
    cflags, ldflags, sources = [], [], []
    it = iter(rest)
    for a in it:
        if a.endswith((".c",)):
            sources.append(a)
        elif a.startswith(("-l", "-L", "-Wl,")) or a == "-pthread":
            ldflags.append(a)
        elif a == "-framework":
            ldflags += [a, next(it)]
        elif a in ("-I", "-D", "-U", "-include", "-isystem"):
            cflags += [a, next(it)]
        else:
            cflags.append(a)
    return cflags, ldflags, sources


def run(cmd, what):
    p = subprocess.run(cmd, capture_output=True, text=True)
    if p.returncode:
        raise Error(f"{what} failed:\n  {shlex.join(cmd)}\n{p.stdout}{p.stderr}")
    return p


# ------------------------------------------------------------------ compiled mode (qoracle)
def type_bits(ctype, char_signed):
    """(bits, signed) of an integer type on the quantum CPU (AArch64, LP64)."""
    t = " ".join(ctype.replace("const", " ").replace("volatile", " ").split())
    if t in ("_Bool", "bool"):
        return 1, False
    m = re.match(r"^u?int(8|16|32|64)_t$", t)
    if m:
        return int(m.group(1)), not t.startswith("u")
    if t in ("size_t", "uintptr_t"):
        return 64, False
    if t in ("ssize_t", "intptr_t", "ptrdiff_t"):
        return 64, True
    unsigned = "unsigned" in t.split()
    core = [w for w in t.split() if w not in ("signed", "unsigned", "int")] or ["int"]
    bits = {"char": 8, "short": 16, "int": 32, "long": 64, "long long": 64}.get(" ".join(core))
    if bits is None:
        raise Error(f"type '{ctype}' is not supported in compiled mode")
    if core == ["char"] and "signed" not in t.split() and not unsigned:
        return 8, char_signed
    return bits, not unsigned


EXHAUSTIVE_BITS = 16           # qoracle tests every input up to this many input bits


def compile_functions(qobj, isa, funcs, defs, mode, char_signed, verify_limit=4096):
    """Compiled circuits (spec 5.5) for the __qcpu__ functions that qualify. Returns (C code, {function: C name},
    report lines). mode "compiled": every function must qualify; "auto": the others stay interpreted."""
    try:
        import qoracle
        from qoracle import verify as qverify, frontend as qfrontend
    except ImportError:
        raise Error("compiled mode needs qoracle: python3 -m pip install git+https://github.com/quantag/qoracle")
    by_name = {d["name"]: d for d in defs}
    code, circuits, lines = [], {}, []
    for name, ret, params in funcs:
        d = by_name[name]

        def skip(why):
            if mode == "compiled":
                raise Error(f"{name}: no compiled circuit: {why} (build with --qcpu-mode auto to keep it interpreted)")
            lines.append(f"{name}: interpreted ({why})")
        if isa.name != "arm64":
            skip(f"compiled mode reads AArch64 machine code, the quantum CPU here is {isa.title}")
            continue
        if any(p.direction for p in params):
            skip("it takes buffers (compiled circuits cannot write memory yet)")
            continue
        if ret == "void":
            skip("it returns nothing")
            continue
        if not params:
            skip("it has no arguments")
            continue
        try:
            rbits, rsigned = type_bits(ret, char_signed)
            spec = []
            for p, ann in zip(params, d["param_bits"] or [None] * len(params)):
                bits, signed = ann if ann else type_bits(p.ctype, char_signed)
                spec.append(qoracle.Arg(name=p.name, bits=bits, signed=signed))
        except Error as e:
            skip(str(e))
            continue
        total = sum(x.bits for x in spec)
        want = d["result_bits"]
        try:
            o = qoracle.build(qobj, name, spec, out_bits=want or rbits, verify_limit=verify_limit)
        except qoracle.OracleError as e:
            skip(f"qoracle: {e}")
            continue
        rep = o.report
        exhaustive = rep["verification"]["exhaustive"]
        if not exhaustive and (rep["loops"] or rep["tables"] or rep["rom_bits"]):
            skip(f"its inputs total {total} bits, so qoracle tested {rep['verification']['inputs_tested']} sampled inputs, "
                 "and it has loops or tables: the run-time guards of spec 5.5.2 are not implemented yet; declare "
                 f"QCPU_BITS so that the inputs total at most {EXHAUSTIVE_BITS} bits")
            continue
        if want:                                     # QCPU_RESULT_BITS(n) is a claim: prove it
            if exhaustive:
                image = qfrontend.load(qobj, name)
                cpu = qverify.CPU(image)
                vectors, _ = qverify.test_inputs([(x.name, x.bits, x.signed) for x in spec], 1 << EXHAUSTIVE_BITS)
                for vec in vectors:
                    argv = qoracle.cpu_args(spec, vec)
                    v = cpu.call(argv) & ((1 << rbits) - 1)
                    if rsigned and v >> (rbits - 1):
                        v -= 1 << rbits
                    if not 0 <= v < (1 << want):
                        raise Error(f"{name}: QCPU_RESULT_BITS({want}) is wrong: for "
                                    + ", ".join(f"{x.name} = {v_}" for x, v_ in zip(spec, argv)) + f" the result is {v}")
            else:
                full = qoracle.build(qobj, name, spec, out_bits=rbits, verify_limit=verify_limit)
                if full.out_bits > want:
                    raise Error(f"{name}: QCPU_RESULT_BITS({want}) cannot be proven: its inputs total {total} bits, too "
                                f"many to test all, and result bit {full.out_bits - 1} is not constant 0 in the logic")
        c = o.circuit
        gates = []
        for g in c.gates:
            if g[0] not in ("x", "cx", "ccx"):
                raise Error(f"{name}: qoracle produced a '{g[0]}' gate, which the engine does not run")
            q = list(g[1:]) + [0] * (4 - len(g))
            gates += [qpu_dll.OPS[g[0]] << 30 | q[0], q[1], q[2]]
        first = {n: f for n, f, _ in c.regs}
        var = f"qpu__k_{name}"
        code += [qpu_dll.c_array(f"{var}_gates", gates), qpu_dll.c_array(f"{var}_in", [first[x.name] for x in spec]),
                 qpu_dll.c_array(f"{var}_bits", [x.bits for x in spec], "unsigned char"),
                 qpu_dll.c_array(f"{var}_signed", [int(x.signed) for x in spec], "unsigned char"),
                 f"static const qpu__circuit {var} = {{{c.nqubits}u, {var}_gates, {len(c.gates)}, {len(spec)}, {var}_in, "
                 f"{var}_bits, {var}_signed, {c.out0}u, {c.n_out}, {c.anc0}u}};", ""]
        circuits[name] = var
        how = (f"all {rep['verification']['inputs_tested']} inputs" if exhaustive else
               f"{rep['verification']['inputs_tested']} sampled inputs, no loops or tables")
        lines.append(f"{name}: compiled circuit, {c.nqubits} qubits ({c.n_in} in, {c.n_out} out), {len(c.gates)} gates "
                     f"({rep['toffoli']} Toffoli); checked on {how}")
    return "\n".join(code), circuits, lines


# ------------------------------------------------------------------ build
def build(a, cflags, ldflags, sources):
    host_cc = shlex.split(a.cc or os.environ.get("CC") or default_cc(), posix=os.name != "nt")
    host_macros = macros(host_cc, [])
    system = host_os(host_macros)
    isa = isas.get(a.qcpu_isa)
    qcpu_cc = ["clang"] + isa.clang
    if not shutil.which("clang"):
        raise Error("clang is needed for the quantum-CPU side (it cross-compiles for the quantum CPU's instruction set)")
    defs_by_file = {}
    for s in sources:
        src, defs = find_qcpu(s)
        defs_by_file[s] = (src, defs)
    all_defs = [d for _, ds in defs_by_file.values() for d in ds]
    names = [d["name"] for d in all_defs]
    if len(set(names)) != len(names):
        raise Error("a __qcpu__ function is defined twice")
    dm_flags, dm_diffs = data_model(host_cc, qcpu_cc, host_macros)
    if a.output:
        out = a.output
        if system == "windows" and not os.path.splitext(out)[1]:
            out += ".dll" if a.shared else ".exe"
    elif a.shared:
        out = {"windows": "a.dll", "darwin": "liba.dylib"}.get(system, "liba.so")
    else:
        out = "a.exe" if system == "windows" else "a.out"
    pic = ["-fPIC"] if a.shared and system != "windows" else []
    with tempfile.TemporaryDirectory(prefix="qcpucc_") as d:
        hdr = os.path.join(d, "qcpu_functions.h")
        protos = "".join(f"{d_['proto']};\n" for d_ in all_defs)
        open(hdr, "w").write("#include <stddef.h>\n#include <stdint.h>\n#include <qcpu.h>\n" + protos)
        objs = []
        user_inc = [x for i, f in enumerate(cflags) for x in ([f] if f.startswith(("-I", "-D", "-U")) else [])]
        # pair-wise options (-I dir) were kept together by split_args
        pairs = []
        k = 0
        while k < len(cflags):
            if cflags[k] in ("-I", "-D", "-U", "-isystem"):
                pairs += cflags[k:k + 2]; k += 2
            elif cflags[k].startswith(("-I", "-D", "-U")):
                pairs.append(cflags[k]); k += 1
            else:
                k += 1
        del user_inc
        if all_defs:
            # 1. quantum-CPU side: one translation unit, pruned to the __qcpu__ functions
            unity = os.path.join(d, "qcpu_side.c")
            open(unity, "w").write("".join(f'#include "{cpath(s)}"\n' for s in sources))
            qobj = os.path.join(d, "qcpu_side.o")
            run(qcpu_cc + [f"-O{a.qcpu_opt}", "-fno-stack-protector", "-ffunction-sections", "-fdata-sections",
                           "-fno-asynchronous-unwind-tables", "-nostdlibinc", "-isystem", QINC, "-I", RT,
                           "-D__QCPU_SIDE__=1"] + dm_flags + pairs + [unity, "-c", "-o", qobj], "compiling for the quantum CPU")
            funcs = qpu_dll.parse_header(hdr)
            try:
                B = qpu_dll.build_machine(qobj, isa, funcs, a.qcpu_width, a.qcpu_memory, roots=names)
            except qpu_dll.BuildError as e:
                msg = str(e)
                if "unresolved" in msg or "cannot print" in msg:
                    msg = (msg.replace("functions in a QPU library cannot print", "I/O stays on the CPU")
                           + " (a __qcpu__ function, or a function it calls, uses the C library; __qcpu__ code must be pure)")
                raise Error(msg)
            if B["unsupported"]:
                print(f"qcpucc: warning: {len(B['unsupported'])} instruction(s) the quantum CPU does not implement; "
                      "a call that reaches one fails with QCPU_TRAP: " + "; ".join(e["asm"] for e in B["unsupported"][:5]),
                      file=sys.stderr)
            extra_c, circuits, mode_lines = "", {}, []
            if a.qcpu_mode != "interpreted":
                extra_c, circuits, mode_lines = compile_functions(qobj, isa, funcs, all_defs, a.qcpu_mode,
                                                                  "__CHAR_UNSIGNED__" not in host_macros)
            gen = os.path.join(d, "qcpu_wrappers.c")
            open(gen, "w").write(qpu_dll.library_source(os.path.splitext(os.path.basename(out))[0], hdr, funcs, B,
                                                        a.qcpu_max_steps, True, circuits, extra_c))
            if a.keep:
                shutil.copy(gen, out + ".qcpu_wrappers.c")
            gobj = os.path.join(d, "qcpu_wrappers.o")
            run(host_cc + ["-c", "-O2", "-D__QCPUCC__=1"] + pic + ["-I", RT, "-I", d, gen, "-o", gobj], "compiling the generated wrappers")
            objs.append(gobj)
        # 2. host side
        for i, s in enumerate(sources):
            src, defs = defs_by_file[s]
            text, _ = host_copy(s, src, defs, names) if all_defs else (f'#line 1 "{cpath(s)}"\n' + src, "")
            if defs:
                # declarations of every __qcpu__ function, so that main() in another file sees them
                text = text.replace("#include <qcpu.h>\n", "#include <qcpu.h>\n#include \"qcpu_functions.h\"\n", 1)
            hs = os.path.join(d, f"host{i}.c")
            open(hs, "w", encoding="utf-8", errors="surrogateescape").write(text)
            ho = os.path.join(d, f"host{i}.o")
            run(host_cc + ["-c", "-D__QCPUCC__=1"] + pic + ["-I", RT, "-I", d, "-I", os.path.dirname(os.path.abspath(s))] + cflags + [hs, "-o", ho],
                f"compiling {s} for this computer")
            objs.append(ho)
        # 3. link
        if a.shared:
            link = host_cc + (["-dynamiclib"] if system == "darwin" else ["-shared"]) + objs + ["-o", out]
        else:
            link = host_cc + objs + ["-o", out]
        if system == "linux":
            link.append("-lpthread")
        run(link + ldflags, "linking")
    if all_defs:
        m = B["m"]
        print(f"qcpucc: {out}: {len(names)} __qcpu__ function(s) on the quantum CPU ({isa.title}, W = {B['W']}, "
              f"{len(B['code'])} {'bytes' if isa.unit == 1 else 'instructions'}, {m.n} qubits, {len(B['tmpl'])} gates "
              f"per cycle): {', '.join(names)}", file=sys.stderr)
        for line in mode_lines:
            print("qcpucc:   " + line, file=sys.stderr)
        if dm_diffs:
            print("qcpucc: note: the host and quantum-CPU data models differ (" + "; ".join(dm_diffs) + "); calls "
                  "rely on the comparison with the host build (QCPU_VERIFY=1, the default)", file=sys.stderr)
    else:
        print(f"qcpucc: {out}: no __qcpu__ functions; an ordinary program", file=sys.stderr)
    return out


def main(argv=None):
    ap = argparse.ArgumentParser(prog="qcpucc", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("-o", dest="output", help="output file")
    ap.add_argument("--shared", action="store_true", help="build a shared library (.dylib / .so)")
    ap.add_argument("--qcpu-isa", default="arm64", help="instruction set of the quantum CPU: " + " | ".join(isas.NAMES))
    ap.add_argument("--qcpu-width", type=int, default=16, help="register width of the quantum CPU, 8..16 (default 16)")
    ap.add_argument("--qcpu-memory", type=int, default=512, help="data memory in ints, a power of two (default 512)")
    ap.add_argument("--qcpu-max-steps", type=int, default=1000000, help="default cycle limit per call")
    ap.add_argument("--qcpu-opt", default="1", help="optimisation level of the quantum-CPU code (default 1)")
    ap.add_argument("--qcpu-mode", choices=("interpreted", "compiled", "auto"), default="interpreted",
                    help="interpreted: the whole quantum CPU runs each function (default); compiled: also a small "
                         "circuit of each function alone (qoracle), required for every function; auto: compiled where "
                         "possible. The program runs the compiled circuit unless QCPU_MODE=interpreted")
    ap.add_argument("--cc", help="host C compiler (default $CC, else cc)")
    ap.add_argument("--keep", action="store_true", help="keep the generated wrapper source next to the output")
    a, rest = ap.parse_known_args(argv)
    cflags, ldflags, sources = split_args(rest)
    if not sources:
        ap.error("no C source files")
    try:
        build(a, cflags, ldflags, sources)
    except (Error, ValueError, OSError) as e:
        print(f"qcpucc: error: {e}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
