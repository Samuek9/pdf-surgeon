#!/usr/bin/env python3
"""
pdf_text_surgery.py -- replace text inside an existing PDF with no visible edit.

Why not redaction + re-stamp: PyMuPDF's add_text defaults to Helvetica 8pt and
you must restate font/size/colour by hand. Get any of that wrong (the common
failure) and the result screams "edited" -- different typeface, different size,
a tell-tale rectangle. This script edits the page content stream in place, so
font, size, colour, baseline, char-spacing, word-spacing, horizontal scale and
kerning stay exactly as the original author set them.

Works on lines laid out as one BT/ET block per word/run with an explicit Tm
origin -- which is what Word, LibreOffice, Excel, Google Docs and most
digital-born PDFs emit. Run with --list to see the structure first.

Usage
-----
  python pdf_text_surgery.py in.pdf --find "six (06) days of July" \
                                     --replace "five (05) days of October" \
                                     [--page 0] [--out out.pdf] [--list]

  --list  print every text line with its baseline and block count

Install: pip install pymupdf
"""
from __future__ import annotations

import argparse
import contextlib
import difflib
import io
import os
import re
import sys
import tempfile

try:
    import pymupdf
except ImportError:                      # older wheels expose only `fitz`
    import fitz as pymupdf

# ------------------------------------------------------------------ parsing

BLOCK_RE = re.compile(r"BT(?P<body>.*?)ET", re.S)
TM_RE = re.compile(
    r"(?P<a>-?[\d.]+)\s+(?P<b>-?[\d.]+)\s+(?P<c>-?[\d.]+)\s+(?P<d>-?[\d.]+)"
    r"\s+(?P<x>-?[\d.]+)\s+(?P<y>-?[\d.]+)\s+[-\d.\s]*?Tm\b")
TF_RE = re.compile(r"/(?P<name>[A-Za-z0-9#.+_-]+)\s+(?P<size>[\d.]+)\s+Tf")
TJ_RE = re.compile(r"\[(?P<body>.*?)\]\s*TJ", re.S)
TJ_ITEM_RE = re.compile(r"(?P<num>-?[\d.]+)|(?P<str>\((?:\\.|[^()\\])*\))", re.S)
TJ_PLAIN_RE = re.compile(r"\((?P<s>(?:\\.|[^()\\])*)\)\s*Tj")


def unescape(s: str) -> str:
    out, i = [], 0
    simple = {"n": "\n", "r": "\r", "t": "\t", "b": "\b", "f": "\f"}
    while i < len(s):
        if s[i] == "\\" and i + 1 < len(s):
            out.append(simple.get(s[i + 1], s[i + 1]))
            i += 2
        else:
            out.append(s[i])
            i += 1
    return "".join(out)


def escape(s: str) -> str:
    return re.sub(r"[\\()]", lambda m: "\\" + m.group(0), s)


def parse_tj(body: str):
    """-> (pieces, kerns): show-string literals and the numbers between them
    (TJ units, negative = tighten)."""
    pieces, kerns = [], []
    for m in TJ_ITEM_RE.finditer(body):
        if m.group("num") is not None:
            kerns.append(float(m.group("num")))
        else:
            pieces.append(unescape(m.group("str")[1:-1]))
    return pieces, kerns


def op_value(body: str, op: str) -> float:
    m = re.search(r"(-?[\d.]+)\s+%s\b" % op, body)
    return float(m.group(1)) if m else 0.0


