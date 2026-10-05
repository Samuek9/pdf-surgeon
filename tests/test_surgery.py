import os

import pymupdf
import pytest

from conftest import make_pdf


def page_text(path):
    with pymupdf.open(path) as doc:
        return doc[0].get_text()


def leftovers(directory):
    return [f for f in os.listdir(directory) if f.startswith(".pdf-surgeon-")]


def test_replace_happy_path(plugin, sample, tmp_path):
    out = str(tmp_path / "out.pdf")
    report = plugin._handle_pdf_replace_text({
        "action": "replace", "pdf": sample, "out": out,
        "find": "six (06) days of July", "replace": "five (05) days of October"})
    assert "RESULT: PASS" in report
    assert "five (05) days of October" in page_text(out)
    assert "Second line here" in page_text(out)
    assert leftovers(tmp_path) == []


def test_refuses_existing_non_pdf_out(plugin, sample, tmp_path):
    victim = tmp_path / "bashrc"
    victim.write_text("export PATH=$HOME/bin:$PATH\n")
    report = plugin._handle_pdf_replace_text({
        "action": "replace", "pdf": sample, "out": str(victim),
        "find": "six", "replace": "five"})
    assert "refusing output path" in report
    assert victim.read_text() == "export PATH=$HOME/bin:$PATH\n"


def test_refuses_existing_pdf_out(plugin, sample, tmp_path):
    existing = tmp_path / "already.pdf"
    existing.write_bytes(b"keep me")
    report = plugin._handle_pdf_replace_text({
        "action": "replace", "pdf": sample, "out": str(existing),
        "find": "six", "replace": "five"})
    assert "already exists" in report
    assert existing.read_bytes() == b"keep me"


def test_refuses_symlink_out(plugin, sample, tmp_path):
    # Creating a symlink needs elevation on Windows (WinError 1314) and is not
    # available in some hardened Linux setups. Without this the test errors in
    # its own body before reaching the plugin, which reads as a real failure.
    if not hasattr(os, "symlink"):
        pytest.skip("os.symlink unavailable on this platform")
    target = tmp_path / "config.yaml"
    target.write_text("model: x\n")
    link = tmp_path / "link.pdf"
    try:
        os.symlink(target, link)
    except (OSError, NotImplementedError) as exc:
        pytest.skip(f"cannot create symlinks here: {exc}")
    report = plugin._handle_pdf_replace_text({
        "action": "replace", "pdf": sample, "out": str(link),
        "find": "six", "replace": "five"})
    assert "refusing output path" in report
    assert os.path.islink(link) and target.read_text() == "model: x\n"


def test_refuses_input_as_out(plugin, sample):
    before = open(sample, "rb").read()
    report = plugin._handle_pdf_replace_text({
        "action": "replace", "pdf": sample, "out": sample,
        "find": "six", "replace": "five"})
    assert "refusing output path" in report
    assert open(sample, "rb").read() == before


def test_refuses_missing_directory(plugin, sample, tmp_path):
    report = plugin._handle_pdf_replace_text({
        "action": "replace", "pdf": sample,
        "out": str(tmp_path / "nope" / "out.pdf"),
        "find": "six", "replace": "five"})
    assert "does not exist" in report


def test_failed_verification_writes_nothing(surgery, sample, tmp_path,
                                            monkeypatch):
    monkeypatch.setattr(surgery, "verify", lambda *a, **k: 1)
    out = str(tmp_path / "out.pdf")
    code, report = surgery.replace_text(sample, "six", "five", out=out)
    assert code == 1
    assert "nothing written" in report
    assert not os.path.exists(out)
    assert leftovers(tmp_path) == []


def test_crash_during_verification_writes_nothing(surgery, sample, tmp_path,
                                                  monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("verifier crashed")
    monkeypatch.setattr(surgery, "verify", boom)
    out = str(tmp_path / "out.pdf")
    code, report = surgery.replace_text(sample, "six", "five", out=out)
    assert code == 2 and "verifier crashed" in report
    assert not os.path.exists(out)
    assert leftovers(tmp_path) == []


def test_absent_phrase_is_refused_not_fuzzy_matched(surgery, sample, tmp_path):
    out = str(tmp_path / "out.pdf")
    code, report = surgery.replace_text(sample, "days of Julyy", "days of May",
                                        out=out)
    assert code == 2
    assert not os.path.exists(out)


def test_empty_replacement_deletes_phrase(surgery, sample, tmp_path):
    out = str(tmp_path / "out.pdf")
    code, report = surgery.replace_text(sample, "(06)", "", out=out)
    assert code == 0, report
    assert "(06)" not in page_text(out)


def test_insertion_at_block_boundary(surgery, sample, tmp_path):
    out = str(tmp_path / "out.pdf")
    code, report = surgery.replace_text(sample, "of July", "of July 2026",
                                        out=out)
    assert code == 0, report
    assert "July2026" in page_text(out).replace(" ", "")


def test_phrase_repeated_on_page(surgery, tmp_path):
    src = make_pdf(str(tmp_path / "in.pdf"),
                   [["six", "days"], ["six", "days", "later"]])
    out = str(tmp_path / "out.pdf")
    code, report = surgery.replace_text(src, "six days", "ten days", out=out)
    assert code == 0, report
    text = page_text(out)
    assert text.index("ten days") < text.index("six days later")


def test_multistream_page_refused(surgery, tmp_path):
    src = make_pdf(str(tmp_path / "in.pdf"),
                   [["six", "(06)", "days"], ["Second", "line"]],
                   multistream=True)
    out = str(tmp_path / "out.pdf")
    code, report = surgery.replace_text(src, "six", "ten", out=out)
    assert code == 2 and "content streams" in report
    assert not os.path.exists(out)


def test_unreadable_input_is_reported(plugin, tmp_path):
    notes = tmp_path / "notes.txt"
    notes.write_text("hello\n")
    report = plugin._handle_pdf_replace_text({"action": "inspect",
                                              "pdf": str(notes)})
    assert "is not a PDF" in report


def test_bad_page_is_reported(plugin, sample):
    report = plugin._handle_pdf_replace_text({"action": "inspect",
                                              "pdf": sample, "page": "x"})
    assert report == "error: 'page' must be an integer"


def test_inspect_writes_nothing(plugin, sample, tmp_path):
    before = sorted(os.listdir(tmp_path))
    report = plugin._handle_pdf_replace_text({"action": "inspect",
                                              "pdf": sample})
    assert "Issued on six (06) days of July" in report
    assert sorted(os.listdir(tmp_path)) == before

