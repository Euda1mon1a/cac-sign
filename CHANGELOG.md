# Changelog

## [Unreleased]

## [0.2.0] - 2026-10-27

### Added

- Local DOCX-to-PDF conversion through LibreOffice, available from Terminal and
  both Finder Quick Actions. The Word original and existing PDF copies are preserved.
- Automatic suggestions for text fields, empty checkbox characters and signature
  lines on selectable-text PDFs. Suggestions use nearby labels, remain reviewable
  and removable, and start without personal values or consent selections.
- Manual text placement and **Save filled copy** for forms with printed blanks.
- Packaging and install checks for DOCX intake and field detection.

### Changed

- Installer scenario failures and packaged smoke-test failures now return a failing
  exit code so release validation cannot report success after a failed check.

### Security

- Word documents carrying digital signatures are refused because conversion cannot
  preserve them. The converter uses an isolated profile with macros and active
  embedded content disabled.
- Suggested and manual fields are instantiated only in an unsigned private
  snapshot and go through the existing review, integrity and signing checks.

### Limits

- Detection uses selectable text, not OCR, a language model or a form-specific
  template. Scans, vector-only blanks and rotated pages may need manual handling.
- LibreOffice conversion can change layout. Review the PDF before signing.

## [0.1.0] - 2026-10-18

- Initial packaged release with local CAC signing, PDF form filling, verification,
  a browser review page, Finder Quick Actions and installation through PyPI,
  Homebrew or the release-pinned installer.
