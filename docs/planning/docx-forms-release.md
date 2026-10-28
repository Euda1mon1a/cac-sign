# DOCX forms and field detection release

Target: 0.2.0. Owner requested documentation, GitHub and PyPI publication, including
the existing Homebrew release route.

## Scope and approach

Convert unsigned Word forms locally to new PDFs, suggest editable fields from
printed blanks, and retain the existing reviewed-snapshot signing path. Use
PDFKit text geometry for detection and LibreOffice for DOCX conversion. Keep the
original files untouched and all personal form content outside source control.

Implementation spans `document_inputs.py`, `field_detection.py`, the CLI/Finder
routing, `cac_sign_editor.py`, `editor.html`, package manifests and Quick Action
templates. The installed checkout is preserved while publication is prepared in
an isolated worktree. Homebrew changes belong in an independent tap worktree.

## Acceptance and evidence

- DOCX inputs convert locally; existing output files and symlinks are preserved.
- Suggestions have readable labels; consent starts unchecked; a single signature
  line is proposed; existing widgets and already signed files are respected.
- Saving, review, private-snapshot hashing, signing and post-sign verification pass
  with synthetic forms and throwaway certificates. Both checkbox states are tested.
- The owner's provided form was inspected locally and detected six text fields,
  one checkbox and one signature line. Synthetic test entries were confined to
  temporary copies. Live-CAC signing and the owner's actual entries are separate.
- Before release preparation: 105 tests passed on Python 3.12 and the browser
  displayed the detected fields. Release checks also cover Python 3.14, scratch
  installer scenarios, a clean wheel install and package-content inspection.
- Release preparation repeated all 105 tests successfully on both Python 3.12 and
  Python 3.14. Installer scenarios and a clean wheel install passed, including
  DOCX service registration and detection on a synthetic printed form. Package
  inspection found both new modules and no user documents or signing material;
  every dependency pin and artifact hash remained unchanged.
- Publish only after a PR merge, create an immutable tag from the merged SHA,
  build from the clean tag, and verify PyPI plus the Homebrew and curl install routes.

## Limits and risks

Detection is heuristic and general across printed blanks, not specific to the
sample form. It does not OCR scans or recognize arbitrary vector-only blanks.
Incorrect suggestions can be removed. LibreOffice may change Word layout; final
review remains required. Native Word signatures cannot survive conversion.
Release credentials are owner-held; unavailable credentials do not authorize an
alternate identity or bypass of publication guards.