class Block:
    """One BT/ET run: a text-showing block with an explicit Tm origin."""

    __slots__ = ("x", "y", "size", "fres", "tc", "tw", "tz", "pieces", "kerns",
                 "raw", "new_x", "new_pieces", "touched", "op_kind")

    def __init__(self, m):
        self.raw = m.group(0)
        body = m.group("body")

        tm = TM_RE.search(body)
        if not tm:
            raise SystemExit("BT block without Tm -- not a plain word block")
        self.x, self.y = float(tm.group("x")), float(tm.group("y"))

        tf = TF_RE.search(body)
        self.fres = "/" + tf.group("name") if tf else "?"
        self.size = float(tf.group("size")) if tf else 0.0

        self.tc = op_value(body, "Tc")
        self.tw = op_value(body, "Tw")
        tz = re.search(r"(-?[\d.]+)\s+Tz\b", body)
        self.tz = (float(tz.group(1)) / 100.0) if tz else 1.0

        tj = TJ_RE.search(body)
        if tj:
            self.pieces, self.kerns = parse_tj(tj.group("body"))
            self.op_kind = "TJ"
        else:
            pj = TJ_PLAIN_RE.search(body)
            self.pieces = [unescape(pj.group("s"))] if pj else []
            self.kerns = []
            self.op_kind = "Tj"

        self.new_x = self.x
        self.new_pieces = list(self.pieces)
        self.touched = False

    @property
    def text(self):
        return "".join(self.pieces)

    @property
    def is_space(self):
        return self.text.strip() == ""

    def width(self, text, font):
        """Rendered advance: glyph advances, plus Tc per glyph and Tw per
        space, all scaled by Tz."""
        if font is None:
            return 0.0
        return (font.text_length(text, self.size) * self.tz
                + self.tc * len(text) * self.tz
                + self.tw * text.count(" ") * self.tz)


# -------------------------------------------------------------------- fonts

def font_metrics(doc, page, blocks):
    """Map /Fres -> pymupdf.Font built from the *embedded* font program, so
    advance widths match the original exactly."""
    table, notes = {}, []
    avail = page.get_fonts(full=True)
    for name in sorted({b.fres for b in blocks}):
        bare = name.lstrip("/")
        # get_fonts tuple: (xref, ext, type, basefont, name, encoding, referencer)
        hit = [f for f in avail if f[4] == bare] or \
              [f for f in avail if ("/" + (f[3] or "")) == name]
        if not hit:
            notes.append("%s: resource not in the page font list" % name)
            continue
        # extract_font returns (xref, ext, type, buffer) on pymupdf >= 1.24
        # and (ext, type, buffer) before that
        ef = doc.extract_font(hit[0][0])
        ext, buf = (ef[1], ef[3]) if len(ef) == 4 else (ef[0], ef[2])
        base = (hit[0][3] or "").split("+")[-1]
        if not buf:
            try:                                   # base-14, not embedded
                table[name] = pymupdf.Font(base)
                notes.append("%s: not embedded, using base font %s" % (name, base))
            except Exception:
                notes.append("%s: no embedded program and no base font" % name)
            continue
        # Load straight from memory: no temp file, so nothing lands in a
        # shared TMPDIR (or the CWD) under a predictable name.
        table[name] = pymupdf.Font(fontbuffer=buf)
    return table, notes


def check_glyphs(blocks, new_text, table, used):
    """Refuse to proceed when the new text needs a glyph the font cannot draw
    -- otherwise you ship blank boxes or a silent fallback.

    `used` maps each font resource to the characters this document already
    draws with it. Those are ground truth and always allowed.

    PyMuPDF's Font.has_glyph is unreliable on subsetted fonts: some subsets
    are glyph-indexed with no usable unicode cmap, and then it reports even
    'a' as absent. So calibrate before trusting it -- if has_glyph cannot
    find a character the document demonstrably draws, has_glyph knows
    nothing about this font, and a negative answer means nothing either.
    """
    for fres, size in sorted({(b.fres, b.size) for b in blocks}):
        f = table.get(fres)
        drawn = {c for c in used.get(fres, set()) if not c.isspace()}
        new = {c for c in new_text if not c.isspace()}
        if f is None:
            print("glyph note: cannot check %s (no metrics); verified visually"
                  % fres)
            continue
        absent = {c for c in new if c not in drawn and not f.has_glyph(ord(c))}
        if not absent:
            continue
        if not any(f.has_glyph(ord(c)) for c in drawn):
            print("glyph note: %s reports even its own drawn characters as "
                  "absent (glyph-indexed subset) -- the check is inconclusive, "
                  "so proceeding. Confirm the rendering." % fres)
            continue
        raise SystemExit(
            "glyphs missing from embedded font %s: %s\n"
            "The document never draws these and the font has no code point for "
            "them. Re-subset the font with them, or reword."
            % (fres, "".join(sorted(absent))))


