"""Build docs/xbox/progress.svg: a treemap progress map in the style of AnyPS5's own (tiles of cells, green = done, grey = not yet).

usage: python tools/xbox/make_progress_svg.py [out.svg]
reads  docs/xbox/results.json              {"compute":{idx:"valid|fail"}, "pixel":{idx:"strict|relaxed|fail"}, "vertex":{...}, "mesh":{...}, "platform":{"dll!fn":"standard|package|shim|fail"}, "todo":[...]}
       docs/xbox/xgr_status.json           {interface: [[method, implemented?], ...]}   (tools/xbox/platform/xgr/status_json.py)
       docs/xbox/bvh_builder_shaders.json  {idx: {name, xbox_intrinsics, lowered_compiles_on_amd_pc, ...}}
Groups: shaders by stage, imports of the package DLLs by imported DLL, GDK runtime methods by interface, BVH builder shaders, and what is still needed.
Colours: green done, amber done only with an opt-in or a shim, grey not yet. A tile's cells are its items (one cell stands for several when a tile has too many)."""
import collections, html, json, os, re, sys

HERE = os.path.dirname(os.path.abspath(__file__))
DOCS = os.path.normpath(os.path.join(HERE, "..", "..", "docs", "xbox"))
out = sys.argv[1] if len(sys.argv) > 1 else os.path.join(DOCS, "progress.svg")
R = json.load(open(os.path.join(DOCS, "results.json")))
XGR = json.load(open(os.path.join(DOCS, "xgr_status.json")))
BVH = json.load(open(os.path.join(DOCS, "bvh_builder_shaders.json")))

BG, GREEN, AMBER, GREY, LINE, TEXT, DIM = "#0d1117", "#2f9e44", "#c99a2e", "#6b7078", "#0d1117", "#e6edf3", "#8b949e"
W, M = 900, 14


def squarify(items, x, y, w, h):
    """items: [(value, payload)] sorted by value desc -> [(payload, x, y, w, h)] (Bruls et al.)"""
    total = float(sum(v for v, _ in items)) or 1.0
    scaled = [(v * w * h / total, p) for v, p in items]
    res = []

    def worst(row, side):
        s = sum(a for a, _ in row)
        return max(max(side * side * a / (s * s), (s * s) / (side * side * a)) for a, _ in row)

    def layout(row, x, y, w, h):
        s = sum(a for a, _ in row)
        if w >= h:                       # lay the row out as a column on the left
            cw = s / h
            yy = y
            for a, p in row:
                rh = a / cw
                res.append((p, x, yy, cw, rh))
                yy += rh
            return x + cw, y, w - cw, h
        rh = s / w
        xx = x
        for a, p in row:
            cw = a / rh
            res.append((p, xx, y, cw, rh))
            xx += cw
        return x, y + rh, w, h - rh

    row = []
    rest = list(scaled)
    while rest:
        side = min(w, h)
        a = rest[0]
        if not row or worst(row + [a], side) <= worst(row, side):
            row.append(a)
            rest.pop(0)
        else:
            x, y, w, h = layout(row, x, y, w, h)
            row = []
    if row:
        layout(row, x, y, w, h)
    return res


def esc(s):
    return html.escape(str(s), quote=True)


def tile_svg(label, counts, x, y, w, h):
    """counts: (green, amber, grey). Cells fill the tile; the label overlays the top-left."""
    g, a, b = counts
    n = g + a + b
    if n == 0 or w < 2 or h < 2:
        return ""
    cell_min = 6.0
    maxcells = max(1, int((w * h) / (cell_min * cell_min)))
    cells = min(n, maxcells)
    cs = (w * h / cells) ** 0.5
    cols = max(1, int(round(w / cs)))
    rows = (cells + cols - 1) // cols
    cw, ch = w / cols, h / max(1, rows)
    ng = round(g * cells / n)
    na = round(a * cells / n)
    if g and ng == 0:
        ng = 1
    if a and na == 0:
        na = 1
    seq = [GREEN] * ng + [AMBER] * na
    seq += [GREY] * (cells - len(seq))
    seq = seq[:cells]
    p = ['<g>']
    paths = collections.defaultdict(list)       # one path per colour: every cell is a subpath
    last = cells - (rows - 1) * cols            # cells in the final row; they are widened to fill it, so no background shows
    for i, c in enumerate(seq):
        r = i // cols
        k = last if r == rows - 1 else cols
        cwi = w / k
        cx, cy = x + (i % cols) * cwi, y + r * ch
        paths[c].append(f"M{cx:.1f} {cy:.1f}h{cwi:.1f}v{ch:.1f}h-{cwi:.1f}z")
    for c, d in paths.items():
        p.append(f'<path d="{"".join(d)}" fill="{c}" stroke="{LINE}" stroke-width="0.6"/>')
    p.append(f'<rect x="{x:.1f}" y="{y:.1f}" width="{w:.1f}" height="{h:.1f}" fill="none" stroke="{LINE}" stroke-width="1.6"/>')
    if label and w > 26 and h > 12:
        t = label
        maxch = int((w - 6) / 5.6)
        if len(t) > maxch:
            t = t[:max(1, maxch - 1)] + ("…" if maxch > 2 else "")
        p.append(f'<text x="{x+4:.1f}" y="{y+11:.1f}" class="tl">{esc(t)}</text>')
    p.append("</g>")
    return "".join(p)


