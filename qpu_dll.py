#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Quantag IT Solutions GmbH
"""Make a shared library (Linux .so, Windows .dll) whose functions run on the quantum machine.

    python3 qpu_dll.py mathlib.c --header mathlib.h -o libmathlib_qpu.so            # AArch64 code (default)
    python3 qpu_dll.py mathlib.c --header mathlib.h --arch riscv64 -o libmathlib_qpu.so
    python3 qpu_dll.py mathlib.o --header mathlib.h -o libmathlib_qpu.so            # an object file you compiled
    python3 qpu_dll.py mathlib.c --header mathlib.h -o mathlib_qpu.dll              # on Windows
    python3 qpu_dll.py mathlib.c --header mathlib.h --target windows -o mathlib_qpu.dll   # from Linux (MinGW)

The functions declared in the header are compiled for the QPU (or taken from the object file). The
generated library exports the same functions with the same signatures; each one sends its call to
the QPU server (qpu_server.py, started automatically), which runs the function on the quantum
machine, checks it against the CPU, and returns the result. A host program links against the
library and calls the functions as ordinary C functions:

    cc main.c -L. -lmathlib_qpu -Wl,-rpath,'$ORIGIN' -o main          Linux
    clang main.c mathlib_qpu.lib -o main.exe                         Windows (or gcc main.c mathlib_qpu.dll)

Supported signatures: integer parameters and return values (char, short, int, long, their unsigned
forms, intN_t / uintN_t, void return), and pointers to int buffers annotated with QPU_IN(count),
QPU_OUT(count) or QPU_INOUT(count) from runtime/qpu_dll.h. Values must fit the register width of the
machine (--width, default 16 bits); a result that does not fit is reported as not exact.
"""
import argparse, hashlib, os, re, subprocess, sys, tempfile

ROOT = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, ROOT)
import cpu_qpu
from qcpu import isa as isas
from qcpu.loader import ObjectImage, LinkError

INT_TYPES = {"char", "signed char", "unsigned char", "short", "unsigned short", "int", "unsigned", "unsigned int",
             "signed", "signed int", "long", "unsigned long", "long long", "unsigned long long", "short int",
             "long int", "unsigned short int", "unsigned long int", "_Bool", "bool",
             "int8_t", "int16_t", "int32_t", "int64_t", "uint8_t", "uint16_t", "uint32_t", "uint64_t",
             "intptr_t", "size_t", "ssize_t"}
BUF_TYPES = {"int", "unsigned", "unsigned int", "signed", "signed int", "int32_t", "uint32_t"}
ANNOT = re.compile(r"\bQPU_(IN|OUT|INOUT)\s*\(")


class Param:
    def __init__(self, ctype, name, direction=None, count=None):
        self.ctype, self.name, self.direction, self.count = ctype, name, direction, count


def norm(t):
    t = re.sub(r"\b(const|volatile|extern|static|inline|register|restrict|__restrict)\b", " ", t)
    return " ".join(t.split())


def take_annotation(text):
    """'const int *QPU_IN(2*n) v' -> ('const int * v', 'IN', '2*n')"""
    m = ANNOT.search(text)
    if not m:
        return text, None, None
    depth, i = 1, m.end()
    while depth:
        if i >= len(text):
            raise ValueError(f"unbalanced parentheses in '{text}'")
        depth += {"(": 1, ")": -1}.get(text[i], 0)
        i += 1
    return text[:m.start()] + text[i:], m.group(1), text[m.end():i - 1].strip()