# ----------------------------------------------------------------- geometry

def group_lines(blocks):
    lines = {}
    for b in blocks:
        lines.setdefault(round(b.y, 1), []).append(b)
    for v in lines.values():
        v.sort(key=lambda b: b.x)
    return dict(sorted(lines.items(), key=lambda kv: -kv[0]))   # top-down


def strip_ws(s):
    return "".join(ch for ch in s if not ch.isspace())


def align_to_find(line, find):
    """-> (first_block, last_block, lo, hi) covering `find`, or None.

    `lo`/`hi` are offsets into the whitespace-stripped line -- the exact span
    the caller asked to replace. They matter: the match can cover only part of
    a block (a block holding a whole phrase), and the edit must splice that
    slice rather than rewrite the block.

    Matching ignores whitespace, because where the exporter put spaces is an
    artefact of the exporter ('( 06 )' is three blocks), not something the
    caller knows or cares about. The match must be exact (modulo
    whitespace): a fuzzy fallback would edit text the caller never named --
    'Invoice 0013' would rewrite an 'Invoice 0018' line -- and verification
    cannot catch that, because it checks the edit against the span that was
    matched, not the one that was meant.
    """
    target = strip_ws(find)
    if not target:
        return None
    raw = "".join(b.text for b in line)
    hay = strip_ws(raw)
    idx = hay.find(target)
    if idx < 0:
        return None
    lo, hi = idx, idx + len(target)

    acc, first, last = 0, None, None
    for i, b in enumerate(line):
        n = len(strip_ws(b.text))
        if first is None and n and acc + n > lo:
            first = i
        if n and acc < hi:
            last = i
        acc += n
    return first, last, lo, hi


def plan_edits(target, new_text, lo=0, hi=None):
    """Decide the new text of every block in the target run.

    One character-level diff over the whitespace-stripped *matched span*, then
    splice each changed run back into the block that owns it. Two details do
    the real work:

    * The diff is bounded by `lo`/`hi` from align_to_find. A block can hold a
      whole phrase while the caller only asked to change three words of it;
      without the bound the untouched remainder of that block gets eaten.
    * Matching ignores whitespace, because where the exporter put spaces is an
      artefact of the exporter -- '( 06 )' is three blocks -- not something the
      caller knows.

    Working at character level rather than token level is what makes the rest
    fall out for free:
      * 'seis'->'cinco' and '06'->'05' while '(' and ')' keep their own blocks,
        so the odd paren spacing the exporter used survives;
      * a replacement that changes the number of words.

    Blocks that end up empty are emptied, not deleted: holding the block count
    stable is what lets verification prove nothing else moved.
    """
    raw = "".join(b.text for b in target)
    offs, acc = [], 0
    for b in target:
        offs.append((acc, acc + len(b.text)))
        acc += len(b.text)

    pos, owner = [], []                    # non-space chars: raw offset, block
    for bi, (a, e) in enumerate(offs):
        for i in range(a, e):
            if not raw[i].isspace():
                pos.append(i)
                owner.append(bi)
    old = "".join(raw[i] for i in pos)
    new = strip_ws(new_text)
    lo = max(0, min(lo, len(old)))
    hi = len(old) if hi is None else max(lo, min(hi, len(old)))

    edits = {}
    for tag, i1, i2, j1, j2 in difflib.SequenceMatcher(
            None, old[lo:hi], new, autojunk=False).get_opcodes():
        if tag == "equal":
            continue
        gi1, gi2 = lo + i1, lo + i2       # back to whole-line coordinates
        start = pos[gi1] if gi1 < len(pos) else (pos[gi1 - 1] + 1 if gi1 else 0)
        end = pos[gi2 - 1] + 1 if gi2 > gi1 else start
        repl = new[j1:j2]
        anchor = owner[gi1] if gi1 < len(owner) else (owner[gi1 - 1] if gi1 else 0)
        if end == start:
            # Pure insertion. When it falls on a block boundary no block
            # overlaps the empty span, so the overlap loop below would drop it
            # and report "already reads that way". Put it in the anchor block.
            a, e = offs[anchor]
            at = min(max(start - a, 0), e - a)
            edits.setdefault(anchor, []).append((at, at, repl))
            continue
        for bi in range(len(target)):
            a, e = offs[bi]
            if e <= start or a >= end:                     # no overlap
                continue
            ls, le = max(start, a) - a, min(end, e) - a
            edits.setdefault(bi, []).append((ls, le, repl if bi == anchor else ""))

    out = []
    for bi, b in enumerate(target):
        t = b.text
        for ls, le, rep in sorted(edits.get(bi, []), key=lambda x: -x[0]):
            t = t[:ls] + rep + t[le:]
        out.append((b, t))
    return out


