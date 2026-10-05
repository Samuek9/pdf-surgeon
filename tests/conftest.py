"""Load the repo root as a package (it is a Hermes plugin directory, not an
installed module) and build small synthetic PDFs for the tests."""
import importlib.util
import os
import sys

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _load_plugin():
    name = "pdf_surgeon_under_test"
    if name in sys.modules:
        return sys.modules[name]
    spec = importlib.util.spec_from_file_location(
        name, os.path.join(ROOT, "__init__.py"),
        submodule_search_locations=[ROOT])
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture(scope="session")
def plugin():
    return _load_plugin()


@pytest.fixture(scope="session")
def surgery(plugin):
    return plugin._surgery()


def _esc(w):
    return w.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")


def _line(font, words, y):
    parts, x = [], 72.0
    for w in words:
        parts.append("BT /helv 12 Tf 1 0 0 1 %.3f %.3f Tm (%s) Tj ET"
                     % (x, y, _esc(w)))
        x += font.text_length(w + " ", 12)
    return "\n".join(parts)


def make_pdf(path, lines, multistream=False):
    """One BT/ET block per word with an explicit Tm, base-14 Helvetica --
    the layout Word/LibreOffice exports and this tool edits."""
    import pymupdf
    doc = pymupdf.open()
    page = doc.new_page()
    page.insert_font(fontname="helv")
    page.insert_text((10, 10), " ", fontname="helv")   # creates /Contents
    font = pymupdf.Font("helv")
    chunks = [_line(font, words, 700 - 40 * i) for i, words in enumerate(lines)]
    xref = page.get_contents()[0]
    if multistream:
        doc.update_stream(xref, chunks[0].encode("latin-1"))
        extra = doc.get_new_xref()
        doc.update_object(extra, "<<>>")
        doc.update_stream(extra, "\n".join(chunks[1:]).encode("latin-1"))
        doc.xref_set_key(page.xref, "Contents", "[%d 0 R %d 0 R]" % (xref, extra))
    else:
        doc.update_stream(xref, "\n".join(chunks).encode("latin-1"))
    doc.save(path)
    doc.close()
    return path


@pytest.fixture
def sample(tmp_path):
    return make_pdf(str(tmp_path / "in.pdf"), [
        ["Issued", "on", "six", "(06)", "days", "of", "July"],
        ["Second", "line", "here"],
    ])
