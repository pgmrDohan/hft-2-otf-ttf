"""
hftlib - reader for Hancom "Han Unified Font File 1.0" (.HFT) fonts.

Format notes (reverse engineered from libHncBaseDraw.dylib of Hancom Office HWP for Mac):

Header (0x200 bytes)
  0x000  32s  signature  "Han Unified Font File 1.0\\x1a\\x04\\x03\\x02\\x01\\xff\\x00"
  0x020  u32  version (1)
  0x024  u32  file size
  0x02a  u16  checksum (sum of header bytes with this field zeroed)
  0x02c  64s  face name (Johab)
  0x06c  32s  family name (Johab)
  0x08c  64s  full / description (Johab)
  0x0cc  64s  copyright
  0x10c  32s  vendor (Johab)
  0x12c   8s  date YYYYMMDD
  0x134  32s  driver signature ("" / "Hangul Mapsi font for HWP 2.5" /
              "Hanyang outline font for HWP 2.1" / "Human Font for HWP 2.1")
  0x151  u8,u8  HCI decrypt key (hi, lo)
  0x17a  u16  em size used for advance widths
  0x1a4  u16  number of width blocks
  0x1a6  u16  number of outline glyph sets
  0x1a8  u16  number of bitmap glyph sets
  0x1aa  u32  offset of first width block
  0x1ae  u32  offset of first glyph-set descriptor

Glyph set descriptor (14 bytes) : u32 size, u16 key(em or pixel size), u16 isOutline,
                                   u16 nSections, u32 relOffsetOfFirstSection
Section header (22 bytes)       : u32 size, u16 type, u16 lo, u16 hi, u16 count, u16 em,
                                   s16 x, s16 y, s16 w, s16 h  (default bbox)
  type & 0x0f : 0 = contiguous range (index = code-lo)
                1 = sorted code list (table follows) / 0xffff id = built-in KS X 1001 Hangul list
                2,4 = Hangul syllable composition from jamo components (table follows)
                3 = HWP old-Hangul jamo codes
  type & 0x10 : glyph records have no per-glyph bbox
  For type 1/2/4 a table follows: u32 (low16 = byte size incl. these 4 bytes, high16 = id)
  Then u32 offset table [count], relative to the start of that offset table.
Glyph record: [s16 x,y,w,h unless type&0x10] u16 len, len bytes charstring (optionally encrypted)
Charstring: Type1-like opcodes, see decode_charstring().
"""
import struct

SIGNATURE = b"Han Unified Font File 1.0\x1a\x04\x03\x02\x01\xff\x00"

# --- tables extracted from libHncBaseDraw / libHncFoundation ---------------------------------
CHO_IDX = (20, 0, 1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12, 13, 14, 15, 16, 17, 18, 19, 21, 22, 23,
           24, 25, 26, 27, 28, 29, 30, 31)
JUNG_IDX = (-1, -1, 0, 1, 2, 3, 4, 5, -1, -1, 6, 7, 8, 9, 10, 11, -1, 22, 12, 13, 14, 15, 16, 17,
            23, 24, 18, 19, 20, 21, 25, 26)
JONG_IDX = (28, 0, 1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12, 13, 14, 15, 16, 29, 17, 18, 19, 20, 21,
            22, 23, 24, 25, 26, 27, 30, 31)
JASO_LIST2 = (0x8461, 0x8481, 0x84a1, 0x84c1, 0x84e1, 0x8541, 0x8561, 0x8581, 0x85a1, 0x85c1,
              0x85e1, 0x8641, 0x8661, 0x8681, 0x86a1, 0x86c1, 0x86e1, 0x8741, 0x8761, 0x8781,
              0x87a1, 0x8841, 0x8c41, 0x9041, 0x9441, 0x9841, 0x9c41, 0xa041, 0xa441, 0xa841,
              0xac41, 0xb041, 0xb441, 0xb841, 0xbc41, 0xc041, 0xc441, 0xc841, 0xcc41, 0xd041,
              0xd3c1, 0xd3c5)