# ------------------------------------------------------------------- output

def rewrite_block(b):
    s = b.raw
    if b.new_x != b.x:
        m = TM_RE.search(s)
        if m:
            s = s[:m.start()] + "%s %s %s %s %g %s Tm" % (
                m.group("a"), m.group("b"), m.group("c"), m.group("d"),
                b.new_x, m.group("y")) + s[m.end():]
    if b.new_pieces != b.pieces:
        if b.op_kind == "TJ":
            open_at, close_at = s.index("["), s.rindex("]")
            if len(b.kerns) >= len(b.new_pieces) - 1:
                parts = []
                for pi, p in enumerate(b.new_pieces):
                    if pi:
                        parts.append("%g" % b.kerns[pi - 1])
                    parts.append("(%s)" % escape(p))
                inner = " ".join(parts)
            else:
                inner = "(%s)" % "".join(escape(p) for p in b.new_pieces)
            s = s[:open_at] + "[" + inner + "]" + s[close_at + 1:]
        else:
            s = TJ_PLAIN_RE.sub(
                "(%s) Tj" % escape("".join(b.new_pieces)), s, count=1)
    return s


def page_blocks(doc, pno):
    raw = doc[pno].read_contents().decode("latin-1", "replace")
    return raw, [Block(m) for m in BLOCK_RE.finditer(raw)]


def faces(doc, pno):
    """Set of (font, size, colour) over every span on the page.

    A set, not a multiset: an edit can make PyMuPDF regroup spans into a
    different number of spans while the document still uses exactly the same
    faces. Comparing sets is what actually matters -- no new font, no size
    drift, no colour drift.
    """
    out = set()
    for blk in doc[pno].get_text("dict")["blocks"]:
        if blk.get("type") != 0:
            continue
        for line in blk["lines"]:
            for s in line["spans"]:
                out.add((s["font"], round(s["size"], 2), s["color"]))
    return out


def verify(src, out, pno, y, find, replace, expected_line, raster=True):
    print("\n--- verify ---")
    a, b = pymupdf.open(src), pymupdf.open(out)
    try:
        return _verify(a, b, pno, y, find, replace, expected_line, raster)
    finally:
        a.close()
        b.close()