def parse_header(path):
    """Function prototypes of the header: [(name, return type, [Param])]."""
    src = open(path).read()
    src = re.sub(r"/\*.*?\*/", " ", src, flags=re.S)
    src = re.sub(r"//[^\n]*", " ", src)
    src = re.sub(r"^\s*#[^\n]*(\\\n[^\n]*)*", " ", src, flags=re.M)        # preprocessor lines
    funcs = []
    for decl in src.split(";"):
        decl = " ".join(decl.split())
        if "(" not in decl or decl.startswith(("typedef", "struct", "enum", "union")) or "{" in decl:
            continue
        m = re.match(r"^(.*?)\b([A-Za-z_]\w*)\s*\((.*)\)\s*$", decl)
        if not m:
            raise ValueError(f"cannot read the declaration '{decl}'")
        ret, name, plist = norm(m.group(1)), m.group(2), m.group(3).strip()
        if ret != "void" and ret not in INT_TYPES:
            raise ValueError(f"{name}: return type '{ret}' is not supported (integers or void)")
        params = []
        if plist and plist != "void":
            depth, cur, parts = 0, "", []
            for ch in plist:                     # split at top-level commas (annotations contain parentheses)
                if ch == "," and depth == 0:
                    parts.append(cur); cur = ""
                    continue
                depth += {"(": 1, ")": -1}.get(ch, 0)
                cur += ch
            parts.append(cur)
            for i, p in enumerate(parts):
                p, direction, count = take_annotation(p.strip())
                pm = re.match(r"^(.*?)(\**)\s*([A-Za-z_]\w*)$", " ".join(p.split()))
                if not pm or not pm.group(1).strip():
                    raise ValueError(f"{name}: parameter {i + 1} ('{p}') needs a type and a name")
                base, stars, pname = norm(pm.group(1).replace("*", " ")), pm.group(2) + "*" * pm.group(1).count("*"), pm.group(3)
                if stars == "":
                    if base not in INT_TYPES:
                        raise ValueError(f"{name}: parameter '{pname}' has type '{base}' (integers only)")
                    if direction:
                        raise ValueError(f"{name}: QPU_{direction} on '{pname}', which is not a pointer")
                    params.append(Param(base, pname))
                elif stars == "*" and base in BUF_TYPES and direction:
                    params.append(Param(" ".join(pm.group(1).split()) + " *", pname, direction, count))
                elif stars == "*" and base in BUF_TYPES:
                    raise ValueError(f"{name}: pointer '{pname}' needs QPU_IN(count), QPU_OUT(count) or QPU_INOUT(count) "
                                     "(the QPU cannot read host memory, so the buffer is copied)")
                else:
                    raise ValueError(f"{name}: parameter '{pname}' ({base} {stars}): only int buffers can be passed by pointer")
        funcs.append((name, ret, params))
    if not funcs:
        raise ValueError(f"no function declarations in {path}")
    return funcs


def c_string(s):
    return '"' + s.replace("\\", "\\\\").replace('"', '\\"') + '"'


def wrapper_source(libname, header, funcs, obj, width, home):
    """One translation unit: the runtime, the embedded object file, and one wrapper per function."""
    sha = hashlib.sha256(obj).hexdigest()
    L = ["/* Generated by qpu_dll.py: each function runs on the quantum machine via qpu_server.py. */",
         f"#define QPU_HOME_DEFAULT {c_string(home)}",
         '#include "qpu_runtime.c"', f'#include "{os.path.basename(header)}"', "",
         "static const unsigned char object[] = {"]
    for i in range(0, len(obj), 20):
        L.append("    " + ", ".join(str(b) for b in obj[i:i + 20]) + ",")
    L += ["};", f'static const qpu__lib lib = {{"{libname}", object, sizeof object, "{sha}", {width}}};', ""]
    for name, ret, params in funcs:
        sig = ", ".join(f"{p.ctype} {p.name}" if p.ctype.endswith("*") else f"{p.ctype} {p.name}" for p in params) or "void"
        L.append(f"QPU__EXPORT {ret} {name}({sig}) {{")
        if params:
            L.append("    long long qpu__a[%d] = {%s};" % (len(params), ", ".join("0" if p.direction else f"(long long){p.name}" for p in params)))
        bufs = [(i, p) for i, p in enumerate(params) if p.direction]
        if bufs:
            L.append("    qpu__buf qpu__b[%d] = {%s};" % (len(bufs), ", ".join(
                f"{{{i}, QPU__{p.direction}, (int *){p.name}, (long)({p.count})}}" for i, p in bufs)))
        call = f'qpu__call(&lib, "{name}", {len(params)}, {"qpu__a" if params else "NULL"}, {"qpu__b" if bufs else "NULL"}, {len(bufs)})'
        L.append(f"    {call};" if ret == "void" else f"    return ({ret}){call};")
        L.append("}")
    return "\n".join(L) + "\n"


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("source", help="C file (compiled for the QPU) or object file")
    ap.add_argument("--header", required=True, help="header that declares the functions to export")
    ap.add_argument("-o", "--output", help="shared library to write (default: libNAME_qpu.so)")
    ap.add_argument("--arch", help="QPU architecture: " + " | ".join(isas.NAMES) + " (default arm64; object files: from the header)")
    ap.add_argument("--cc", help="compiler for the QPU code: clang | gcc | command")
    ap.add_argument("-O", dest="opt", default="1", help="optimization level for the QPU code (default 1)")
    ap.add_argument("--width", type=int, default=16, help="register width of the quantum machine, 8..16 (default 16)")
    ap.add_argument("--target", choices=("linux", "windows"), help="system the library is for (default: this one; "
                    "windows when --host-cc is a MinGW compiler)")
    ap.add_argument("--host-cc", help="C compiler for the library: Linux cc; Windows clang, gcc or cl; "
                    "from Linux to Windows x86_64-w64-mingw32-gcc (default: $CC, else found automatically)")
    ap.add_argument("--qpu-home", help="folder of qpu_server.py on the machine that will run the library "
                    "(default: this repository; set it, or QPU_HOME at run time, when building for another machine)")
    ap.add_argument("--keep", action="store_true", help="keep the generated wrapper source next to the library")
    a = ap.parse_args()
    native = "windows" if os.name == "nt" else "linux"
    target = a.target or ("windows" if a.host_cc and "mingw" in a.host_cc else native)
    host_cc = a.host_cc or os.environ.get("CC") or host_compiler(target, native)
    home = a.qpu_home or (ROOT if target == native else "")
    if target != native and not a.qpu_home:
        print(f"note: building for {target} on {native}: set QPU_HOME on the target machine to the folder of "
              "qpu_server.py (or pass --qpu-home)", file=sys.stderr)
    if not 8 <= a.width <= 16:
        sys.exit("error: --width must be between 8 and 16")
    try:
        funcs = parse_header(a.header)
    except (ValueError, OSError) as e:
        sys.exit(f"error: {a.header}: {e}")
    cfg = cpu_qpu.load_config(os.path.join(ROOT, "config.json"))
    if a.source.endswith(".c"):
        isa = isas.get(a.arch or cfg["arch"])
        cflags = [f for f in cfg["cflags"] if not f.startswith("-O")] + [f"-O{a.opt}"]
        obj_path, _ = cpu_qpu.compile_c(a.source, a.cc or cfg["compiler"], cflags, isa.name)
    else:
        obj_path = a.source
        try:
            found = isas.detect(obj_path)
        except (ValueError, OSError) as e:
            sys.exit(f"error: {e}")
        if a.arch and isas.get(a.arch).name != found:
            sys.exit(f"error: {obj_path} is a {found} file, but --arch {a.arch} was given")
        isa = isas.get(found)
    # every exported function must be in the object, and its arguments must fit in registers
    try:
        have = ObjectImage(obj_path, isa.name).link(None)["functions"]
    except LinkError as e:
        sys.exit(f"error: {e}")
    for name, _, params in funcs:
        if name not in have:
            sys.exit(f"error: {name} is declared in {a.header} but not defined in {a.source}")
        if len(params) > len(isa.arg_regs):
            sys.exit(f"error: {name} has {len(params)} parameters; {isa.title} passes at most {len(isa.arg_regs)} in registers")
    base = os.path.splitext(os.path.basename(a.source))[0]
    out = a.output or (f"{base}_qpu.dll" if target == "windows" else f"lib{base}_qpu.so")
    obj = open(obj_path, "rb").read()
    src = wrapper_source(base, a.header, funcs, obj, a.width, home)
    keep = os.path.splitext(out)[0] + ".wrapper.c"
    if os.path.dirname(out):
        os.makedirs(os.path.dirname(out), exist_ok=True)
    implib = os.path.splitext(out)[0] + ".lib" if target == "windows" else None
    with tempfile.TemporaryDirectory() as d:
        c = keep if a.keep else os.path.join(d, "wrapper.c")
        open(c, "w").write(src)
        cmd = build_command(host_cc, target, c, out, implib,
                            [os.path.join(ROOT, "runtime"), os.path.dirname(os.path.abspath(a.header))])
        p = subprocess.run(cmd, capture_output=True, text=True)
        if p.returncode:
            sys.exit("error: building the library failed:\n" + " ".join(cmd) + "\n" + p.stdout + p.stderr)
    extra = f" (import library {implib})" if implib and os.path.exists(implib) else ""
    print(f"{out}{extra}: {len(funcs)} function(s) on the QPU ({isa.title}, W = {a.width}): " + ", ".join(f[0] for f in funcs))


