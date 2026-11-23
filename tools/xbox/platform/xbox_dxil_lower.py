"""Rewrite Xbox DXIL text (the output of `dxc -dumpbin`) so it no longer calls the dxcxbox `@xbox.*` intrinsics.

Each intrinsic that has a standard DXIL equivalent is replaced by it; a shader that still calls an intrinsic with no replacement is
reported (exit status 1). The first argument of every xbox.* call (an instruction number) is dropped. The Xbox target triple
(dxil-ms-xb2) and the `xbox.compiler.options` module metadata are rewritten, because the standard validator rejects them.
The Xbox validator also accepts first-class aggregates such as `{ half, half }` and `{ i32, i32 }` (a 64-bit lane mask); standard DXIL does not,
so every such pair value is split into two scalars (its insertvalue/extractvalue chains disappear).
Numeric type IDs (`%N = type ...`) are not supported; the input is expected to use named type tokens as `dxc -dumpbin` does.

usage: python -I xbox_dxil_lower.py in.ll out.ll [address table heap index, default 0]        then: dxasm out.ll out.dxil

Lowered so far (more are added as shaders need them):
  Med3_F32                 max(min(a,b), min(max(a,b),c))                              dx.op.binary.f32
  V_PK_MIN_F16/MAX_F16     lane-wise min/max of a half pair                            dx.op.binary.f16
  GetLaneID                WaveGetLaneIndex
  MakeUniform              WaveReadLaneFirst (v_readfirstlane)
  ReadLane                 WaveReadLaneAt (v_readlane)
  LaneSwizzle              ds_swizzle_b32: offset bit 15 = 0 is the bitmask mode ((lane & and) | or) ^ xor inside a group of 32 lanes
                           (and = off[4:0], or = off[9:5], xor = off[14:10]); bit 15 = 1 is the quad-permute mode. -> WaveReadLaneAt
  Ballot64                 WaveActiveBallot, returning the low two dwords as the 64-bit mask
  V_CNDMASK_B32            (mask pair, {src0, src1}): the mask bit of this lane picks src1, else src0. Argument order inferred from the
                           Hillis-Steele prefix sums these shaders contain (masks 0xAAAAAAAA, 0xCCCCCCCC, 0xF0F0F0F0 with swizzles 30, 60, 120)
  MBCNT64                  number of set mask bits below this lane (v_mbcnt_lo + v_mbcnt_hi)
  S_BCNT1_U64              popcount of the 64-bit mask
  Min3/Max3_F32, Min3/Max3/Med3_U32, BFI, V_ALIGNBYTE_B32, WaveMin_U32, MakeUniform.i64   the obvious standard operations
  TypedLoad.i32            (handle, byte offset, format 65 = 8_UINT): the dword at (offset & ~3) of the raw buffer, byte (offset & 3);
                           only component 0 of the result may be used
  global_load_dword{,x2,x3,x4}_with_cache, s_load_dword_with_cache, global_store_dword{,x2,x3,x4}_with_cache,
  global_atomic_{add,sub,and,or,xor,smin,smax,umin,umax,swap,fmin,fmax}
                           64-bit address -> (resource, offset) through an address table read with ResourceDescriptorHeap[table]
                           (the optional third argument, default 0): a 16-byte header, then 2^ADDRESS_TABLE_LOG2 entries of
                           (base low, base high, size, heap index) sorted by base, unused ones with base 0xFFFFFFFFFFFFFFFF. An unrolled
                           binary search picks the last entry whose base is <= the address; the access goes to
                           ResourceDescriptorHeap[heap index] (a raw UAV) at address - base. fmin/fmax are two integer atomics
                           (signed min/max for a non-negative value, unsigned max/min for a negative one, the other one neutral);
                           their result is the first atomic's. Sets the ResourceDescriptorHeapIndexing shader flag.
  llvm.copysign.f32        bit operations (the Xbox compiler leaves it; it is not a DXIL operation)
Aggregates of up to four i32 (the x2/x3/x4 load results and store operands) are split into scalars like the pairs.
The wave intrinsics assume the 64-lane waves the shaders were built for: run the result with the wave size pinned to 64 (dxil_sm66_wave.py),
unless needs_wave64() finds that the input cannot tell a 64-lane wave from two 32-lane ones: no WaveGetLaneCount, and a thread group of at most
32 threads (lanes 32-63 of the wave are never active) or no standard wave operation and only lane-local intrinsics (LaneSwizzle, which
stays inside its group of 32 lanes, and V_CNDMASK_B32 with a constant mask whose two dwords are equal). Such a shader is left unpinned, so it also runs on GPUs without 64-lane waves.
"""
import re, sys