def _ks_table():
    out = []
    for b1 in range(0xB0, 0xC9):
        for b2 in range(0xA1, 0xFF):
            j = bytes([b1, b2]).decode("cp949").encode("johab")
            out.append((j[0] << 8) | j[1])
    return out


KS_TBL = _ks_table()  # 2350 KS X 1001 Hangul syllables as Johab codes (sorted)
KS_INDEX = {c: i for i, c in enumerate(KS_TBL)}


# --- decryption ----------------------------------------------------------------------------
def _lcg_decrypt(r, add):
    def f(buf):
        out = bytearray(len(buf))
        rr = r
        for i, c in enumerate(buf):
            out[i] = c ^ (rr >> 8)
            rr = ((c + rr) * 0xC73E + add) & 0xFFFF
        return bytes(out)
    return f


decrypt_mapsi = _lcg_decrypt(0xA729, 0xE696)     # "Hangul Mapsi font for HWP 2.5"
decrypt_hanyang = _lcg_decrypt(0xE696, 0xC863)   # "Hanyang outline font for HWP 2.1"


def make_hci_decrypt(key):
    if key == 0:
        return None

    def f(buf):
        out = bytearray(len(buf))
        k = key
        for i, c in enumerate(buf):
            out[i] = c ^ (k & 0xFF)
            k = (k + c + 1) & 0xFFFF
        return bytes(out)
    return f


# --- charstring ----------------------------------------------------------------------------
def _num(b, i):
    v = b[i]
    sv = v - 256 if v > 127 else v
    if -123 <= sv <= 123:
        return sv, i + 1
    if sv >= 124:
        return (sv - 124) * 256 + b[i + 1] + 124, i + 2
    if v == 0x80:
        x = b[i + 1] | (b[i + 2] << 8)
        return (x - 65536 if x >= 32768 else x), i + 3
    return -((-sv - 124) * 256) - b[i + 1] - 124, i + 2


def decode_charstring(b, ox=0, oy=0):
    """Decode a glyph program. Returns list of contours, each a list of
    ('M',(x,y)) / ('L',(x,y)) / ('C',(p1,p2,p3)). Coordinates are absolute."""
    i = 0
    n = len(b)
    cx, cy = ox, oy
    contours = []
    cur = None
    while i < n:
        op = b[i]
        i += 1
        if op == 0 or op == 4:
            if cur:
                contours.append(cur)
                if len(cur) > 1:  # closepath: current point returns to the subpath start
                    cx, cy = cur[0][1]
            cur = None
            if op == 0:
                break
        elif 1 <= op <= 3:
            if cur:
                contours.append(cur)
            dx = dy = 0
            if op & 1:
                dx, i = _num(b, i)
            if op & 2:
                dy, i = _num(b, i)
            cx += dx
            cy += dy
            cur = [("M", (cx, cy))]
        elif 5 <= op <= 7:
            dx = dy = 0
            if op & 1:
                dx, i = _num(b, i)
            if op & 2:
                dy, i = _num(b, i)
            if cur is None:
                cur = [("M", (cx, cy))]
            cx += dx
            cy += dy
            cur.append(("L", (cx, cy)))
        elif 9 <= op <= 11:
            dx1 = dy1 = dx3 = dy3 = 0
            if op & 1:
                dx1, i = _num(b, i)
            if op & 2:
                dy1, i = _num(b, i)
            dx2, i = _num(b, i)
            dy2, i = _num(b, i)
            if op & 2:
                dx3, i = _num(b, i)
            if op & 1:
                dy3, i = _num(b, i)
            if cur is None:
                cur = [("M", (cx, cy))]
            p1 = (cx + dx1, cy + dy1)
            p2 = (p1[0] + dx2, p1[1] + dy2)
            p3 = (p2[0] + dx3, p2[1] + dy3)
            cur.append(("C", (p1, p2, p3)))
            cx, cy = p3
        elif op == 0x20:  # hint data
            n1 = b[i]
            i += 1 + 2 * n1
            i += 1
            n2 = b[i]
            i += 1 + 2 * n2
        elif op == 0x21:
            cnt = b[i]
            i += 1
            for _ in range(cnt):
                L = b[i]
                i += 1 + L
        elif op in (0x22, 0x23):
            i += 1
        elif op in (0x40, 0x42):
            for _ in range(2):
                _, i = _num(b, i)
        elif op in (0x41, 0x43):
            for _ in range(6):
                _, i = _num(b, i)
        elif op == 0x44:
            pass
        else:
            raise ValueError("bad opcode 0x%02x at %d" % (op, i - 1))
    if cur:
        contours.append(cur)
    return contours