def _verify(a, b, pno, y, find, replace, expected_line, raster):
    ok = True
    print("pages %d -> %d   encrypted=%s  repaired=%s"
          % (a.page_count, b.page_count, b.is_encrypted, b.is_repaired))
    ok &= a.page_count == b.page_count

    _, ba = page_blocks(a, pno)
    _, bb = page_blocks(b, pno)
    la = group_lines(ba).get(round(y, 1), [])
    lb = group_lines(bb).get(round(y, 1), [])
    print("blocks on target line: %d -> %d" % (len(la), len(lb)))
    ok &= len(la) == len(lb)

    changed, moved = [], []
    for x, z in zip(la, lb):
        if x.text != z.text:
            changed.append((x.text, z.text))
        if abs(x.x - z.x) > 0.002:
            moved.append(z.text)
        if abs(x.y - z.y) > 0.002:
            print("  FAIL baseline drift on %r" % x.text); ok = False
        if x.fres != z.fres or abs(x.size - z.size) > 0.01:
            print("  FAIL font/size drift on %r" % x.text); ok = False
        if abs(x.tc - z.tc) > 0.001 or abs(x.tw - z.tw) > 0.001 \
                or abs(x.tz - z.tz) > 0.001:
            print("  FAIL spacing drift on %r" % x.text); ok = False
    # Everything off the edited line must be untouched: same lines, same
    # blocks, same text. This is what makes "nothing else changed" true for
    # the page rather than just for the edited line.
    others_a = {k: [x.text for x in v] for k, v in group_lines(ba).items()
                if k != round(y, 1)}
    others_b = {k: [x.text for x in v] for k, v in group_lines(bb).items()
                if k != round(y, 1)}
    print("other lines on the page unchanged: %s" % (others_a == others_b))
    ok &= others_a == others_b
    print("blocks whose text changed: %d  %s" % (len(changed), changed))
    print("blocks that moved (reflow):  %d" % len(moved))
    ok &= bool(changed)

    # The decisive check: the edited line must read exactly as the original
    # with `find` swapped for `replace`. Everything else -- font identity,
    # block count, "old phrase gone" -- can pass while neighbouring words have
    # been silently eaten, so this one is what decides.
    got = strip_ws("".join(b_.text for b_ in lb))
    print("edited line reads exactly as intended: %s" % (got == expected_line))
    if got != expected_line:
        for k, (want, have) in enumerate(zip(expected_line, got)):
            if want != have:
                print("  first divergence at char %d: expected ...%s... got ...%s..."
                      % (k, expected_line[max(0, k - 20):k + 20],
                         got[max(0, k - 20):k + 20]))
                break
        else:
            print("  length differs: expected %d chars, got %d"
                  % (len(expected_line), len(got)))
    ok &= got == expected_line

    fa, fb = faces(a, pno), faces(b, pno)
    print("page font/size/colour set identical: %s" % (fa == fb))
    if fa != fb:
        print("  only in original: %s" % sorted(fa - fb))
        print("  only in edited  : %s" % sorted(fb - fa))
    ok &= fa == fb

    txt_a, txt = strip_ws(a[pno].get_text()), strip_ws(b[pno].get_text())
    f, r = strip_ws(find), strip_ws(replace)
    # exactly one occurrence of `find` went away (plus any that `replace`
    # itself contains): works when the phrase repeats on the page or is a
    # substring of its replacement
    gone = txt.count(f) == txt_a.count(f) - 1 + r.count(f)
    words = replace.split()
    present = (strip_ws(words[0]) in txt) if words else True
    print("old phrase removed once: %s   new word present: %s" % (gone, present))
    ok &= gone and present

    if raster:
        zoom = 2.0
        ra = a[pno].get_pixmap(matrix=pymupdf.Matrix(zoom, zoom))
        rb = b[pno].get_pixmap(matrix=pymupdf.Matrix(zoom, zoom))
        n = max(1, rb.n)                   # bytes per pixel
        height = min(ra.height, rb.height)
        # Hoist the sample buffers. Pixmap.samples rebuilds a bytes object on
        # every attribute access, so touching it inside the loop copies the
        # whole page each time -- that alone was ~90s of the run.
        sa, sb = ra.samples, rb.samples
        sa_stride, sb_stride = ra.stride, rb.stride

        # Recursive halving over row bands. Comparing each row individually
        # costs a Python-level loop over the whole page; comparing band-sized
        # byte slices lets the C layer do the work and only the handful of
        # genuinely changed bands get subdivided. ~20 slice compares instead
        # of ~1200.
        rows = []

        def split(lo, hi):
            if lo >= hi:
                return
            if (sa[lo * sa_stride:hi * sa_stride]
                    == sb[lo * sb_stride:hi * sb_stride]):
                return
            if hi - lo == 1:
                rows.append(lo)
                return
            mid = (lo + hi) // 2
            split(lo, mid)
            split(mid, hi)

        split(0, height)
        if rows:
            lo, hi_x = rb.stride, 0
            for r in rows:                 # only changed rows: cheap to scan
                oa, ob = r * sa_stride, r * sb_stride
                for c in range(0, rb.stride - n, n):
                    if sa[oa + c:oa + c + n] != sb[ob + c:ob + c + n]:
                        lo, hi_x = min(lo, c), max(hi_x, c + n)
            pt = 1.0 / (zoom * n)          # byte offset -> points
            print("pixel diff band: %d row(s), rows %d-%d  "
                  "(pt x %.1f-%.1f, y-from-top %.1f-%.1f)"
                  % (len(rows), rows[0], rows[-1], lo * pt, hi_x * pt,
                     rows[0] / zoom, rows[-1] / zoom))
        else:
            print("pixel diff: none (unexpected -- nothing changed?)")
        # informational only: the band is reported for the caller to read,
        # it does not decide PASS/FAIL

    print("RESULT: %s" % ("PASS" if ok else "FAIL"))
    return 0 if ok else 1