PAIR_RE = r"\{ (?:half, half|i32, i32|i32, i32, i32|i32, i32, i32, i32) \}"
ADDRESS_TABLE = 0
ADDRESS_TABLE_LOG2 = 12


def split_params(text):
    out, depth, current = [], 0, ""
    for ch in text:
        if ch in "([{":
            depth += 1
        elif ch in ")]}":
            depth -= 1
        if ch == "," and depth == 0:
            out.append(current.strip())
            current = ""
        else:
            current += ch
    if current.strip():
        out.append(current.strip())
    return out


def param_text(header):
    start = header.find("(")
    if start < 0:
        return ""
    depth = 0
    for i in range(start, len(header)):
        if header[i] == "(":
            depth += 1
        elif header[i] == ")":
            depth -= 1
            if depth == 0:
                return header[start + 1:i]
    return ""


def renumber_function(header, body):
    mapping, n, original = {}, 0, 0
    for param in split_params(param_text(header)):
        tokens = param.split()
        names = [t for t in tokens if t.startswith("%")]
        if not names:
            mapping[str(original)] = str(n)
            original += 1
            n += 1
        elif re.fullmatch(r"%\d+", names[-1]):
            mapping[names[-1][1:]] = str(n)
            original = max(original, int(names[-1][1:]) + 1)
            n += 1
        elif not (len(tokens) > 1 and tokens[-1] == names[-1]):
            mapping[str(original)] = str(n)
            original += 1
            n += 1
    lines = [line.strip() for line in body.split("\n")]
    lines = [line for line in lines if line and (not line.startswith(";") or line.startswith("; <label>:"))]
    if lines and not any(re.match(pattern, lines[0]) for pattern in
                         (r"^; <label>:", r"^%?\d+:", r"^[-\w.$]+:\s*$")):
        mapping[str(original)] = str(n)
        original += 1
        n += 1
    for line in lines:
        label = re.match(r"^; <label>:(\d+)", line)
        value = re.match(r"^%(\d+) = ", line)
        numbered_label = re.match(r"^%?(\d+):", line)
        if label or numbered_label:
            mapping[(label or numbered_label).group(1)] = str(n)
            n += 1
        elif value:
            mapping[value.group(1)] = str(n)
            n += 1
    sub = lambda x: "%" + mapping.get(x.group(1), x.group(1))
    header = re.sub(r"(?<=\s)%(\d+)(?=\s*(?:,|\)))", sub, header)
    body = re.sub(r"%(\d+)\b", sub, body)
    body = re.sub(r"^; <label>:(\d+)", lambda x: "; <label>:" + mapping.get(x.group(1), x.group(1)), body, flags=re.M)
    body = re.sub(r"^(\s*)(\d+):", lambda x: x.group(1) + mapping.get(x.group(2), x.group(2)) + ":", body, flags=re.M)
    return header + body


def renumber(text):
    """Unnamed values and blocks (%N, '; <label>:N') must be numbered consecutively; removing instructions leaves holes."""
    out, pos = [], 0
    for m in re.finditer(r"^define [^\n]*\{\n", text, re.M):
        if m.start() < pos:
            continue
        start = m.end()
        end = text.find("\n}\n", start)
        if end < 0:
            continue
        out.append(text[pos:m.start()])
        out.append(renumber_function(text[m.start():start], text[start:end]))
        pos = end
    out.append(text[pos:])
    return "".join(out)


def split_args(s):
    """Split a call's argument text at top-level commas (braces nest)."""
    out, depth, cur = [], 0, ""
    for ch in s:
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
        if ch == "," and depth == 0:
            out.append(cur.strip())
            cur = ""
        else:
            cur += ch
    if cur.strip():
        out.append(cur.strip())
    return out