def host_compiler(target, native):
    """A C compiler for the library on this machine."""
    import shutil
    if target == "linux":
        return "cc"
    names = ["clang", "gcc", "cl"] if native == "windows" else ["x86_64-w64-mingw32-gcc", "x86_64-w64-mingw32-clang"]
    for n in names:
        if shutil.which(n):
            return n
    sys.exit("error: no C compiler for Windows found (tried " + ", ".join(names) + "); pass --host-cc")


def build_command(cc, target, src, out, implib, includes):
    """Compiler command for a shared library. MSVC-style drivers (cl, clang-cl) or GCC-style (gcc, clang)."""
    name = os.path.basename(cc).lower().removesuffix(".exe")
    if name in ("cl", "clang-cl"):
        return [cc, "/nologo", "/LD", "/O2", "/W3"] + [f"/I{i}" for i in includes] + [src, f"/Fe:{out}", "/link", f"/IMPLIB:{implib}", "ws2_32.lib"]
    inc = [x for i in includes for x in ("-I", i)]
    if target == "linux":
        return [cc, "-shared", "-fPIC", "-O2", "-fvisibility=hidden", "-Wall"] + inc + [src, "-o", out, "-lpthread"]
    flavor = subprocess.run([cc, "-dumpmachine"], capture_output=True, text=True).stdout
    implib_flag = [] if "msvc" in flavor else [f"-Wl,--out-implib,{implib}"]   # clang (MSVC target) writes OUT.lib itself
    return [cc, "-shared", "-O2", "-Wall"] + inc + [src, "-o", out] + implib_flag + ["-lws2_32"]


if __name__ == "__main__":
    main()