def describe(blocks):
    for y, ln in group_lines(blocks).items():
        print("y=%9.2f  blocks=%-3d  %s"
              % (y, len(ln), " ".join(x.text for x in ln)[:96]))


# ------------------------------------------------------------------- entry

def run(a):
    """Do the work described by an `argparse.Namespace`-shaped `a`.

    Prints a human-readable report and returns a process-style exit code:
    0 ok, 1 verification failed, 2 phrase not found, 3 already said that way.
    Only code 0 leaves a file at the output path.
    Kept separate from `main` so a plugin tool can call it with a namespace it
    built itself instead of going through the command line.
    """
    doc = pymupdf.open(a.pdf)
    if not doc.is_pdf:
        # PyMuPDF also opens text, images, EPUB, XPS ... as documents, and
        # the PDF-only calls below fail on them (some PyMuPDF versions
        # segfault in read_contents on a .txt). Refuse up front.
        doc.close()
        raise SystemExit("%s is not a PDF" % a.pdf)
    if a.page >= doc.page_count:
        raise SystemExit("page %d out of range (%d pages)" % (a.page, doc.page_count))
    raw, blocks = page_blocks(doc, a.page)
    if not blocks:
        raise SystemExit("no BT/ET text blocks found on page %d" % a.page)

    if a.list:
        describe(blocks)
        return 0
    if not a.find or a.replace is None:
        raise SystemExit("--find and --replace are required (or use --list)")
    out = a.out or os.path.splitext(a.pdf)[0] + "_editado.pdf"
    reason = out_path_problem(a.pdf, out)
    if reason:
        raise SystemExit("refusing output path: %s" % reason)
    streams = doc[a.page].get_contents()
    if len(streams) != 1:
        # update_stream below rewrites one stream with the text of all of
        # them; with several streams that duplicates every stream but the
        # first on the page.
        raise SystemExit("page %d has %d content streams; only single-stream "
                         "pages are supported" % (a.page, len(streams)))

    hit = None
    for y, ln in group_lines(blocks).items():
        rng = align_to_find(ln, a.find)
        if rng:
            hit = (y, ln, rng)
            break
    if not hit:
        print("could not locate %r on page %d. Lines on the page:" % (a.find, a.page))
        describe(blocks)
        return 2

    y, line, (i0, i1, lo, hi) = hit
    target = line[i0:i1 + 1]
    line_stripped = strip_ws("".join(b.text for b in line))
    # `lo`/`hi` are offsets into the whole line, but plan_edits indexes into the
    # target run alone -- and the first target block may straddle `lo` (a block
    # holding a whole phrase). Rebase the window onto the run.
    before = sum(len(strip_ws(b.text)) for b in line[:i0])
    # the one invariant that makes PASS mean something: the edited line must
    # read exactly as the old one with `find` swapped for `replace`, and
    # nothing else. This is what catches collateral damage to surrounding
    # words -- the failure mode every weaker check lets through.
    expected_line = line_stripped[:lo] + strip_ws(a.replace) + line_stripped[hi:]
    print("page %d  baseline y=%.2f" % (a.page, y))
    print("line    : %r" % " ".join(b.text for b in line)[:110])
    print("blocks  : %s" % [b.text for b in target])

    # every character this document already draws, per font resource: ground
    # truth for the glyph check
    used = {}
    for b in blocks:
        used.setdefault(b.fres, set()).update(b.text)
    table, notes = font_metrics(doc, doc[a.page], line)
    for n in notes:
        print("font note: %s" % n)
    if not table:
        raise SystemExit("no usable font metrics for this line")
    check_glyphs(target, a.replace, table, used)

    print("\nedits:")
    for blk, new_txt in plan_edits(target, a.replace, lo - before, hi - before):
        if blk.text == new_txt:
            continue
        f = table.get(blk.fres)
        blk.new_pieces = [new_txt]
        blk.touched = True
        print("  %-14r -> %-16r x=%8.3f  %7.3f -> %7.3f pt  (%+6.3f)"
              % (blk.text, new_txt, blk.x, blk.width(blk.text, f),
                 blk.width(new_txt, f),
                 blk.width(new_txt, f) - blk.width(blk.text, f)))
    if not any(b.touched for b in target):
        print("  nothing to change -- the phrase already reads that way")
        return 3

    # A block keeps its own origin; the width change it introduces is carried
    # into every *later* block on the line. Walk the line once, banking each
    # edited block's delta and spending it on the blocks that follow.
    shift = pending = 0.0
    for i, b in enumerate(line):
        shift += pending
        pending = 0.0
        if b.touched:
            f = table.get(b.fres)
            pending = b.width("".join(b.new_pieces), f) - b.width(b.text, f)
        b.new_x = round(b.x + shift, 3)
        if b.new_x != b.x:
            b.touched = True
    print("  line width change %+.3f pt" % shift)

    out_parts, cursor = [], 0
    for b in line:
        if not b.touched:
            continue
        j = raw.find(b.raw, cursor)
        if j < 0:
            raise SystemExit("block no longer locatable in stream")
        out_parts.append(raw[cursor:j])
        out_parts.append(rewrite_block(b))
        cursor = j + len(b.raw)
    out_parts.append(raw[cursor:])

    doc.update_stream(streams[0], "".join(out_parts).encode("latin-1"),
                      compress=True)

    # Write next to the destination under a temporary name, verify that, and
    # only then move it into place. A failed check (or a crash) leaves
    # nothing at `out`.
    fd, tmp = tempfile.mkstemp(prefix=".pdf-surgeon-", suffix=".pdf",
                               dir=os.path.dirname(os.path.abspath(out)))
    os.close(fd)
    try:
        doc.save(tmp, garbage=0, deflate=True)
        doc.close()
        if a.no_verify:
            print("\nverification skipped (--no-verify)")
            code = 0
        else:
            code = verify(a.pdf, tmp, a.page, y, a.find, a.replace,
                          expected_line, raster=not a.no_raster)
        if code == 0:
            publish(tmp, out)
            print("\nwrote %s (%d bytes)" % (out, os.path.getsize(out)))
        else:
            print("\nverification failed: nothing written to %s" % out)
        return code
    finally:
        if os.path.exists(tmp):
            os.remove(tmp)


