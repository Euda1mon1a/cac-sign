# cac-sign -- design notes

**`cac_sign.py`** fills, CAC-signs and verifies *any existing* PDF on a Mac. Two front
ends sit on top of it: the Finder Quick Actions ("CAC Sign" to confirm and sign, and
the "CAC Sign…" review page). Signing code shared by all of them is in
`cac_signing.py`. `README.md` is the user guide; this file is why the code is the way
it is. Read it before changing a check.

It was built in October 2026 after Acrobat in AVHE failed to sign a course-request
form ("General failure"). The CAC signed correctly every time, but the document
digest never matched the bytes Acrobat wrote to the Mac through Citrix drive mapping.

## Layout and install

- `install.sh` puts the code in `~/.cac-sign` (or, run from a clone, links
  `~/.cac-sign` to the clone), runs `uv sync --locked`, copies `quick-action/*.workflow`
  to `~/Library/Services`, and adds one marked line to `~/.zshrc`. Both Quick Actions
  and `cac-sign.zsh` call `$HOME/.cac-sign/.venv/bin/python` directly, so nothing
  depends on PATH or on where a clone lives.
- **Packaged installs (PyPI, Homebrew).** `cac_sign_launch.py` is the `cac-sign`
  console script. The wheel (hatchling `force-include`) puts it in as the package
  `cac_sign_app/__init__.py` with every module and data file beside it, unchanged;
  it adds its own folder to `sys.path`, so the modules keep importing each other by
  plain name and `HERE`-relative data (profiles, `editor.html`, Quick Action
  templates) keeps working. It routes `--quick-action` and `--review` to the two
  front ends, and `cac-sign --setup` copies the Quick Actions with their command
  rewritten (plistlib) to `exec <installed cac-sign> --quick-action|--review`.
  `_command()` uses the PATH link (Homebrew's bin, uv's `~/.local/bin`) so an
  upgrade doesn't break them. The comment line names `cac_sign_finder.py` or
  `cac_sign_editor.py`, which is how `install.sh` and `--setup --remove` recognise
  them as ours. `requires-python >= 3.10`, `pyhanko >= 0.35`, `pikepdf >= 10`: older
  versions behave differently in the checks. `[tool.uv] package = false` stays,
  so the curl installer still runs the scripts in place.
- **User data lives in `~/Library/Application Support/cac-sign`** (`APP_SUPPORT`):
  `identity.json` and `ledger.jsonl`, folder 0700, files 0600. Updates replace the
  code folder wholesale (keeping `.venv` and any extra `profiles/*.json`), so nothing
  personal may live next to the code. A test pins this.
- The installer is bash 3.2 (macOS `/bin/bash`), everything inside `main()` so a
  truncated download runs nothing. It installs the release tag it names
  (`CAC_SIGN_REF`, bumped with each release), and fetches uv, when missing, as a
  release tarball checked against a pinned SHA-256 rather than running a script.
  An EXIT trap puts `.venv` and the previous install back after a failure or Ctrl-C.
  It replaces or removes a Quick Action only if the workflow runs one of our scripts,
  and `--uninstall` edits `~/.zshrc` with `grep -a` (a stray NUL byte once turned the
  file into "Binary file matches"), checks the line count, and keeps
  `~/.zshrc.cac-sign.bak`. It needs Apple silicon because `cryptography` 49 ships
  macOS wheels for arm64 only.

## cac_sign.py -- sign existing PDFs (shell command `cac-sign`)

### DOCX intake and suggested fields (0.2.0)

- `document_inputs.py` exports a private snapshot of an unsigned DOCX through a
  local LibreOffice installation. Its isolated profile disables macros and active
  embedded content. Conversion has a 90-second timeout. The resulting PDF is
  validated and created exclusively with mode 0600 next to the Word original;
  existing PDFs and symlinks are never replaced. Word digital-signature parts are
  refused because conversion cannot preserve those signatures. LibreOffice is
  optional for PDF-only use. Export can change layout; the owner reviews the PDF.
- The plain DOCX CLI and both Finder actions open the review page. Explicit CLI
  placement still supports conversion followed by the usual PDF signing checks.
  `--verify` refuses DOCX rather than reporting the converted PDF as verified.
  The shell entry point routes through `cac_sign_launch.py`, like packaged installs.