def treemap(tiles, x, y, w, h):
    """tiles: [(label, (g, a, b), weight)]"""
    tiles = sorted(tiles, key=lambda t: -t[2])
    rects = squarify([(t[2], t) for t in tiles], x, y, w, h)
    return "".join(tile_svg(t[0], t[1], rx, ry, rw, rh) for t, rx, ry, rw, rh in rects)


parts = []
y = 24


def group(title, tiles, height):
    global y
    parts.append(f'<text x="{M}" y="{y}" class="h1">{esc(title)}</text>')
    y += 8
    parts.append(treemap(tiles, M, y, W - 2 * M, height))
    y += height + 24


def pct(g, n):
    return "%.1f" % (100.0 * g / n) if n else "0.0"


GOOD = {"valid", "strict", "standard", "package"}
OPT = {"relaxed", "shim"}

# 1. shaders by stage
stage_tiles, tot_g, tot_n = [], 0, 0
for key, name in (("pixel", "Pixel"), ("mesh", "Mesh"), ("compute", "Compute"), ("vertex", "Vertex")):
    v = list(R[key].values())
    g = sum(1 for s in v if s in GOOD)
    a = sum(1 for s in v if s in OPT)
    stage_tiles.append((f"{name} {pct(g, len(v))}% ({g}/{len(v)})", (g, a, len(v) - g - a), len(v)))
    tot_g += g
    tot_n += len(v)
stage_tiles.append(("Geometry 0.0% (0/195), not run", (0, 0, 195), 195))
tot_n += 195
group(f"Shaders*: {pct(tot_g, tot_n)}% ({tot_g}/{tot_n}) valid SPIR-V", stage_tiles, 250)

# 2. platform imports
byd = collections.defaultdict(lambda: [0, 0, 0])
for k, s in R["platform"].items():
    d = k.split("!")[0]
    byd[d][0 if s in GOOD else 1 if s in OPT else 2] += 1
pg = sum(v[0] for v in byd.values())
pa = sum(v[1] for v in byd.values())
pn = sum(sum(v) for v in byd.values())
tiles = [(d.replace(".dll", ""), tuple(v), sum(v)) for d, v in byd.items()]
group(f"Imports of the 16 package DLLs: {pct(pg + pa, pn)}% ({pg + pa}/{pn}) resolve on a PC, {pa} through a stand-in", tiles, 190)

# 3. GDK runtime methods
rt_tiles, rg, rn = [], 0, 0
fam = collections.OrderedDict()
for iface, ms in XGR.items():
    name = re.sub(r"Impl\d*$", "", re.sub(r"^IX", "", iface)) or iface
    f = fam.setdefault(name, [0, 0])
    f[0] += sum(1 for m in ms if m[1])
    f[1] += len(ms)
for name, (d, n) in fam.items():
    rt_tiles.append((name, (d, 0, n - d), n))
    rg += d
    rn += n
group(f"GDK runtime methods implemented (xgameruntime stand-in): {pct(rg, rn)}% ({rg}/{rn})", rt_tiles, 190)

# 4. BVH builder shaders
b_tiles, bg = [], 0
for idx in sorted(BVH, key=int):
    v = BVH[idx]
    ok = v.get("lowered_compiles_on_amd_pc") or v.get("result_on_amd_pc_compiler") == "OK"
    bg += 1 if ok else 0
    b_tiles.append((v["name"], (1, 0, 0) if ok else (0, 0, 1), 1))
group(f"BVH builder shaders compiling on an RX 7800 XT after lowering the Xbox intrinsics: {pct(bg, len(BVH))}% ({bg}/{len(BVH)})", b_tiles, 150)

# 5. still needed
parts.append(f'<text x="{M}" y="{y}" class="h1">Still needed</text>')
y += 18


def cls(n):
    if n.startswith("global_atomic"):
        return "Global atomics"
    if n.startswith("global_load") or n.startswith("global_store"):
        return "Global loads/stores"
    if n.startswith("s_load"):
        return "Scalar loads"
    return "Other"