def _cstr(b):
    b = b.split(b"\0")[0]
    for enc in ("johab", "cp949", "latin-1"):
        try:
            return b.decode(enc).strip()
        except UnicodeDecodeError:
            pass
    return ""


class Section:
    pass


class HFTError(Exception):
    pass


class HFTFont:
    def __init__(self, path):
        self.path = path
        with open(path, "rb") as f:
            self.d = d = f.read()
        if len(d) < 0x200 or not d.startswith(SIGNATURE):
            raise HFTError("not an HFT file")
        h = d[:0x200]
        self.face_name = _cstr(h[0x2C:0x6C])
        self.family_name = _cstr(h[0x6C:0x8C])
        self.description = _cstr(h[0x8C:0xCC])
        self.copyright = _cstr(h[0xCC:0x10C])
        self.vendor = _cstr(h[0x10C:0x12C])
        self.date = _cstr(h[0x12C:0x134])
        self.driver = h[0x134:0x154].split(b"\0")[0].decode("latin-1")
        self.charset = h[0x157]
        self.width_em = struct.unpack_from("<H", h, 0x17A)[0] or 1000
        if self.driver.startswith("Hangul Mapsi font"):
            self.decrypt = decrypt_mapsi
        elif self.driver.startswith("Hanyang outline font"):
            self.decrypt = decrypt_hanyang
        elif self.driver.startswith("Human"):
            self.decrypt = make_hci_decrypt((h[0x151] << 8) | h[0x152])
        else:
            self.decrypt = None
        nw, no, nb = struct.unpack_from("<3H", h, 0x1A4)
        woff, soff = struct.unpack_from("<2I", h, 0x1AA)
        # width blocks
        self.width_blocks = []
        off = woff
        for _ in range(nw):
            size = struct.unpack_from("<I", d, off)[0]
            self.width_blocks.append(d[off + 4:off + size])
            off += size
        # glyph sets
        self.outline_sections = []
        self.bitmap_sets = []
        off = soff
        for _ in range(no + nb):
            size, key, is_ol, nsec, rel = struct.unpack_from("<IHHHI", d, off)
            secs = self._read_sections(off + rel, nsec, key)
            if is_ol:
                self.outline_sections.extend(secs)
            else:
                self.bitmap_sets.append((key, secs))
            off += size

    # ------------------------------------------------------------------
    def _read_sections(self, off, nsec, key):
        d = self.d
        out = []
        for _ in range(nsec):
            s = Section()
            (size, s.type, s.lo, s.hi, s.count, s.em, s.bx, s.by, s.bw, s.bh) = \
                struct.unpack_from("<IHHHHH4h", d, off)
            s.key = key
            s.kind = s.type & 0x0F
            s.nobbox = bool(s.type & 0x10)
            p = off + 22
            s.codes = None
            s.comp = None
            if s.kind in (1, 2, 4):
                tw = struct.unpack_from("<I", d, p)[0]
                tsize, tid = tw & 0xFFFF, tw >> 16
                tdata = d[p + 4:p + tsize]
                if tid == 0xFFFF:
                    s.codes = KS_TBL
                elif s.kind == 1:
                    s.codes = list(struct.unpack_from("<%dH" % ((tsize - 4) // 2), tdata))
                else:
                    s.comp = self._parse_comp(tdata, s.kind, self.driver.startswith("Human"))
                p += tsize
            s.base = p
            out.append(s)
            off += size
        return out

    @staticmethod
    def _parse_comp(t, kind, hci=False):
        c = {}
        ncho, njung, njong = t[0], t[1], t[2]
        if hci:  # OpenSection decrements these for HCI ("Human") fonts
            ncho, njung, njong = (ncho - 1) & 0xFF, (njung - 1) & 0xFF, (njong - 1) & 0xFF
        c["ncho"], c["njung"], c["njong"], c["flags"] = ncho, njung, njong, t[3]
        # three u16[32] arrays: glyph-variant counts per jamo, turned into running
        # (exclusive) sums over the first n+1 entries of each array, carried across arrays
        cho = list(struct.unpack_from("<32H", t, 4))
        jung = list(struct.unpack_from("<32H", t, 0x44))
        jong = list(struct.unpack_from("<32H", t, 0x84))
        run = 0
        for arr, n in ((cho, ncho), (jung, njung), (jong, njong)):
            for i in range(min(n + 1, 32)):
                v = arr[i]
                arr[i] = run
                run = (run + v) & 0xFFFF
        c["cho_base"], c["jung_base"], c["jong_base"] = cho, jung, jong
        c["gcho"], c["gjung"], c["gjong"] = t[0xC4], t[0xC5], t[0xC6]
        c["cho_grp"] = t[0xC8:0xE8]
        c["jung_grp"] = t[0xE8:0x108]
        c["jong_grp"] = t[0x108:0x128]
        N = c["gcho"] * c["gjung"] * c["gjong"]
        c["N"] = N
        c["var"] = t[0x128:0x128 + 3 * N]
        p = 0x128 + 3 * N
        c["dx"] = c["dy"] = None
        if kind == 4:
            if c["flags"] & 1:
                c["dx"] = struct.unpack_from("<%dh" % (3 * N), t, p)
                p += 6 * N
            if c["flags"] & 2:
                c["dy"] = struct.unpack_from("<%dh" % (3 * N), t, p)
        return c

    # ------------------------------------------------------------------
    def section_codes(self, s):
        """All character codes that a section can produce."""
        if s.kind == 0:
            return range(s.lo, min(s.hi, s.lo + s.count - 1) + 1)
        if s.kind == 1:
            return [c for c in s.codes if s.lo <= c <= s.hi]
        if s.kind in (2, 4):
            if s.codes is not None:  # composition section that uses the KS list
                return [c for c in s.codes if s.lo <= c <= s.hi]
            cands = sorted(set(all_johab_codes()) | set(EXTRA_HANGUL_CODES))
            return [c for c in cands if s.lo <= c <= s.hi]
        if s.kind == 3:
            return [c for c in EXTRA_HANGUL_CODES if s.lo <= c <= s.hi]
        return []

    def lookup(self, code, sections=None):
        """-> list of (section, glyph_index, dx, dy) or None (mirrors GetGlyphIndexExt)."""
        special = (code & 0x83C0) == 0x8000 and ((code + 0x17D7) & 0xFFFF) > 0x16
        if sections is None:
            sections = self.outline_sections
        for s in sections:
            if not (s.lo <= code <= s.hi):
                continue
            k = s.kind
            if k == 0:
                return [(s, code - s.lo, 0, 0)]
            if k == 1:
                idx = _bsearch(s.codes, code)
                if idx is not None:
                    return [(s, idx, 0, 0)]
                continue
            if k in (2, 4):
                if s.comp is None:
                    idx = _bsearch(s.codes, code) if s.codes else None
                    if idx is not None:
                        return [(s, idx, 0, 0)]
                    continue
                r = self._compose(s, code)
                if r:
                    return r
                continue
            if k == 3:
                if special and ((code & 0xE0) != 0x20 or
                                ((code & 0xFF00) != 0xEC00 and (code & 0xFB00 | 0x800) != 0xF800)):
                    idx = (((code >> 5) & 0x3E0) + (code & 0x1F)) | ((code & 0x20) << 5)
                    return [(s, idx, 0, 0)]
                continue
            return None
        return None

    def _compose(self, s, code):
        c = s.comp
        ci = CHO_IDX[(code >> 10) & 0x1F]
        ji = JUNG_IDX[(code >> 5) & 0x1F]
        ki = JONG_IDX[code & 0x1F]
        if ci > c["ncho"] or ji < 0 or ji > c["njung"] or ki > c["njong"]:
            return None
        g1 = c["cho_grp"][ci]
        g2 = c["jung_grp"][ji]
        g3 = c["jong_grp"][ki]
        if g1 >= c["gcho"] or g2 >= c["gjung"] or g3 >= c["gjong"]:
            return None
        N = c["N"]
        li = g3 + (g1 * c["gjung"] + g2) * c["gjong"]
        out = []
        for part, ji_, base in ((0, ci, c["cho_base"]), (1, ji, c["jung_base"]),
                               (2, ki, c["jong_base"])):
            if ji_ == 0:
                continue
            k = li + part * N
            dx = c["dx"][k] if c["dx"] else 0
            dy = c["dy"][k] if c["dy"] else 0
            out.append((s, c["var"][k] + base[ji_], dx, dy))
        return out or None

    # ------------------------------------------------------------------
    def glyph_record(self, s, idx):
        """-> (bbox or None, decrypted charstring) or None if glyph missing."""
        d = self.d
        if idx < 0 or idx >= s.count:
            return None
        o = struct.unpack_from("<I", d, s.base + idx * 4)[0]
        if o == 0:
            return None
        p = s.base + o
        bbox = None
        if not s.nobbox:
            bbox = struct.unpack_from("<4h", d, p)
            p += 8
        L = struct.unpack_from("<H", d, p)[0]
        if L > 0x2000:
            return None
        cs = d[p + 2:p + 2 + L]
        if self.decrypt:
            cs = self.decrypt(cs)
        return bbox, cs

    def glyph_contours(self, code):
        """Outline for a character code in section em units (y up, 0 = bottom of em box),
        plus the em of the (first) section. Returns (contours, em) or None."""
        comps = self.lookup(code)
        if not comps:
            return None
        allc = []
        em = comps[0][0].em
        for s, idx, dx, dy in comps:
            rec = self.glyph_record(s, idx)
            if rec is None:
                if len(comps) == 1:
                    return None
                continue
            bbox, cs = rec
            allc.extend(decode_charstring(cs, dx, dy))
        return allc, em

    # ------------------------------------------------------------------
    def advance(self, code):
        """Advance width in width_em units (None if no width info)."""
        for blk in self.width_blocks:
            first, last, typ = struct.unpack_from("<HHH", blk, 0)
            if not (first <= code <= last):
                continue
            if typ == 0:
                return struct.unpack_from("<h", blk, 6)[0]
            if typ == 1:
                if code & 0x8000:
                    if first == last:
                        return struct.unpack_from("<h", blk, 6)[0]
                    if code in KS_INDEX and code != 0xD3C5:
                        i = KS_INDEX[code]
                    else:
                        try:
                            j = JASO_LIST2.index(code)
                        except ValueError:
                            return None
                        i = 2350 + j
                else:
                    i = code - first
                if 6 + 2 * i + 2 <= len(blk):
                    return struct.unpack_from("<h", blk, 6 + 2 * i)[0]
                return None
            if typ == 2:
                ji = JUNG_IDX[(code >> 5) & 0x1F]
                if ji < 0:
                    continue
                ci = CHO_IDX[(code >> 10) & 0x1F]
                ki = JONG_IDX[code & 0x1F]
                njung, njong = blk[7], blk[8]
                gc, gj, gk = blk[0xA + ci], blk[0x2A + ji], blk[0x4A + ki]
                i = njong * (njung * gc + gj) + gk
                if 0x6A + 2 * i + 2 <= len(blk):
                    return struct.unpack_from("<h", blk, 0x6A + 2 * i)[0]
                return None
            return None
        return None

    # ------------------------------------------------------------------
    def bitmap_glyph(self, code, sections):
        """Bitmap for a code from one bitmap set.
        -> (x, ytop, w, h, rows) with rows = list of ints (bit w-1-k = column k) or None."""
        comps = self.lookup(code, sections)
        if not comps:
            return None
        d = self.d
        parts = []
        for s, idx, dx, dy in comps:
            if idx < 0 or idx >= s.count:
                return None
            if s.nobbox:
                x, y, w, h = s.bx, s.by, s.bw, s.bh
                rb = (w + 7) >> 3
                p = s.base + idx * rb * h
            else:
                o = struct.unpack_from("<I", d, s.base + idx * 4)[0]
                if o == 0:
                    if len(comps) == 1:
                        return None
                    continue
                p = s.base + o
                x, y, w, h = struct.unpack_from("<4h", d, p)
                p += 8
                rb = (w + 7) >> 3
            if w <= 0 or h <= 0:
                parts.append((x + dx, y + dy, max(w, 0), max(h, 0), []))
                continue
            raw = d[p:p + rb * h]
            if len(raw) < rb * h:
                return None
            rows = []
            for r in range(h):
                v = int.from_bytes(raw[r * rb:(r + 1) * rb], "big")
                rows.append(v >> (rb * 8 - w))
            parts.append((x + dx, y + dy, w, h, rows))
        if not parts:
            return None
        if len(parts) == 1:
            return parts[0]
        # merge components (OR) into one bitmap
        x0 = min(p[0] for p in parts)
        y0 = max(p[1] for p in parts)
        x1 = max(p[0] + p[2] for p in parts)
        y1 = min(p[1] - p[3] for p in parts)
        W, H = x1 - x0, y0 - y1
        rows = [0] * H
        for x, y, w, h, rr in parts:
            for r, v in enumerate(rr):
                rows[(y0 - y) + r] |= v << (x1 - (x + w))
        return (x0, y0, W, H, rows)

    def all_codes(self, sections=None):
        seen = set()
        out = []
        for s in (self.outline_sections if sections is None else sections):
            for c in self.section_codes(s):
                if c not in seen:
                    seen.add(c)
                    out.append(c)
        return out


def _bsearch(lst, v):
    lo, hi = 0, len(lst) - 1
    while lo <= hi:
        m = (lo + hi) >> 1
        if lst[m] == v:
            return m
        if lst[m] < v:
            lo = m + 1
        else:
            hi = m - 1
    return None


_ALL_JOHAB = None
# additional Hangul-area HWP codes (old Hangul etc.) worth probing; filled by the converter
EXTRA_HANGUL_CODES = []


def all_johab_codes():
    """Johab codes for all 11172 modern syllables + compatibility jamo (fill combos)."""
    global _ALL_JOHAB
    if _ALL_JOHAB is None:
        s = set()
        for u in range(0xAC00, 0xD7A4):
            j = chr(u).encode("johab")
            s.add((j[0] << 8) | j[1])
        for u in range(0x3131, 0x3164):
            try:
                j = chr(u).encode("johab")
                s.add((j[0] << 8) | j[1])
            except UnicodeEncodeError:
                pass
        _ALL_JOHAB = sorted(s)
    return _ALL_JOHAB