- `field_detection.py` is a local heuristic over PDFKit's selectable text and page
  geometry. It finds underscores, dashed blanks and empty checkbox characters,
  derives labels from nearby text, and suggests a signature placement only when
  exactly one signature line is detected and no native signature target is chosen.
  It has no form-specific template, model call, OCR or vector-line recognition.
  NSString ranges use UTF-16 units. Rotated pages are left for manual handling.
- `review_model` adds suggestions only to the page's model. It does not change the
  source PDF, duplicate existing widgets, infer personal values or select consent.
  Blocked or already signed PDFs receive no suggestions. Failures are visible and
  leave manual placement available. Multiple signature lines are not auto-selected.
- `Add text` and detected fields use `_add_text_fields` to add text or checkbox
  widgets only to a private unsigned snapshot. Names, labels, kinds, page indices
  and finite page-contained rectangles are validated. Each widget has an appearance
  stream; checkbox states are explicit `/Off` and `/Yes`. `/TU` holds readable
  labels. The existing fill code writes values and appearances. Review, snapshot
  hashing, signature verification and visual comparison then run as before.
- Suggestions can be removed. **Save filled copy** works without signing. The page
  can never supply an arbitrary output path, and the original source is unchanged.
  Tests use synthetic forms and certificates; no real form, identity or signature
  material belongs in the repository or release archive.

### PDF signing invariants

- **Incremental only.** Fills and the signature go in one appended revision through
  pyHanko's `IncrementalPdfFileWriter`, so earlier signatures stay intact. Never
  `pikepdf.save()` a signed PDF: a full rewrite breaks every existing signature.
- **Profiles** (`profiles/*.json`) recognise a form by field names and say what to fill
  and which field to sign. Personal values come from `identity.json` in Application
  Support. Never commit names or phone numbers into a profile.
- **`verify_signatures` fails closed.** `ok` is pyHanko's `bottom_line`: intact,
  valid, trusted, timestamps and seed values all good, and every later change at
  most form filling. Changes that were never analysed also count as a failure.
  `detail` names each failing part.
  - `make_plan` refuses an input whose existing signatures are not `ok`, unless
    `--force` is given. This catches a self-made cert carrying the applicant's
    name, and content rewritten after signing.
  - `--verify` exits 1 on any signature that is not `ok`.
  - Both were tightened after a security review. Before that, an intact signature
    counted as OK.
- **Verification is the definition of done.** Each output is checked before it is
  moved into place:
  - every signature is still intact and valid;
  - the new signature covers `ENTIRE_FILE`;
  - `audit_last_revision` passes. It allows only the touched fields, their widgets
    and the document-level objects pyHanko's signer always rewrites (catalog,
    info, XMP, AcroForm).

  A failed check leaves `*.FAILED.pdf` and exits 1. Trust and earlier changes are
  not re-gated here, because the input already passed them. A signature that is
  intact but not `ok` prints as a `note:` under `DONE`.
- **pyHanko diff analysis cannot gate this tool's own revision** (pyHanko 0.35.1).
  - Its in-place appearance rule crashes on standard checkboxes (direct `/AP /N`
    state dicts), so `_diff_policy()` turns that rule off. That is stricter, not
    looser.
  - It cannot justify radio-button widget `/AS` updates and reports them as
    `OTHER`, even though that is exactly how Acrobat fills a radio. So after this
    tool fills a radio, the earlier signer's `ok` reads False. `audit_last_revision`
    is the gate for the tool's own write.
  - A later run on that output needs `--force`, after a look in Acrobat.
- **Trust and chain.** Trust anchors are the macOS trust store, which must hold the
  DoD roots. Tests add throwaway roots through `EXTRA_TRUST_ROOTS`.
  - The CAC holds only its own certs. `keychain_ca_certs()` and `issuer_chain()`
    find the issuing DOD EMAIL CA and root in the keychain, and the signers embed
    them (`ca_chain`), as Acrobat does.
  - The same keychain CAs are offered as `other_certs` for path building when
    verifying. They are never trust anchors.