LOWERED = {
    "BFI", "Ballot64", "GetLaneID", "LaneSwizzle.i32", "MBCNT64",
    "MakeUniform.i32", "MakeUniform.i64", "Max3_F32", "Max3_U32",
    "Med3_F32", "Med3_U32", "Min3_F32", "Min3_U32", "ReadLane.i32",
    "S_BCNT1_U64", "TypedLoad.i32", "V_ALIGNBYTE_B32", "V_CNDMASK_B32",
    "V_PK_MAX_F16", "V_PK_MIN_F16", "WaveMin_U32",
    "global_atomic_add", "global_atomic_sub", "global_atomic_fmin", "global_atomic_fmax",
    "global_load_dword_with_cache", "global_load_dwordx2_with_cache",
    "global_load_dwordx3_with_cache", "global_load_dwordx4_with_cache",
    "global_store_dword_with_cache", "global_store_dwordx2_with_cache",
    "global_store_dwordx3_with_cache", "global_store_dwordx4_with_cache",
    "s_load_dword_with_cache",
}
left = collections.defaultdict(set)
nb = 0
for idx, v in BVH.items():
    if v.get("lowered_compiles_on_amd_pc") or v.get("result_on_amd_pc_compiler") == "OK":
        continue
    nb += 1
    for n in v["xbox_intrinsics"]:
        if cls(n) != "Other" or n not in LOWERED:      # lowered ones only fail these shaders for another intrinsic
            left[cls(n)].add(n)
cols = [("BVH builder: %d shaders use raw 64-bit addresses" % nb, [(k, sorted(v)) for k, v in sorted(left.items())])]
unimpl = [(name, n - d) for name, (d, n) in fam.items() if n - d]
unimpl.sort(key=lambda t: -t[1])
cols.append(("GDK runtime: methods still stubbed", [(k, [str(v)]) for k, v in unimpl[:12]]))
cols.append(("Not started", [(t["title"], [t["note"]]) for t in R.get("todo", [])]))
colw = (W - 2 * M) // 2 - 6
col_y = [y, y]
colx = [M, M + colw + 12]
for ci, (title, rows) in enumerate(cols):
    slot = ci % 2
    cx, cyy = colx[slot], col_y[slot]
    parts.append(f'<text x="{cx}" y="{cyy}" class="h2">{esc(title)}</text>')
    cyy += 14
    for head, names in rows:
        head_t = head if len(head) <= 26 else head[:25] + "…"
        parts.append(f'<text x="{cx+4}" y="{cyy+9}" class="n">{esc(head_t)}</text>')
        maxch = int((colw - 150) / 5.0)
        lines, cur = [], ""
        for w_ in ", ".join(names).split(" "):
            if len(cur) + len(w_) + 1 > maxch and cur:
                lines.append(cur)
                cur = w_
            else:
                cur = (cur + " " + w_).strip()
        if cur:
            lines.append(cur)
        for ln in lines[:3]:
            parts.append(f'<text x="{cx+150}" y="{cyy+9}" class="m">{esc(ln)}</text>')
            cyy += 11
        cyy += 6 if lines else 12
    col_y[slot] = cyy + 18
y = max(col_y) + 6

# legend + footnote
lx = M
for c, t in ((GREEN, "done"), (AMBER, "done only with an opt-in or through a stand-in"), (GREY, "not yet")):
    parts.append(f'<rect x="{lx}" y="{y-9}" width="10" height="10" fill="{c}"/><text x="{lx+15}" y="{y}" class="l">{esc(t)}</text>')
    lx += 40 + 6 * len(t)
y += 18
parts.append(f'<text x="{M}" y="{y}" class="l">* "valid" = the recompiler\'s SPIR-V passes spirv-val; it says nothing about correct output. One cell stands for several shaders in large tiles. Mesh configuration is a guess.</text>')
y += 14
parts.append(f'<text x="{M}" y="{y}" class="l">Regenerate: python tools/xbox/make_progress_svg.py (reads docs/xbox/results.json, xgr_status.json, bvh_builder_shaders.json).</text>')
H = y + 14
svg = (f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {W} {H}" width="{W}" height="{H}" font-family="system-ui,Segoe UI,Arial,sans-serif">'
       f'<style>.h1{{font-size:13px;font-weight:600;fill:{TEXT}}}.h2{{font-size:12px;font-weight:600;fill:{TEXT}}}'
       f'.tl{{font-size:9.5px;fill:#fff;paint-order:stroke;stroke:#0d1117;stroke-width:2.2px;stroke-linejoin:round}}'
       f'.n{{font-size:10px;fill:{TEXT}}}.m{{font-size:9.5px;fill:{DIM};font-family:ui-monospace,Consolas,monospace}}.l{{font-size:10.5px;fill:{DIM}}}</style>'
       f'<rect width="{W}" height="{H}" fill="{BG}"/>' + "".join(parts) + '</svg>')
open(out, "w", encoding="utf-8").write(svg)
print(out, len(svg), "bytes", H, "px high")