# declarations a lowering may need: marker -> text
DECLS = {
    "@dx.op.binary.f32(": "declare float @dx.op.binary.f32(i32, float, float) #0",
    "@dx.op.binary.f16(": "declare half @dx.op.binary.f16(i32, half, half) #0",
    "@dx.op.waveGetLaneIndex(": "declare i32 @dx.op.waveGetLaneIndex(i32) #0",
    "@dx.op.waveReadLaneFirst.i32(": "declare i32 @dx.op.waveReadLaneFirst.i32(i32, i32) #1",
    "@dx.op.waveReadLaneAt.i32(": "declare i32 @dx.op.waveReadLaneAt.i32(i32, i32, i32) #1",
    "@dx.op.waveActiveBallot(": "declare %dx.types.fouri32 @dx.op.waveActiveBallot(i32, i1) #1",
    "@dx.op.unaryBits.i32(": "declare i32 @dx.op.unaryBits.i32(i32, i32) #0",
    "@dx.op.binary.i32(": "declare i32 @dx.op.binary.i32(i32, i32, i32) #0",
    "@dx.op.waveActiveOp.i32(": "declare i32 @dx.op.waveActiveOp.i32(i32, i32, i8, i8) #1",
    "@dx.op.waveReadLaneFirst.i64(": "declare i64 @dx.op.waveReadLaneFirst.i64(i32, i64) #1",
    "@dx.op.rawBufferLoad.i32(": "declare %dx.types.ResRet.i32 @dx.op.rawBufferLoad.i32(i32, %dx.types.Handle, i32, i32, i8, i32) #1",
    "@dx.op.rawBufferStore.i32(": "declare void @dx.op.rawBufferStore.i32(i32, %dx.types.Handle, i32, i32, i32, i32, i32, i32, i8, i32)",
    "@dx.op.atomicBinOp.i32(": "declare i32 @dx.op.atomicBinOp.i32(i32, %dx.types.Handle, i32, i32, i32, i32, i32)",
    "@dx.op.createHandleFromHeap(": "declare %dx.types.Handle @dx.op.createHandleFromHeap(i32, i32, i1, i1)",
    "@dx.op.annotateHandle(": "declare %dx.types.Handle @dx.op.annotateHandle(i32, %dx.types.Handle, %dx.types.ResourceProperties)",
}


def insert_decls(text, decls):
    block = "\n".join(decls) + "\n\n"
    for prefix in ("declare ", "define "):
        m = re.search(r"^" + prefix, text, re.M)
        if m:
            return text[:m.start()] + block + text[m.start():]
    return text.rstrip("\n") + "\n\n" + block