- **The PIN belongs to macOS, not this tool.** On a Mac, `cac_sign.py` signs through
  Security.framework, using pyobjc and Apple's built-in PIV token driver
  (`com.apple.pivtoken`).
  - `SecKeyCreateSignature` on the card's key makes macOS show its own smart-card
    PIN window. No OpenSC is needed, and the PIN never enters the process.
  - The nonRepudiation key is PIN-always, so the window appears once per signature.
    `--opensc` keeps the PKCS#11 route: one PIN typed in Terminal per batch, via
    `reaffirm_credentials`.
  - Cancel maps to `SigningCancelled`: errSecUserCanceled -128, or CryptoTokenKit
    -4. A wrong PIN (CryptoTokenKit -5) and other card errors map to
    `SigningError`. Either one stops the batch, because the CAC locks after three
    wrong PINs.
  - `execute` writes through a randomly named `.partial` file created with
    `mkstemp` (exclusive, written through its descriptor, so a planted symlink can't
    redirect it) and removes it on any exception.
  - Apple's `/usr/lib/ssh-keychain.dylib` PKCS#11 bridge was tried: python-pkcs11
    cannot load it (FunctionNotSupported).
- **Review page (`cac_sign_editor.py`).** Security model:
  - single-threaded `HTTPServer` on 127.0.0.1, random port; never a
    `ThreadingHTTPServer` (an unbounded thread-per-request server has panicked a
    Mac before);
  - a one-time token, carried in the URL fragment and sent as `X-CAC-Token`, plus
    a Host-header check against DNS rebinding;
  - no CORS headers, and a CSP with a per-load nonce and `default-src 'none'`;
  - it serves only the PDFs it was started with and writes only new files
    (`free_output`); the response never carries full paths;
  - idle exit after 10 minutes, kept alive by a 60-second ping from the page;
  - PDF text goes to the DOM with `textContent` only. A test greps for
    `innerHTML`.

  **What you see is what you sign.** The first version drew pages without
  annotations and painted /V values over them. A PDF could hide a stamp, or show a
  field appearance different from its /V. Now:
  - pages render from a pikepdf `flatten_annotations("screen")` copy, held in
    memory and never saved, which is what viewers show;
  - unchanged fields get only an outline; edits are painted opaque;
  - **Readers must agree** (`reader_problems`). Rendering goes through qpdf,
    signing through pyHanko and display through Apple's CoreGraphics; Acrobat is a
    fourth reader that cannot be run. So the page refuses to sign when:
    - qpdf had to repair the file, or `check_pdf_syntax` reports a problem;
    - pyHanko's strict parser rejects it;
    - the file is XFA or has hybrid tables. That is detected from the parsed
      data (pyHanko `xrefs.hybrid_xrefs_present`, qpdf's trailer), because a
      byte search misses `/XRef#53tm`;
    - the readers disagree on pages or fields (types and text/name values);
    - the three readers' views differ: `_q_view`, `_h_view` and `_cg_view`
      compare each page's decoded content digest and every annotation's
      subtype, rect, /F, /AS and the decoded digest of the normal appearance
      selected by /AS;
    - CoreGraphics draws the original's page content differently from qpdf's
      re-save;
    - something is hidden on screen but prints (`_screen_print_problems`): an
      annotation with Hidden or NoView plus Print, or optional content whose default
      configuration switches a layer off or auto-switches by usage, or a layer whose
      own /Usage carries Print, View or Zoom behaviour or whose /Intent leaves out
      /View (a content-stream cover in a "don't print" layer got past the default-
      configuration check; real layers carry only CreatorInfo or PageElement).
      `_optional_content` finds groups by walking from the pages (resources,
      XObjects, annotation /OC, OCMDs) and the layer table, so a group left out of
      /OCGs, or defined inline with no table at all, is still judged. Being left out
      of /OCGs is not itself refused: Acrobat's Headers/Footers do that on real forms.
      CoreGraphics doesn't resolve OCMD /VE, OCMD policies or radio groups per spec,
      so an OCMD with /VE or a /P other than /AnyOn, an RBGroup with 2+ members,
      and alternate /Configs are refused (none in 177 real PDFs). Residual, named in
      the README: an undeclared group's state and the viewer's choice of config are
      not fixed by the spec, and one renderer can't prove what Acrobat shows; the
      layer warning makes the signer check layered files in Acrobat. The review shows
      the screen view, and a printout is what gets filed. A security review signed
      a NoView+Print "APPROVED FOR 90 DAYS EXCESS LEAVE" before this. In real
      paperwork it only ever caught a DocuSign envelope-ID stamp. It also refuses a
      form-field widget that names its page in /P but is missing from that page's
      /Annots (`_orphan_widgets`): the review draws /Annots, while a viewer that
      builds fields from /AcroForm may draw it. Orphans without /P, which flattened
      forms leave behind (2 of 55 real forms), are a viewer warning instead. The walk
      refuses rather than stops: deeper than 32 levels, or a non-dictionary entry,
      counts as a placed orphan. `_misplaced_annotations` refuses any annotation,
      direct or indirect, whose /P names another page or that sits on several pages
      (none of 177 real PDFs has one);
    - a box shown on screen but not printed covers something that prints
      (`_screen_only_boxes`, `_covered_print`): the print view (pikepdf
      `flatten_annotations("print")`) is rendered and each screen-only box's area
      checked for non-uniform pixels. The review's screen render showed a white box
      while the printout showed "APPROVED: 90 DAYS EXCESS LEAVE" under it (fourth
      review round). Screen-only Print/Reset buttons over empty areas (5 of 55 real
      forms have them) pass;
    - any of these checks raises: `reader_problems` returns "its structure could not
      be checked (...)" instead of crashing, so the file is refused with a reason;

    Real forms pass in about 50 ms. The readers' results are cached per file on
    inode, size, mtime and ctime (ctime can't be set back by the writer). The code
    lives in `pdf_readers.py`.
    `cac_sign.make_plan` runs the same gate whenever it signs, so the CLI and the
    quick Quick Action get it too. It imports `pdf_readers` inside the function,
    because `pdf_readers` imports `cac_sign`. In the CLI, `--force` overrides it;
    the review page never does.
  - Sign runs `prepare` on a **private snapshot**: one read of the bytes into the
    0700 stage folder, with its SHA-256 recorded. It stages a fill-only copy from
    the snapshot with the same fill code, renders reference PNGs, and the page
    shows the staged copy.
  - `commit` re-checks the snapshot's hash and signs the snapshot, never the
    original path, so a file swapped on disk after review cannot be signed. It
    signs in one revision, which keeps pyHanko's radio-button blind spot out of
    the pre-flight, then `_visual_diff`s the signed render against the reference
    with the signature box masked. A mismatch leaves `.FAILED.pdf`;
  - `viewer_warnings` (in `pdf_readers.py`) flags what is signable but may look
    different elsewhere: NeedAppearances, JavaScript or AA actions, page areas
    outside the CropBox, form fields on no page, comment text, and layers (readers
    can switch a layer off and reveal what it covered). They are not refusals, because real forms carry them, but the
    signer must have seen them: `prepare` sets `needs_ack` for them as for flagged
    changes, and `make_plan` adds them to the CLI and quick Quick Action plan as
    `WARNING:` lines;
  - `describe_changes` lists every edit in the review, with flags when it
    replaces an existing value, sits above an earlier signer's signature on a
    field the profile doesn't fill, or isn't a profile field. Any flag disables
    **Sign this** until "I meant to make the flagged changes" is ticked. The server
    enforces it as well: `prepare` sets `needs_ack`, and `/api/commit` and
    `commit()` refuse unless `ack is True`, leaving the review open (a review found
    the tickbox once existed only in the page). `sign_direct`, which has no review
    step and is not served over HTTP, passes `ack=True` explicitly. The rule exists
    because stray test values once landed in an applicant's fields under a real
    signature;
  - there is no one-shot sign endpoint over HTTP.

  Credentials: the editor CLI has no `--p12` or `--p12-pass`; tests pass extra
  args to `Editor` directly. The browser opens through NSWorkspace, so the token
  is not in any argv. `--background` sends stdout to DEVNULL and stderr to a 0600
  log, and refuses `--no-browser`.
  `to_display` and `from_display` map PDF space to page fractions, including
  `/Rotate`. `--background` detaches with `start_new_session`, so the Quick Action
  returns at once.
- **Signing log** (`LEDGER` = `~/Library/Application Support/cac-sign/ledger.jsonl`).
  - `execute` appends one JSON line after a verified signature: `when`,
    `input_sha256`, `output`, `output_sha256`, `field`, `signer`. It never stores
    form content, and failing to write the log never fails a signature.
  - `make_plan` notes earlier signings of identical input bytes; so do the review
    page's banner and its review dialog.
  - `--history` lists the log. `newest_first` breaks same-second ties by line
    order.
  - Tests redirect `LEDGER` and `IDENTITY_FILE` through an autouse fixture.
- **Terminal output goes through `say()`/`printable()`.** Field names and values,
  signer CNs and exception text come from the PDF, and an ESC sequence or newline in
  them could fake a `DONE` line. Never `print()` PDF-derived text directly.
- **Appearances.** Text appearances follow Acrobat's auto-size, about (h-2) x 0.741.
  The visible signature uses `stamp_style()`. pyHanko's default stamp is Courier
  with a border and a pyHanko-logo watermark, and its text box pads 10 pt on every
  side. Zero that padding or the text halves.

## CAC specifics (hard-won on a live card -- keep)

- **Multiple certs:** DoD CACs carry several certs, so a bare PKCS#11 signer errors
  with "Found more than one certificate." The signers auto-select the
  non-repudiation signing cert by key usage, print which one, and accept
  `--cert-label` / `--cert-id` overrides. `--list-certs` shows them.
- **CKA_ALWAYS_AUTHENTICATE (chip-enforced):** the non-repudiation key requires a
  context-specific login before *every* signature; no session login covers more
  than one. On the OpenSC route the PIN (collected once) is passed via pyHanko
  `base_sign_kwargs={'pin': ...}`, so python-pkcs11 calls
  `reaffirm_credentials(pin)` between SignInit and Sign for each file. One PIN entry
  signs the whole batch.
- **OpenSC module locations:** `/opt/homebrew/lib/opensc-pkcs11.so`
  (`brew install opensc`), or `/Library/OpenSC/lib/...` from the `.pkg` installer.

## Files

| File | Purpose |
|------|---------|
| `cac_signing.py` | Shared CAC code: native macOS signer (`build_keychain_signer`), PKCS#11/OpenSC signer + PIN entry, signing, `verify_signatures`, `stamp_style`, `printable`. |
| `cac_sign.py` / `cac-sign.zsh` | Fill + sign + verify for existing PDFs, and its shell command. |
| `cac_sign_finder.py`, `quick-action/CAC Sign.workflow` | Finder Quick Action: plan + Sign/Cancel dialog (no password field), then `cac_sign.main()` (macOS asks the PIN), then a result dialog. Dialog text reaches AppleScript as an argument, never as script source. With no file arguments it signs the Finder selection. |
| `cac_sign_editor.py`, `editor.html`, `quick-action/CAC Sign….workflow` | The review page. Edits go through `cac_sign.main()` with `--no-profile-fill --set ...`, so all the CLI checks apply. |
| `pdf_readers.py` | Page rendering (flattened annotations, Quartz) and `reader_problems`, the qpdf / pyHanko strict / CoreGraphics agreement gate. |
| `profiles/*.json`, `identity.example.json` | Per-form profiles; template for `identity.json`. |
| `install.sh` | Installer, updater and `--uninstall`. |
| `cac_sign_launch.py` | The `cac-sign` command for PyPI/Homebrew installs: routing and `--setup`. |
| `RELEASING.md`, `scripts/test-installer.sh`, `scripts/test-uv-install.sh` | The release checklist, and the scratch-HOME checks for the curl installer and a uv/PyPI install. |
| `tests/test_cac_sign.py` | End-to-end tests on synthetic forms with throwaway certs: `uv run --with pytest pytest -q tests`. |
| `pyproject.toml` / `uv.lock` / `requirements.txt` | Dependencies: reportlab, pikepdf, pyhanko[pkcs11], and on macOS pyobjc-framework-Security (native signing) and pyobjc-framework-Quartz (page rendering). |

## Open items

- First-run setup in the review page, instead of the installer prompt.
- "Remember this form", which creates profiles from the page.
- An email hand-off that only drafts, never sends.
- A card check up front, plus a certificate-expiry warning.
- Bulk signing in the page. Bulk "one PIN for many" stays on `--opensc`, because
  macOS asks for the PIN once per signature.
- An OCSP revocation check, with optional LTV.
- A read-only install (code not writable by the user), and profiles taken only from
  the reviewed repo.
- Intel Macs: would need `cryptography` held below 49 for x86_64.
- The quick Quick Action's native-PIN path is the same signer the review page uses,
  but has itself been run live only on the OpenSC route.
