"""
HWP character code <-> Unicode mapping, built from the code tables shipped with
Hancom Office (Resources/Hnc/Bin/Code/u300_*.dat).

HWP 2.x/3.x internal codes used by HFT fonts:
  0x0000-0x3FFF  symbols / Latin / foreign scripts  (u300_gen/com/pua + u300_h2u)
  0x4000-0x7FFF  Hanja (0x4000 + KS X 1001 hanja index, then extended hanja) (u300_cjk)
  0x8000-0xFFFF  Hangul in Johab (KS X 1001:1992 annex 3)
"""
import os
import struct

NONE = 0x3441  # "no mapping" marker used in the tables


def _load(path):
    with open(path, "rb") as f:
        d = f.read()
    i = d.find(b"Data count")
    j = d.find(b"\n", i) + 1
    n = (len(d) - j) // 2
    return list(struct.unpack_from("<%dH" % n, d, j))


class HwpUnicodeMap:
    def __init__(self, code_dir):
        self.uni2hwp = {}      # primary  unicode -> hwp
        self.extra = []        # (unicode, hwp, priority)

        def p(name):
            return os.path.join(code_dir, name)

        def put(u, h):
            if h != NONE and u not in self.uni2hwp:
                self.uni2hwp[u] = h

        if os.path.exists(p("u300_cjk.dat")):  # hanja first (it owns U+F900..U+FAFF)
            for i, h in enumerate(_load(p("u300_cjk.dat"))):
                put(0x3400 + i if i < 0x6C00 else 0xF800 + (i - 0x6C00), h)
        if os.path.exists(p("u300_gen.dat")):
            for u, h in enumerate(_load(p("u300_gen.dat"))):
                put(u, h)
        if os.path.exists(p("u300_com.dat")):
            for i, h in enumerate(_load(p("u300_com.dat"))):
                put(0xF300 + i, h)
        if os.path.exists(p("u300_pua.dat")):
            t = _load(p("u300_pua.dat"))  # 4 x u16 per PUA code: hwp code + 3 jamo
            for i in range(len(t) // 4):
                put(0xE000 + i, t[4 * i])
        if os.path.exists(p("u300_u2h.dat")):  # extra unicode -> hwp pairs
            t = _load(p("u300_u2h.dat"))
            for k in range(0, len(t) - 1, 2):
                self.extra.append((t[k], t[k + 1], 2))
        if os.path.exists(p("u300_h2u.dat")):  # extra hwp -> unicode pairs
            t = _load(p("u300_h2u.dat"))
            for k in range(0, len(t) - 1, 2):
                self.extra.append((t[k + 1], t[k], 3))
        # Hangul syllables and compatibility jamo -> Johab
        self.johab = {}
        for u in list(range(0xAC00, 0xD7A4)) + list(range(0x3131, 0x318F)):
            try:
                j = chr(u).encode("johab")
            except UnicodeEncodeError:
                continue
            if len(j) == 2:
                self.johab[u] = (j[0] << 8) | j[1]

    def build_cmap(self, available):
        """available: set of HWP codes that have glyphs.
        Returns dict unicode -> hwp code (only for available codes)."""
        entries = []
        for u, h in self.uni2hwp.items():
            entries.append((0, u, h))
        for u, h in self.johab.items():
            entries.append((1, u, h))
        for u, h, pr in self.extra:
            entries.append((pr, u, h))
        entries.sort()
        cmap = {}
        for pr, u, h in entries:
            if u in cmap or h not in available:
                continue
            if 0xD800 <= u <= 0xDFFF or u == 0xFFFF or u == 0xFFFE:
                continue
            cmap[u] = h
        return cmap
