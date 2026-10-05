# pdf-surgeon

Replace a phrase inside an existing PDF so the edit is **not visible**.

The obvious approach — redact the old words and stamp the new ones over the
top — defaults to Helvetica 8pt and makes you restate font, size and colour by
hand. Get any of that wrong, which is the common failure, and the page reads as
forged: different typeface, different size, a tell-tale rectangle.

This plugin edits the page content stream **in place**. It rewrites the string
operand inside the `BT ... ET` block that already holds the text and retargets
the `Tm` origin of everything after it on the line. Font, size, colour,
baseline, char-spacing, word-spacing, horizontal scale and kerning survive
untouched, because they are never rewritten.

## Use

```
pdf_replace_text(action="inspect", pdf="/abs/path/in.pdf")
pdf_replace_text(action="replace", pdf="/abs/path/in.pdf",
                 find="six (06) days of July",
                 replace="five (05) days of October",
                 out="/abs/path/out.pdf")
```

`inspect` prints every text line with its baseline and how many `BT`/`ET`
blocks it is built from. Do it first: it is how you find out whether the
document is one this can edit at all.

## When it works

Requires a **digital-born** PDF whose text is laid out as one block per
word/run, each with an explicit `Tm` origin. Word, LibreOffice, Excel, Google
Docs and most PDF exporters emit this.

It will not work on: scans and image-only pages, `Type3` or `XObject` text, or
an edit that needs a glyph the embedded font subset does not have. It **refuses
in each case** and writes no output file — it never falls back to substituting
a different typeface, because a subtly wrong font is worse than no edit.

## What "verified" means

Every replacement is checked before the result is accepted:

| Check | Catches |
|---|---|
| edited line reads exactly as the original with the phrase swapped, nothing else | neighbouring words silently eaten |
| block count on the line unchanged | an edit that dropped a block |
| no baseline / font / size / `Tc` / `Tw` / `Tz` drift | restyling of untouched runs |
| every other line on the page has the same blocks and text | collateral edits off the target line |
| `(font, size, colour)` set unchanged page-wide | a new face sneaking in |
| the phrase occurs once less (plus any copies inside the replacement) | the edit landing nowhere |
| page count unchanged | structural damage |

Reported for the caller to read, not part of PASS/FAIL: how many blocks moved,
whether the output is encrypted or was repaired, and the pixel-diff band
(rows and x-range of changed pixels).

The phrase must match exactly (whitespace ignored); there is no fuzzy
fallback, so a near miss is refused instead of editing similar text. Pages
with more than one content stream are refused.

The first one is the decisive check. The others can all pass while surrounding
words have been eaten; only that one cannot. It is why "the old phrase is gone"
plus "the new word appears" is not a verification, and this plugin does not
report success on those alone.

## Safety

The input file is **never** modified. Output goes to `out`, defaulting to the
input path plus `_editado.pdf`. `out` must end in `.pdf` and must not already
exist (so it can never be the input, an existing file, or a symlink); the
tool refuses otherwise. The edited file is saved under a temporary
`.pdf-surgeon-*.pdf` name in the destination directory, verified there, and
moved to `out` only if verification passes. Refusals and failed verifications
leave no output file behind. With `verify=false` the file is written
unchecked.

## Disclosure

For the catalog reviewer, stated plainly so it does not have to be inferred:

- **Files read**: only the source PDF you name, and (via PyMuPDF) the font
  programs embedded in it. Nothing outside the given paths.
- **Files written**: only the `out` path you name, which must be a new `.pdf`,
  plus a temporary file beside it that is removed before the call returns.
  The input PDF is opened read-only and is never written back. Refusals and
  failed verifications write nothing.
- **Network**: none. No telemetry, no analytics, no update check, no remote
  fetch of any kind. The plugin never replaces its own files.
- **Credentials**: none. No `requires_env`, no secrets, no reads of any other
  tool's login or token store.
- **Subprocesses**: none. No shell commands, no background processes, no
  timers. No waits on a human, so it behaves identically under cron and the
  messaging gateway.
- **Host**: pure computation via PyMuPDF. One declared dependency,
  `pymupdf>=1.24.3,<2`, installed by Hermes for this plugin (it is not a
  Hermes core dependency).
- **Hermes core**: untouched. Extends only via `ctx.register_tool`, and runs
  only when the agent calls the tool.

The one thing worth a user's attention is inherent to what the tool is for:
the edited page looks typeset by the original author. What does change, and
what a document examiner can check: the file is re-saved by PyMuPDF as a
single revision, so its bytes and hash differ, the second half of the trailer
`/ID` changes, any earlier incremental-save history is dropped, and any
digital signature no longer validates. The Info dictionary (producer,
modification date) is left as it was. Use it on documents you are entitled
to change.

## License note

PyMuPDF is licensed under the GNU AGPL-3.0 (or a commercial license from
Artifex). This plugin imports it at runtime; if you redistribute the plugin
together with PyMuPDF, or offer it as a network service, check that the AGPL
terms work for you.

## Notes for the curious

Matching ignores whitespace, because where an exporter put spaces inside
`( 06 )` is an artefact of the exporter, not something a caller knows. Widths
are measured with the **embedded** font program rather than the system font of
the same name, since the subset's advances are what the reader will apply.

The same approach, with the reasoning behind each rule, is documented in the
`invisible-pdf-text-edit` skill.