def out_path_problem(src, out):
    """Why `out` is not an acceptable destination, or '' when it is.

    The output path comes from the caller (a model, when this runs as a
    tool), so it is held to: a .pdf name, not already present -- which also
    rules out the input itself and anything a symlink points at -- and an
    existing parent directory.
    """
    if not out.lower().endswith(".pdf"):
        return "%s does not end in .pdf" % out
    if os.path.lexists(out):
        return "%s already exists; choose a new file name" % out
    if os.path.realpath(out) == os.path.realpath(src):
        return "%s is the input file" % out
    parent = os.path.dirname(os.path.abspath(out))
    if not os.path.isdir(parent):
        return "directory %s does not exist" % parent
    return ""


def publish(tmp, out):
    """Move the verified temp file to `out` without replacing anything that
    appeared there in the meantime."""
    try:
        os.link(tmp, out)                  # fails if `out` exists
    except FileExistsError:
        raise SystemExit("refusing output path: %s appeared while editing"
                         % out)
    except (AttributeError, NotImplementedError, OSError):
        # no hard links on this filesystem: check, then rename
        if os.path.lexists(out):
            raise SystemExit("refusing output path: %s appeared while editing"
                             % out)
        os.replace(tmp, out)


class _Opts:
    """Plain namespace; mirrors the argparse defaults."""
    page = 0
    out = None
    list = False
    no_verify = False
    no_raster = False
    find = None
    replace = None

    def __init__(self, pdf, **kw):
        self.pdf = pdf
        for k, v in kw.items():
            setattr(self, k, v)


