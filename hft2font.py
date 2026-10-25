#!/usr/bin/env python3
"""
hft2font - convert Hancom HFT ("Han Unified Font File 1.0") fonts to OpenType (.otf, CFF)
and/or TrueType (.ttf).

  python3 hft2font.py [options] FILE_OR_DIR...

Options
  -o DIR            output directory (default: ./converted)
  -f otf|ttf|both   output format (default: both)
  --code-dir DIR    Hancom code tables (u300_*.dat). Default: looked up relative to the
                    input (…/Resources/Hnc/Bin/Code) or next to this script.
  --merge           additionally build one combined font per typeface name
                    (e.g. Hangul + Hanja + Latin + symbol files of "신명 신명조")
  --no-merge-only   (default) write per-file fonts; use --merge-only to skip them
  --keep-overlaps   do not remove contour overlaps (requires skia-pathops otherwise)
  -j N              parallel jobs (default: number of CPUs)

Requires: fontTools (>=4.30); skia-pathops is optional but recommended.
"""
import argparse
import os
import sys
import traceback

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import hftlib  # noqa: E402
from hftlib import HFTFont, HFTError  # noqa: E402
from hwpmap import HwpUnicodeMap  # noqa: E402

from fontTools.fontBuilder import FontBuilder  # noqa: E402
from fontTools.pens.t2CharStringPen import T2CharStringPen  # noqa: E402
from fontTools.pens.ttGlyphPen import TTGlyphPen  # noqa: E402
from fontTools.pens.cu2quPen import Cu2QuPen  # noqa: E402
from fontTools.pens.recordingPen import RecordingPen  # noqa: E402
from fontTools.pens.boundsPen import ControlBoundsPen  # noqa: E402

try:
    import pathops
except ImportError:  # pragma: no cover
    pathops = None

UPM = 1000
ASC, DESC = 800, -200     # HWP lays HFT text out with ascent 80% / descent 20% of the em


# ------------------------------------------------------------------ geometry
def outline_to_rec(contours, em):
    """HFT contours (em units, y=0 at bottom of em box) -> RecordingPen in UPM units."""
    k = UPM / float(em)
    sh = 0.2 * em

    def t(p):
        return (round(p[0] * k), round((p[1] - sh) * k))
    rec = RecordingPen()
    for c in contours:
        if len(c) < 2:
            continue
        rec.moveTo(t(c[0][1]))
        last = t(c[0][1])
        for seg in c[1:]:
            if seg[0] == "L":
                q = t(seg[1])
                if q != last:
                    rec.lineTo(q)
                    last = q
            else:
                p1, p2, p3 = (t(x) for x in seg[1])
                rec.curveTo(p1, p2, p3)
                last = p3
        rec.closePath()
    return rec


def bitmap_to_rec(bm, key):
    """Trace a 1-bit bitmap into pixel-outline polygons (UPM units)."""
    x0, ytop, w, h, rows = bm
    k = UPM / float(key)
    sh = 0.2 * key
    on = set()
    for r, v in enumerate(rows):
        for c in range(w):
            if (v >> (w - 1 - c)) & 1:
                on.add((c, r))
    if not on:
        return RecordingPen()
    # directed boundary edges, counter-clockwise outer in a y-up frame
    # pixel (c, r) covers x[c, c+1], Y[ytop-r-1, ytop-r] ; use integer grid (c, Y)
    edges = {}

    def add(a, b):
        edges.setdefault(a, []).append(b)
    for (c, r) in on:
        Y = -r  # relative; converted later
        if (c, r + 1) not in on:
            add((c, Y - 1), (c + 1, Y - 1))
        if (c + 1, r) not in on:
            add((c + 1, Y - 1), (c + 1, Y))
        if (c, r - 1) not in on:
            add((c + 1, Y), (c, Y))
        if (c - 1, r) not in on:
            add((c, Y), (c, Y - 1))
    rec = RecordingPen()
    while edges:
        start = next(iter(edges))
        loop = [start]
        cur = start
        prev_dir = None
        while True:
            outs = edges[cur]
            if len(outs) > 1 and prev_dir is not None:
                # at a pinch point prefer the left turn so loops stay simple
                def turn(b):
                    d = (b[0] - cur[0], b[1] - cur[1])
                    return -(prev_dir[0] * d[1] - prev_dir[1] * d[0])
                outs.sort(key=turn)
            nxt = outs.pop(0)
            if not outs:
                del edges[cur]
            prev_dir = (nxt[0] - cur[0], nxt[1] - cur[1])
            cur = nxt
            if cur == start:
                break
            loop.append(cur)
        # drop collinear points
        pts = []
        n = len(loop)
        for i in range(n):
            a, b, c2 = loop[i - 1], loop[i], loop[(i + 1) % n]
            if (b[0] - a[0]) * (c2[1] - b[1]) - (b[1] - a[1]) * (c2[0] - b[0]) != 0:
                pts.append(b)
        if len(pts) < 3:
            continue

        def t(p):
            return (round((x0 + p[0]) * k), round((ytop + p[1] - sh) * k))
        rec.moveTo(t(pts[0]))
        for p in pts[1:]:
            rec.lineTo(t(p))
        rec.closePath()
    return rec