def lower(text):
    n_lowered = 0
    pairs = {}            # SSA name of a pair value -> (scalar0, scalar1)
    alias = {}            # SSA name of a scalar produced by extractvalue -> the scalar that replaces it
    bytes_ = {}
    out = []
    counter = [0]
    used = set()

    def fresh():
        counter[0] += 1
        return "%%xl.%d" % counter[0]

    def sub(line):
        if not alias:
            return line
        return re.sub(r"%[\w.]+", lambda m: alias.get(m.group(0), m.group(0)), line)

    ins_re = re.compile(r"^\s*(%[\w.]+) = insertvalue (" + PAIR_RE + r") (undef|%[\w.]+|\{[^}]*\}), (?:half|i32) (%[\w.]+|[-+\w.]+), ([0-3])\s*$")
    ext_re = re.compile(r"^\s*(%[\w.]+) = extractvalue " + PAIR_RE + r" (%[\w.]+), ([0-3])\s*$")
    copysign_re = re.compile(r"^(\s*)(%[\w.]+) = (?:tail )?call float @llvm\.copysign\.f32\(float ([^,]+), float ([^)]+)\)\s*(?:,\s*![^;]*)?(?:;.*)?$")
    store_re = re.compile(r"^(\s*)call void @xbox\.(global_store_dword(?:x[234])?_with_cache)\(i32 \d+, (.*?)\)\s*(?:,\s*![^;]*)?(?:;.*)?$")
    resret_re = re.compile(r"^(\s*)(%[\w.]+) = extractvalue %dx\.types\.ResRet\.i32 (%[\w.]+), (\d+)\s*$")
    call_re = re.compile(r"^(\s*)(%[\w.]+) = call ([^@]+?) @xbox\.([\w.]+)\(i32 \d+(?:, (.*?))?\)\s*(?:,\s*![^;]*)?(?:;.*)?$")

    def operand(a):
        """('%x' | literal pair) -> (s0, s1);  'i32 %x' -> '%x'"""
        m = re.match(r"^" + PAIR_RE + r" (.*)$", a)
        if m:
            v = m.group(1).strip()
            if v.startswith("{"):
                lit = re.findall(r"(?:half|i32) ([^,}\s]+)", v)
                return (lit[0], lit[1])
            return pairs[v]
        return a.split(" ", 1)[1]

    def heap(emit, index, nonuniform):
        r, a = fresh(), fresh()
        emit("%s = call %%dx.types.Handle @dx.op.createHandleFromHeap(i32 218, i32 %s, i1 false, i1 %s)" % (r, index, "true" if nonuniform else "false"))
        emit("%s = call %%dx.types.Handle @dx.op.annotateHandle(i32 216, %%dx.types.Handle %s, %%dx.types.ResourceProperties { i32 4107, i32 0 })" % (a, r))
        used.update(("@dx.op.createHandleFromHeap(", "@dx.op.annotateHandle(", "@dx.op.rawBufferLoad.i32("))
        return a

    def address(emit, addr, nonuniform):
        def i(op, a, b):
            r = fresh()
            emit("%s = %s i32 %s, %s" % (r, op, a, b))
            return r
        table = heap(emit, ADDRESS_TABLE, False)

        def entry(index, mask):
            r = fresh()
            emit("%s = call %%dx.types.ResRet.i32 @dx.op.rawBufferLoad.i32(i32 139, %%dx.types.Handle %s, i32 %s, i32 undef, i8 %d, i32 4)"
                 % (r, table, i("add", i("shl", index, 4), 16), mask))
            comps = []
            for k in range(4 if mask == 15 else 2):
                c = fresh()
                emit("%s = extractvalue %%dx.types.ResRet.i32 %s, %d" % (c, r, k))
                comps.append(c)
            lo, hi, w, base = fresh(), fresh(), fresh(), fresh()
            emit("%s = zext i32 %s to i64" % (lo, comps[0]))
            emit("%s = zext i32 %s to i64" % (hi, comps[1]))
            emit("%s = shl i64 %s, 32" % (w, hi))
            emit("%s = or i64 %s, %s" % (base, w, lo))
            return base, comps
        cur = "0"
        for step in (1 << k for k in range(ADDRESS_TABLE_LOG2 - 1, -1, -1)):
            probe = i("add", cur, step)
            base, _ = entry(probe, 3)
            le, nxt = fresh(), fresh()
            emit("%s = icmp ule i64 %s, %s" % (le, base, addr))
            emit("%s = select i1 %s, i32 %s, i32 %s" % (nxt, le, probe, cur))
            cur = nxt
        base, comps = entry(cur, 15)
        rel, off = fresh(), fresh()
        emit("%s = sub i64 %s, %s" % (rel, addr, base))
        emit("%s = trunc i64 %s to i32" % (off, rel))
        return heap(emit, comps[3], nonuniform), off

    for line in text.split("\n"):
        line = sub(line)
        m = ins_re.match(line)
        if m:
            dst, ty, src, val, idx = m.groups()
            if src.startswith("{"):
                cur = re.findall(r"(?:half|i32) ([^,}\s]+)", src)
            else:
                cur = list(pairs.get(src, ("undef",) * (ty.count(",") + 1)))
            cur[int(idx)] = val
            pairs[dst] = tuple(cur)
            continue
        m = ext_re.match(line)
        if m and m.group(2) in pairs:
            alias[m.group(1)] = pairs[m.group(2)][int(m.group(3))]
            continue
        m = resret_re.match(line)
        if m and m.group(3) in bytes_:
            if m.group(4) != "0":
                raise SystemExit("TypedLoad.i32 result component %s is used; only component 0 is lowered" % m.group(4))
            out.append("%s%s = and i32 %s, 255" % (m.group(1), m.group(2), bytes_[m.group(3)]))
            continue
        m = copysign_re.match(line)
        if m:
            ind, dst, a, b = m.groups()
            ba, bb, ma, mb = fresh(), fresh(), fresh(), fresh()
            out.append("%s%s = bitcast float %s to i32" % (ind, ba, a))
            out.append("%s%s = bitcast float %s to i32" % (ind, bb, b))
            out.append("%s%s = and i32 %s, 2147483647" % (ind, ma, ba))
            out.append("%s%s = and i32 %s, -2147483648" % (ind, mb, bb))
            r = fresh()
            out.append("%s%s = or i32 %s, %s" % (ind, r, ma, mb))
            out.append("%s%s = bitcast i32 %s to float" % (ind, dst, r))
            n_lowered += 1
            continue
        m = store_re.match(line)
        if m:
            ind, name, argtext = m.groups()
            args = split_args(argtext)
            emit = lambda s: out.append(ind + s)
            h, off = address(emit, operand(args[0]), True)
            vals = operand(args[1])
            vals = (vals,) if isinstance(vals, str) else vals
            emit("call void @dx.op.rawBufferStore.i32(i32 140, %%dx.types.Handle %s, i32 %s, i32 undef, %s, i8 %d, i32 4)"
                 % (h, off, ", ".join("i32 " + v for v in (list(vals) + ["undef"] * 4)[:4]), (1 << len(vals)) - 1))
            used.add("@dx.op.rawBufferStore.i32(")
            n_lowered += 1
            continue
        m = call_re.match(line)
        if not m:
            out.append(line)
            continue
        ind, dst, rty, name, argtext = m.groups()
        args = split_args(argtext or "")
        emit = lambda s: out.append(ind + s)

        def lane():
            l = fresh()
            emit("%s = call i32 @dx.op.waveGetLaneIndex(i32 111)" % l)
            used.add("@dx.op.waveGetLaneIndex(")
            return l

        def bin_(kind, ty, a, b, op):
            r = fresh()
            emit("%s = call %s @dx.op.binary.%s(i32 %d, %s %s, %s %s)" % (r, ty, kind, op, ty, a, ty, b))
            used.add("@dx.op.binary.%s(" % kind)
            return r

        def popc(v):
            r = fresh()
            emit("%s = call i32 @dx.op.unaryBits.i32(i32 31, i32 %s)" % (r, v))
            used.add("@dx.op.unaryBits.i32(")
            return r

        def i(op, a, b):
            r = fresh()
            emit("%s = %s i32 %s, %s" % (r, op, a, b))
            return r

        load = re.fullmatch(r"(?:global_load_dword(x[234])?|s_load_dword)_with_cache", name)
        if load:
            n = int(load.group(1)[1]) if load.group(1) else 1
            h, off = address(emit, operand(args[0]), not name.startswith("s_"))
            r = fresh()
            emit("%s = call %%dx.types.ResRet.i32 @dx.op.rawBufferLoad.i32(i32 139, %%dx.types.Handle %s, i32 %s, i32 undef, i8 %d, i32 4)"
                 % (r, h, off, (1 << n) - 1))
            used.add("@dx.op.rawBufferLoad.i32(")
            if n == 1:
                emit("%s = extractvalue %%dx.types.ResRet.i32 %s, 0" % (dst, r))
            else:
                comps = tuple(fresh() for _ in range(n))
                for k, c in enumerate(comps):
                    emit("%s = extractvalue %%dx.types.ResRet.i32 %s, %d" % (c, r, k))
                pairs[dst] = comps
            n_lowered += 1
            continue
        ATOMIC_INT_OPS = {
            "global_atomic_add": 0,
            "global_atomic_and": 1,
            "global_atomic_or": 2,
            "global_atomic_xor": 3,
            "global_atomic_smin": 4,
            "global_atomic_smax": 5,
            "global_atomic_umin": 6,
            "global_atomic_umax": 7,
            "global_atomic_swap": 8,
            "global_atomic_xchg": 8,
        }
        if name in ATOMIC_INT_OPS or name in ("global_atomic_sub", "global_atomic_fmin", "global_atomic_fmax"):
            h, off = address(emit, operand(args[0]), True)
            v = operand(args[1])
            used.add("@dx.op.atomicBinOp.i32(")

            def atomic(op, value, d=None):
                r = d or fresh()
                emit("%s = call i32 @dx.op.atomicBinOp.i32(i32 78, %%dx.types.Handle %s, i32 %d, i32 %s, i32 undef, i32 undef, i32 %s)" % (r, h, op, off, value))
                return r
            if name in ATOMIC_INT_OPS:
                atomic(ATOMIC_INT_OPS[name], v, dst)
            elif name == "global_atomic_sub":
                atomic(0, i("sub", 0, v), dst)
            else:
                bits, neg = fresh(), fresh()
                emit("%s = bitcast float %s to i32" % (bits, v))
                emit("%s = icmp slt i32 %s, 0" % (neg, bits))
                lo, hi = fresh(), fresh()
                if name == "global_atomic_fmin":
                    emit("%s = select i1 %s, i32 2147483647, i32 %s" % (lo, neg, bits))
                    emit("%s = select i1 %s, i32 %s, i32 0" % (hi, neg, bits))
                    first = atomic(4, lo)
                    atomic(7, hi)
                else:
                    emit("%s = select i1 %s, i32 -2147483648, i32 %s" % (lo, neg, bits))
                    emit("%s = select i1 %s, i32 %s, i32 -1" % (hi, neg, bits))
                    first = atomic(5, lo)
                    atomic(6, hi)
                emit("%s = bitcast i32 %s to float" % (dst, first))
            n_lowered += 1
            continue
        if name == "Med3_F32":
            a, b, c = [operand(x) for x in args]
            lo, hi = bin_("f32", "float", a, b, 36), bin_("f32", "float", a, b, 35)
            t = bin_("f32", "float", hi, c, 36)
            emit("%s = call float @dx.op.binary.f32(i32 35, float %s, float %s)" % (dst, lo, t))
        elif name in ("V_PK_MIN_F16", "V_PK_MAX_F16"):
            a, b = operand(args[0]), operand(args[1])
            op = 36 if name.endswith("MIN_F16") else 35
            pairs[dst] = tuple(bin_("f16", "half", a[k], b[k], op) for k in range(2))
        elif name == "GetLaneID":
            emit("%s = call i32 @dx.op.waveGetLaneIndex(i32 111)" % dst)
            used.add("@dx.op.waveGetLaneIndex(")
        elif name == "MakeUniform.i32":
            emit("%s = call i32 @dx.op.waveReadLaneFirst.i32(i32 118, i32 %s)" % (dst, operand(args[0])))
            used.add("@dx.op.waveReadLaneFirst.i32(")
        elif name == "ReadLane.i32":
            emit("%s = call i32 @dx.op.waveReadLaneAt.i32(i32 117, i32 %s, i32 %s)" % (dst, operand(args[0]), operand(args[1])))
            used.add("@dx.op.waveReadLaneAt.i32(")
        elif name == "LaneSwizzle.i32":
            v, off = operand(args[0]), int(operand(args[1]))
            l = lane()
            if off & 0x8000:                         # quad permute: lane' = (lane & ~3) | ((off >> 2*(lane&3)) & 3)
                q = i("and", l, 3)
                sh = i("shl", q, 1)
                s = i("lshr", str(off & 0xFF), sh)
                p = i("and", s, 3)
                base = i("and", l, -4)
                tgt = i("or", base, p)
            else:                                    # bitmask: ((lane & and) | or) ^ xor, inside a group of 32
                t1 = i("and", l, off & 31)
                t2 = i("or", t1, (off >> 5) & 31)
                t3 = i("xor", t2, (off >> 10) & 31)
                base = i("and", l, -32)
                tgt = i("or", base, t3)
            emit("%s = call i32 @dx.op.waveReadLaneAt.i32(i32 117, i32 %s, i32 %s)" % (dst, v, tgt))
            used.add("@dx.op.waveReadLaneAt.i32(")
        elif name == "Ballot64":
            c = fresh()
            emit("%s = icmp ne i32 %s, 0" % (c, operand(args[0])))
            b = fresh()
            emit("%s = call %%dx.types.fouri32 @dx.op.waveActiveBallot(i32 116, i1 %s)" % (b, c))
            used.add("@dx.op.waveActiveBallot(")
            lo, hi = fresh(), fresh()
            emit("%s = extractvalue %%dx.types.fouri32 %s, 0" % (lo, b))
            emit("%s = extractvalue %%dx.types.fouri32 %s, 1" % (hi, b))
            pairs[dst] = (lo, hi)
        elif name == "V_CNDMASK_B32":
            lo, hi = operand(args[0])
            s0, s1 = operand(args[1])
            l = lane()
            ishi = fresh()
            emit("%s = icmp uge i32 %s, 32" % (ishi, l))
            sh = i("and", l, 31)
            blo, bhi = i("lshr", lo, sh), i("lshr", hi, sh)
            w = fresh()
            emit("%s = select i1 %s, i32 %s, i32 %s" % (w, ishi, bhi, blo))
            bit = i("and", w, 1)
            c = fresh()
            emit("%s = icmp ne i32 %s, 0" % (c, bit))
            emit("%s = select i1 %s, i32 %s, i32 %s" % (dst, c, s1, s0))
        elif name == "MBCNT64":
            lo, hi = operand(args[0])
            l = lane()
            ishi = fresh()
            emit("%s = icmp uge i32 %s, 32" % (ishi, l))
            sh = i("and", l, 31)
            below = i("add", i("shl", "1", sh), -1)                 # (1 << sh) - 1
            lo_m = fresh()
            emit("%s = select i1 %s, i32 -1, i32 %s" % (lo_m, ishi, below))      # lanes >= 32 see all of the low word
            hi_m = fresh()
            emit("%s = select i1 %s, i32 %s, i32 0" % (hi_m, ishi, below))
            a = popc(i("and", lo, lo_m))
            b = popc(i("and", hi, hi_m))
            emit("%s = add i32 %s, %s" % (dst, a, b))
        elif name == "S_BCNT1_U64":
            lo, hi = operand(args[0])
            emit("%s = add i32 %s, %s" % (dst, popc(lo), popc(hi)))
        elif name in ("Min3_F32", "Max3_F32"):
            a, b, c = [operand(x) for x in args]
            op = 36 if name.startswith("Min") else 35
            emit("%s = call float @dx.op.binary.f32(i32 %d, float %s, float %s)" % (dst, op, bin_("f32", "float", a, b, op), c))
            used.add("@dx.op.binary.f32(")
        elif name in ("Min3_U32", "Max3_U32", "Med3_U32"):
            a, b, c = [operand(x) for x in args]
            used.add("@dx.op.binary.i32(")
            def ub(op, x, y, d=None):
                r = d or fresh()
                emit("%s = call i32 @dx.op.binary.i32(i32 %d, i32 %s, i32 %s)" % (r, op, x, y))
                return r
            if name == "Min3_U32":
                ub(40, ub(40, a, b), c, dst)
            elif name == "Max3_U32":
                ub(39, ub(39, a, b), c, dst)
            else:                                    # median: max(min(a,b), min(max(a,b), c))
                lo, hi = ub(40, a, b), ub(39, a, b)
                ub(39, lo, ub(40, hi, c), dst)
        elif name == "BFI":                          # v_bfi_b32: (mask & x) | (~mask & y)
            m, x, y = [operand(a) for a in args]
            nm = i("xor", m, -1)
            emit("%s = or i32 %s, %s" % (dst, i("and", m, x), i("and", nm, y)))
        elif name == "V_ALIGNBYTE_B32":              # ({hi, lo} >> (8 * sh[1:0]))[31:0]
            hi, lo, sh = [operand(a) for a in args]
            sb = i("shl", i("and", sh, 3), 3)
            right = i("lshr", lo, sb)
            nz = fresh()
            emit("%s = icmp ne i32 %s, 0" % (nz, sb))
            left = i("shl", hi, i("and", i("sub", 32, sb), 31))
            emit("%s = select i1 %s, i32 %s, i32 0" % (fresh(), nz, left))
            sel = "%%xl.%d" % counter[0]
            emit("%s = or i32 %s, %s" % (dst, right, sel))
        elif name == "WaveMin_U32":
            emit("%s = call i32 @dx.op.waveActiveOp.i32(i32 119, i32 %s, i8 2, i8 1)" % (dst, operand(args[0])))
            used.add("@dx.op.waveActiveOp.i32(")
        elif name == "MakeUniform.i64":
            emit("%s = call i64 @dx.op.waveReadLaneFirst.i64(i32 118, i64 %s)" % (dst, operand(args[0])))
            used.add("@dx.op.waveReadLaneFirst.i64(")
        elif name == "TypedLoad.i32":
            h, off = operand(args[0]), operand(args[1])
            fmt = operand(args[2]) if len(args) > 2 else "65"
            used.add("@dx.op.rawBufferLoad.i32(")
            if fmt == "65":
                r = fresh()
                emit("%s = call %%dx.types.ResRet.i32 @dx.op.rawBufferLoad.i32(i32 139, %%dx.types.Handle %s, i32 %s, i32 undef, i8 1, i32 4)"
                     % (r, h, i("and", off, -4)))
                w = fresh()
                emit("%s = extractvalue %%dx.types.ResRet.i32 %s, 0" % (w, r))
                bytes_[dst] = i("lshr", w, i("shl", i("and", off, 3), 3))
            else:
                emit("%s = call %%dx.types.ResRet.i32 @dx.op.rawBufferLoad.i32(i32 139, %%dx.types.Handle %s, i32 %s, i32 undef, i8 1, i32 4)"
                     % (dst, h, off))
        else:
            out.append(line)
            continue
        n_lowered += 1
    res = "\n".join(out)
    res = re.sub(r'target triple = "dxil-ms-xb\d*"', 'target triple = "dxil-ms-dx"', res)
    res = re.sub(r"!xbox\.compiler\.options = [^\n]*\n", "", res)
    # drop declarations of intrinsics that are gone; add the standard ones that are now used
    still = set(re.findall(r"call [^@\n]*@(xbox\.[\w.]+|llvm\.copysign\.f32)\(", res))
    for nm in set(re.findall(r"^declare [^@\n]*@(xbox\.[\w.]+|llvm\.copysign\.f32)\(", res, re.M)):
        if nm not in still:
            res = re.sub(r"(; Function Attrs:[^\n]*\n)?declare [^\n]*@" + re.escape(nm) + r"\([^\n]*\n\n?", "", res)
    add = [DECLS[k] for k in sorted(used) if not re.search(r"^declare [^\n]*" + re.escape(k), res, re.M)]
    if add:
        res = insert_decls(res, add)
    if "%dx.types.fouri32" in res and "%dx.types.fouri32 = type" not in res:
        res = res.replace("%dx.types.Handle = type { i8* }\n", "%dx.types.Handle = type { i8* }\n%dx.types.fouri32 = type { i32, i32, i32, i32 }\n", 1)
    if "@dx.op.createHandleFromHeap(" in used:
        res = re.sub(r"^(!\d+ = !\{i32 0, i64 )(\d+)", lambda m: m.group(1) + str(int(m.group(2)) | 1 << 30), res, count=1, flags=re.M)
    if "%dx.types.ResRet.i32" in res and "%dx.types.ResRet.i32 = type" not in res:
        res = res.replace("%dx.types.Handle = type { i8* }\n", "%dx.types.Handle = type { i8* }\n%dx.types.ResRet.i32 = type { i32, i32, i32, i32, i32 }\n", 1)
    if "%dx.types.ResourceProperties" in res and "%dx.types.ResourceProperties = type" not in res:
        res = res.replace("%dx.types.Handle = type { i8* }\n", "%dx.types.Handle = type { i8* }\n%dx.types.ResourceProperties = type { i32, i32 }\n", 1)
    res = renumber(res)
    left = set(re.findall(r"call [^@\n]*@(xbox\.[\w.]+)\(", res))
    return res, n_lowered, left