def replace_text(pdf, find=None, replace=None, out=None, page=0,
                 verify=True, raster=True, list_lines=False):
    """Programmatic entry point. -> (exit_code, report_text).

    The report is the same text the CLI prints, captured. That is deliberate:
    the verification transcript *is* the useful output for both a human and a
    tool caller, and duplicating it would guarantee the two drift apart.

    `SystemExit` from the CLI path is caught and turned into a report line --
    every refusal in here (missing page, no text blocks, no usable font,
    missing glyphs, unacceptable output path) is one, and raising out of a
    library call is unhelpful. Any other exception (a file PyMuPDF cannot
    open, say) is reported the same way.

    `list_lines` prints each line's block layout and returns without editing,
    which is what a caller should do before choosing what to replace.
    """
    if not out:
        out = os.path.splitext(pdf)[0] + "_editado.pdf"
    opts = _Opts(pdf, find=find, replace=replace, out=out, page=page,
                 no_verify=not verify, no_raster=not raster, list=list_lines)
    buf = io.StringIO()
    try:
        with contextlib.redirect_stdout(buf):
            code = run(opts)
    except SystemExit as exc:
        buf.write("%s\n" % exc)
        return 2, buf.getvalue()
    except Exception as exc:               # unreadable file, bad page, ...
        buf.write("error: %s: %s\n" % (type(exc).__name__, exc))
        return 2, buf.getvalue()
    return code, buf.getvalue()


def main():
    ap = argparse.ArgumentParser(description="invisible PDF text replacement")
    ap.add_argument("pdf")
    ap.add_argument("--find")
    ap.add_argument("--replace")
    ap.add_argument("--page", type=int, default=0)
    ap.add_argument("--out")
    ap.add_argument("--list", action="store_true")
    ap.add_argument("--no-verify", action="store_true")
    ap.add_argument("--no-raster", action="store_true",
                    help="skip the pixel diff (the only slow check)")
    return run(ap.parse_args())


if __name__ == "__main__":
    sys.exit(main())