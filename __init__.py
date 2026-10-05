"""pdf-surgeon — replace text in a PDF, keeping its original typesetting.

Registers one tool, `pdf_replace_text`, into the ``pdf_surgery`` toolset.

Why not redact-and-re-stamp: PyMuPDF's ``add_text`` defaults to Helvetica 8pt
and you must restate font, size and colour by hand. Get any of that wrong --
the common failure -- and the result reads as forged: different typeface,
different size, a tell-tale rectangle. This plugin edits the page content
stream in place instead, so font, size, colour, baseline, char-spacing,
word-spacing, horizontal scale and kerning all survive untouched because they
are never rewritten.

Scope and refusal: this only works on lines laid out as one BT/ET block per
word/run with an explicit Tm origin, which is what Word, LibreOffice, Excel,
Google Docs and most digital-born PDFs emit. It refuses rather than guessing:
no phrase match, no usable font metrics, or a replacement glyph the embedded
subset does not contain all end the call without writing an output file.
The output is written under a temporary name next to the destination and
moved into place only after verification passes.
"""

from __future__ import annotations

from typing import Any, Dict


def _surgery():
    """Import the engine on demand, not at module load.

    `surgery` imports PyMuPDF at its top level, which is a compiled dependency
    the loader's interpreter may not have yet -- the validator's capability
    probe runs `register()` somewhere the declared deps are not installed. If
    this module imported `surgery` eagerly, a missing PyMuPDF would make the
    whole plugin unimportable instead of making one tool unavailable, and
    there would be no way to surface a useful message. `check_fn` is the
    designed place for that, so the import is deferred to here and failures
    become a readable error instead of an ImportError during discovery.
    """
    from . import surgery
    return surgery


def _missing_dep_reason() -> str:
    """Empty when the engine imports, otherwise why it does not."""
    try:
        _surgery()
    except ImportError as exc:
        return str(exc)
    return ""


def _check_pymupdf() -> bool:
    """Gate dispatch on the engine (and PyMuPDF under it) actually importing."""
    return not _missing_dep_reason()


PDF_REPLACE_TEXT_SCHEMA: Dict[str, Any] = {
    "name": "pdf_replace_text",
    "description": (
        "Replace a phrase in a digital-born PDF by rewriting the page content "
        "stream in place, so the new text keeps the original font, size, "
        "colour and spacing. For correcting or filling in documents the user "
        "is entitled to change (typos, template fields, their own drafts). "
        "Inspect the layout first with action='inspect': it prints each text "
        "line and how many BT/ET blocks it is built from; scans, Type3/XObject "
        "text and pages with several content streams cannot be edited. The "
        "phrase must match exactly (whitespace is ignored). Unless verify is "
        "false, the edit is checked before anything is written: the edited "
        "line must read exactly as the original with the phrase swapped, "
        "every other line on the page must be unchanged, block count, "
        "baseline, font, size and spacing must hold, and the page's "
        "font/size/colour set must be unchanged. A pixel-diff band is "
        "reported for information. If a check fails, nothing is written. "
        "Always writes a new file: 'out' must end in .pdf and must not exist "
        "yet. The file is re-saved by PyMuPDF, so its bytes, hash and trailer "
        "ID change and any digital signature on it no longer validates."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "action": {
                "type": "string",
                "enum": ["inspect", "replace"],
                "description": (
                    "'inspect' lists the text lines and their block layout. "
                    "'replace' performs the edit."
                ),
            },
            "pdf": {
                "type": "string",
                "description": "Absolute path to the source PDF. Never modified.",
            },
            "find": {
                "type": "string",
                "description": (
                    "Exact phrase to replace. Matching ignores whitespace, so "
                    "it need not reproduce the PDF's own spacing inside "
                    "'( 06 )'. Needed for action='replace'."
                ),
            },
            "replace": {
                "type": "string",
                "description": "Replacement text. Needed for action='replace'.",
            },
            "out": {
                "type": "string",
                "description": (
                    "Output path for the new PDF. Must end in .pdf and must "
                    "not already exist. Defaults to the input path plus "
                    "'_editado.pdf'. The source is never overwritten."
                ),
            },
            "page": {
                "type": "integer",
                "description": "Zero-based page index. Default 0.",
            },
            "verify": {
                "type": "boolean",
                "description": (
                    "Run the verification pass. Default true. With false the "
                    "edited file is written without any checks and may be "
                    "subtly wrong."
                ),
            },
        },
        "required": ["action", "pdf"],
    },
}


def _handle_pdf_replace_text(args: Dict[str, Any]):
    missing = _missing_dep_reason()
    if missing:
        return ("error: pdf-surgeon needs PyMuPDF, which is not importable in "
                "the Hermes plugin environment (%s). Install the plugin's "
                "declared dependency, or run the surgery directly with "
                "surgery.py." % missing)

    surgery = _surgery()
    action = (args.get("action") or "").strip().lower()
    pdf = args.get("pdf")
    if not pdf:
        return "error: 'pdf' is required"
    if action not in ("inspect", "replace"):
        return "error: action must be 'inspect' or 'replace'"
    try:
        page = int(args.get("page") or 0)
    except (TypeError, ValueError):
        return "error: 'page' must be an integer"

    if action == "inspect":
        code, report = surgery.replace_text(pdf, out=args.get("out") or _scratch(pdf),
                                            page=page, list_lines=True)
        return report

    find, replace = args.get("find"), args.get("replace")
    if not find or replace is None:
        return ("error: action='replace' needs both 'find' and 'replace' "
                "(or use action='inspect')")

    code, report = surgery.replace_text(
        pdf, find, replace,
        out=args.get("out"),
        page=page,
        verify=bool(args.get("verify", True)),
    )
    if code == 0:
        return report
    # Surface the refusal reason first: the transcript is long and the reason
    # is the one line that decides what the caller does next.
    return "edit refused or failed (code %d) — no output file was written\n\n%s" % (code, report)


def _scratch(pdf: str) -> str:
    """A harmless output path for --list, which writes nothing anyway."""
    import os
    return os.path.join(os.path.dirname(os.path.abspath(pdf)) or ".",
                        os.path.basename(pdf) + ".list.tmp.pdf")


def register(ctx) -> None:
    """Called once by the plugin loader."""
    ctx.register_tool(
        name="pdf_replace_text",
        toolset="pdf_surgery",
        schema=PDF_REPLACE_TEXT_SCHEMA,
        handler=_handle_pdf_replace_text,
        check_fn=_check_pymupdf,
        emoji="📄",
    )