WAVE64_INTRINSICS = ("GetLaneID", "MakeUniform.", "ReadLane.", "Ballot64", "V_CNDMASK_B32", "MBCNT64", "S_BCNT1_U64", "WaveMin_U32")


def needs_wave64(text):
    if "@dx.op.waveGetLaneCount" in text:
        return True
    props = re.search(r"^!\d+ = !\{i32 0, i64 \d+(?:, i32 \d+, ![^,}]+)*?, i32 4, !(\d+)", text, re.M)
    if props:
        dims = re.search(r"^!%s = !\{i32 (\d+), i32 (\d+), i32 (\d+)\}" % props.group(1), text, re.M)
        if dims and int(dims.group(1)) * int(dims.group(2)) * int(dims.group(3)) <= 32:
            return False
    if "@dx.op.wave" in text:
        return True
    for name, rest in re.findall(r"call [^@\n]*@xbox\.([\w.]+)\(i32 \d+([^\n]*)", text):
        if name == "V_CNDMASK_B32":
            mask = re.match(r", \{ i32, i32 \} \{ i32 (-?\d+), i32 (-?\d+) \}", rest)
            if mask and mask.group(1) == mask.group(2):
                continue
        if name.startswith(WAVE64_INTRINSICS):
            return True
    return False


if __name__ == "__main__":
    text = open(sys.argv[1], encoding="utf-8", errors="ignore").read()
    if len(sys.argv) > 3:
        ADDRESS_TABLE = int(sys.argv[3])
    res, n, left = lower(text)
    open(sys.argv[2], "w", encoding="utf-8", newline="\n").write(res)
    print("lowered %d call(s); intrinsics still called: %s" % (n, sorted(left) or "none"))
    sys.exit(1 if left else 0)