def clean(rec, clockwise):
    """Remove overlaps / normalise winding with skia-pathops."""
    if pathops is None or not rec.value:
        return rec
    path = pathops.Path()
    rec.replay(path.getPen())
    try:
        path = pathops.simplify(path, fix_winding=True, clockwise=clockwise)
    except Exception:
        return rec
    out = RecordingPen()
    path.draw(out)
    return out


# ------------------------------------------------------------------ font assembly
class Glyph:
    __slots__ = ("code", "rec", "adv")


def collect_glyphs(sources, keep_overlaps=False, clockwise=False):
    """sources: list of HFTFont in priority order. -> dict code -> Glyph"""
    glyphs = {}
    for f in sources:
        if f.outline_sections:
            for code in f.all_codes():
                if code in glyphs:
                    continue
                try:
                    r = f.glyph_contours(code)
                except Exception:
                    r = None
                if r is None:
                    continue
                g = Glyph()
                g.code = code
                g.rec = outline_to_rec(*r)
                glyphs[code] = (g, f)
        else:
            for key, secs in sorted(f.bitmap_sets, key=lambda t: -t[0]):
                for code in f.all_codes(secs):
                    if code in glyphs:
                        continue
                    try:
                        bm = f.bitmap_glyph(code, secs)
                    except Exception:
                        bm = None
                    if bm is None:
                        continue
                    g = Glyph()
                    g.code = code
                    g.rec = bitmap_to_rec(bm, key)
                    glyphs[code] = (g, f)
    out = {}
    for code, (g, f) in glyphs.items():
        if not keep_overlaps:
            g.rec = clean(g.rec, clockwise)
        a = f.advance(code)
        if a is None:          # HWP falls back to the typeface default width (= full em)
            a = f.width_em
        if a == 0 and code == 0x20:  # some Hanyang Latin fonts declare a zero-width space
            a0 = f.advance(0x30)
            a = (a0 // 2) if a0 else f.width_em // 4
        a_upm = a * UPM / float(f.width_em)
        g.adv = max(0, int(round(a_upm)))
        out[code] = g
    return out


def build(glyphs, cmap, names, path, fmt):
    code_uni = {}
    for u, c in sorted(cmap.items()):
        code_uni.setdefault(c, u)

    def gname(code):
        u = code_uni.get(code)
        if u is not None:
            if u >= 0xF0000:
                return "hwp%04X" % code
            return "uni%04X" % u if u <= 0xFFFF else "u%06X" % u
        return "hwp%04X" % code if code < 0x10000 else "space%d" % code
    order_codes = sorted(glyphs, key=lambda c: (code_uni.get(c, 0x110000), c))
    names_by_code = {c: gname(c) for c in order_codes}
    glyph_order = [".notdef"] + [names_by_code[c] for c in order_codes]

    fb = FontBuilder(UPM, isTTF=(fmt == "ttf"))
    fb.setupGlyphOrder(glyph_order)
    fb.setupCharacterMap({u: names_by_code[c] for u, c in cmap.items()})

    metrics = {".notdef": (500, 0)}
    notdef = RecordingPen()
    for (a, b, c, d) in ((50, -150, 450, 750), (100, -100, 400, 700)):
        notdef.moveTo((a, b))
        if a == 50:
            notdef.lineTo((c, b)); notdef.lineTo((c, d)); notdef.lineTo((a, d))
        else:
            notdef.lineTo((a, d)); notdef.lineTo((c, d)); notdef.lineTo((c, b))
        notdef.closePath()
    recs = {".notdef": notdef}
    for c in order_codes:
        g = glyphs[c]
        recs[names_by_code[c]] = g.rec
        metrics[names_by_code[c]] = (g.adv, 0)

    if fmt == "ttf":
        ttglyphs = {}
        for n in glyph_order:
            pen = TTGlyphPen(None)
            recs[n].replay(Cu2QuPen(pen, max_err=1.0, reverse_direction=True))
            ttglyphs[n] = pen.glyph()
        fb.setupGlyf(ttglyphs)
        glyf = fb.font["glyf"]
        for n in glyph_order:
            g = glyf[n]
            g.recalcBounds(glyf)
            metrics[n] = (metrics[n][0], getattr(g, "xMin", 0) if g.numberOfContours else 0)
    else:
        charstrings = {}
        for n in glyph_order:
            pen = T2CharStringPen(metrics[n][0], None)
            recs[n].replay(pen)
            charstrings[n] = pen.getCharString()
        fb.setupCFF(names["ps"], {"FullName": names["full_en"], "FamilyName": names["family_en"],
                                  "Weight": "Regular", "Notice": names.get("copyright", "")[:250]},
                    charstrings, {})
        cff = fb.font["CFF "].cff
        cs = cff[cff.fontNames[0]].CharStrings
        for n in glyph_order:
            b = cs[n].calcBounds(cs)
            metrics[n] = (metrics[n][0], int(b[0]) if b else 0)
    fb.setupHorizontalMetrics(metrics)
    fb.setupHorizontalHeader(ascent=ASC, descent=DESC, lineGap=0)
    nm = {
        "familyName": {"en": names["family_en"], "ko": names["family_ko"]},
        "styleName": "Regular",
        "uniqueFontIdentifier": "HFT2Font: %s" % names["ps"],
        "fullName": {"en": names["full_en"], "ko": names["family_ko"]},
        "psName": names["ps"],
        "version": "Version 1.000",
    }
    if names.get("copyright"):
        nm["copyright"] = names["copyright"]
    if names.get("vendor"):
        nm["manufacturer"] = {"en": names["vendor"], "ko": names["vendor"]}
    if names.get("description"):
        nm["description"] = {"en": names["description"], "ko": names["description"]}
    fb.setupNameTable(nm, windows=True, mac=False)
    # vertical extents
    ymin, ymax = DESC, ASC
    if fmt == "ttf":
        glyf = fb.font["glyf"]
        for n in glyph_order:
            g = glyf[n]
            if g.numberOfContours:
                ymin, ymax = min(ymin, g.yMin), max(ymax, g.yMax)
    else:
        for n in glyph_order:
            b = cs[n].calcBounds(cs)
            if b:
                ymin, ymax = min(ymin, b[1]), max(ymax, b[3])
    fb.setupOS2(version=4, sTypoAscender=ASC, sTypoDescender=DESC, sTypoLineGap=0,
                usWinAscent=int(ymax), usWinDescent=int(-ymin), fsType=0,
                fsSelection=0x40 | 0x80, achVendID="HNC ",
                ulCodePageRange1=(1 << 0) | (1 << 19), ulCodePageRange2=0)
    fb.setupPost(keepGlyphNames=(fmt == "otf"))
    fb.font["OS/2"].recalcUnicodeRanges(fb.font)
    tmp = path + ".part"
    fb.save(tmp)
    os.replace(tmp, path)


def make_names(stem, face, f):
    ps = "".join(ch for ch in stem if ch.isalnum() or ch in "-_")[:60] or "HFTFont"
    face = face or stem
    ascii_face = face.isascii() if hasattr(face, "isascii") else all(ord(c) < 128 for c in face)
    fam_en = ("%s %s" % (face, stem)) if ascii_face else stem
    fam_ko = "%s (%s)" % (face, stem)
    return {
        "ps": ps, "family_en": fam_en, "family_ko": fam_ko,
        "full_en": fam_en, "copyright": f.copyright, "vendor": f.vendor,
        "description": f.description,
    }


# ------------------------------------------------------------------ driver
_MAP = None


def _get_map(code_dir):
    global _MAP
    if _MAP is None:
        _MAP = HwpUnicodeMap(code_dir)
    return _MAP


def convert_job(job):
    srcs, stem, outdir, fmts, code_dir, keep = job
    try:
        hm = _get_map(code_dir)
        if not hftlib.EXTRA_HANGUL_CODES:
            hftlib.EXTRA_HANGUL_CODES[:] = sorted(set(
                h for h in list(hm.uni2hwp.values()) + [e[1] for e in hm.extra] if h >= 0x8000))
        fonts = [HFTFont(p) for p in srcs]
        glyphs = collect_glyphs(fonts, keep, clockwise=False)
        if not glyphs:
            return (stem, "skipped (no glyphs)")
        cmap = _get_map(code_dir).build_cmap(set(glyphs))
        # glyphs without a Unicode equivalent stay reachable via Plane-15 PUA (U+F0000 + HWP code)
        mapped = set(cmap.values())
        for c in glyphs:
            if c not in mapped:
                cmap[0xF0000 + c] = c
        # every font gets a space (HWP takes it from the Latin font)
        if 0x20 not in cmap:
            g = Glyph()
            g.code, g.rec, g.adv = 0x10000, RecordingPen(), UPM // 2  # pseudo code
            glyphs[0x10000] = g
            cmap[0x20] = 0x10000
            if 0x3000 not in cmap:
                g2 = Glyph()
                g2.code, g2.rec, g2.adv = 0x10001, RecordingPen(), UPM
                glyphs[0x10001] = g2
                cmap[0x3000] = 0x10001
        if stem.startswith("MERGED_"):
            face = fonts[0].face_name.replace(" Oblique", "").strip()
            first = os.path.splitext(os.path.basename(srcs[0]))[0]
            names = make_names(first, face, fonts[0])
            names["family_ko"] = face
            names["family_en"] = names["full_en"] = first + " Combined"
            names["ps"] = "HFT-" + first + "-Combined"
            if all(ord(ch) < 128 for ch in face):
                names["family_en"] = names["full_en"] = face
        else:
            names = make_names(stem, fonts[0].face_name, fonts[0])
        kind = "outline" if any(f.outline_sections for f in fonts) else "bitmap-traced"
        for fmt in fmts:
            sub = os.path.join(outdir, fmt.upper())
            os.makedirs(sub, exist_ok=True)
            fname = stem[7:] if stem.startswith("MERGED_") else stem
            build(glyphs, cmap, names, os.path.join(sub, fname + "." + fmt), fmt)
        return (stem, "%d glyphs, %d unicode, %s%s" % (len(glyphs), len(cmap), kind,
                ("  <- " + "+".join(os.path.basename(p) for p in srcs)) if len(srcs) > 1 else ""))
    except Exception as e:
        return (stem, "ERROR %s\n%s" % (e, traceback.format_exc()))


def find_code_dir(inputs, given):
    if given:
        return given
    cands = []
    for p in inputs:
        p = os.path.abspath(p)
        d = p if os.path.isdir(p) else os.path.dirname(p)
        cands.append(os.path.join(d, "..", "..", "Bin", "Code"))
        cands.append(os.path.join(d, "Code"))
    cands.append(os.path.join(os.path.dirname(os.path.abspath(__file__)), "Code"))
    for c in cands:
        if os.path.exists(os.path.join(c, "u300_gen.dat")):
            return os.path.normpath(c)
    sys.exit("Hancom code tables (u300_*.dat) not found; use --code-dir")


def main():
    ap = argparse.ArgumentParser(description="Convert Hancom HFT fonts to OTF/TTF")
    ap.add_argument("inputs", nargs="+")
    ap.add_argument("-o", "--out", default="converted")
    ap.add_argument("-f", "--format", default="both", choices=["otf", "ttf", "both"])
    ap.add_argument("--code-dir")
    ap.add_argument("--merge", action="store_true")
    ap.add_argument("--merge-only", action="store_true")
    ap.add_argument("--keep-overlaps", action="store_true")
    ap.add_argument("-j", "--jobs", type=int, default=os.cpu_count() or 1)
    ap.add_argument("--skip-existing", action="store_true",
                    help="skip fonts whose outputs already exist (resume)")
    ap.add_argument("--progress", help="file listing finished jobs (resume support)")
    ap.add_argument("--only", help="comma separated list of file stems to process")
    a = ap.parse_args()
    files = []
    for p in a.inputs:
        if os.path.isdir(p):
            files += [os.path.join(p, n) for n in sorted(os.listdir(p)) if n.upper().endswith(".HFT")]
        else:
            files.append(p)
    if a.only:
        want = set(s.upper() for s in a.only.split(","))
        files = [p for p in files if os.path.splitext(os.path.basename(p))[0].upper() in want]
    code_dir = find_code_dir(a.inputs, a.code_dir)
    fmts = ["otf", "ttf"] if a.format == "both" else [a.format]
    if pathops is None and not a.keep_overlaps:
        print("note: skia-pathops not installed, overlaps are kept", file=sys.stderr)
    jobs = []
    if not a.merge_only:
        for p in files:
            stem = os.path.splitext(os.path.basename(p))[0]
            jobs.append(([p], stem, a.out, fmts, code_dir, a.keep_overlaps))
    if a.merge or a.merge_only:
        jobs += merge_jobs(files, a, fmts, code_dir)
    if a.skip_existing:
        def done(j):
            srcs, stem, outdir = j[0], j[1], j[2]
            fname = stem[7:] if stem.startswith("MERGED_") else stem
            return all(os.path.exists(os.path.join(outdir, f.upper(), fname + "." + f)) for f in fmts)
        jobs = [j for j in jobs if not done(j)]
        # biggest first so long jobs start early
    done_stems = set()
    if a.progress and os.path.exists(a.progress):
        with open(a.progress, encoding="utf-8") as fh:
            done_stems = set(l.strip() for l in fh if l.strip())
        jobs = [j for j in jobs if j[1] not in done_stems]
    jobs.sort(key=lambda j: -sum(os.path.getsize(p) for p in j[0]))

    def report(stem, msg):
        print("%-12s %s" % (stem, msg), flush=True)
        if a.progress and not msg.startswith("ERROR"):
            with open(a.progress, "a", encoding="utf-8") as fh:
                fh.write(stem + "\n")
    if a.jobs > 1 and len(jobs) > 1:
        import multiprocessing as mp
        with mp.Pool(a.jobs, maxtasksperchild=4) as pool:
            for stem, msg in pool.imap_unordered(convert_job, jobs):
                report(stem, msg)
    else:
        for j in jobs:
            report(*convert_job(j))


# priority of language files inside a merged family: Hangul, Latin, symbols, Hanja, Japanese...
def _lang_rank(f):
    cs = set()
    for s in f.outline_sections:
        cs.add(s.lo)
    lo = min(cs) if cs else 0xFFFF
    if lo >= 0x8000:
        return 0
    if lo < 0x0400:
        return 1
    if lo >= 0x4000:
        return 3
    return 2


def merge_jobs(files, a, fmts, code_dir):
    groups = {}
    for p in files:
        try:
            f = HFTFont(p)
        except HFTError:
            continue
        if not f.outline_sections:
            continue
        key = f.face_name.replace(" Oblique", "").strip()
        groups.setdefault(key, []).append((_lang_rank(f), os.path.basename(p), p))
    jobs = []
    used = set()
    for face, items in sorted(groups.items()):
        items.sort()
        covered, keep = set(), []
        for it in items:  # skip files mostly covered already (bold/italic siblings)
            codes = set(HFTFont(it[2]).all_codes())
            if codes and len(codes - covered) >= 0.5 * len(codes):
                keep.append(it)
                covered |= codes
        items = keep
        if len(items) < 2:
            continue
        stem = "MERGED_" + "".join(ch for ch in face if ch not in '/\\:*?"<>|').strip()
        if stem in used:
            stem += "_" + os.path.splitext(items[0][1])[0]
        used.add(stem)
        jobs.append(([p for _, _, p in items], stem, os.path.join(a.out, "merged"),
                     fmts, code_dir, a.keep_overlaps))
    return jobs


if __name__ == "__main__":
    main()
