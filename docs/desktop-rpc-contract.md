# `CACSign.app` Desktop RPC & Module Contract

Shared Phase 0 interface contract between:
- **Worker A (Python):** `cac_sign_desktop.py`, `cac_sign.py`, `cac_sign_launch.py`, `cac-sign.zsh`, `tests/test_desktop_worker.py`
- **Worker B (Swift Services):** `macos/DesktopBridge.swift`, `macos/FileIntake.swift`, `macos/TokenMonitor.swift`, `macos/PatientClipboard.swift`, `macos/AdvisoryFillController.swift`, `macos/ServicesProvider.swift`, `macos/MakeIcon.swift`, `tests/native_macos_tests.swift`
- **Worker C (SwiftUI/AppKit UI):** `macos/WorkspaceViewModel.swift`, `macos/PDFPageCanvasView.swift`, `macos/MainWindowView.swift`, `macos/PostSignActionBar.swift`, `macos/ProfileBuilderSheet.swift`, `macos/SettingsView.swift`, `macos/CACSignApp.swift`

## 1. Process & Transport Rules

1. **Executable invocation:**
   - One-shot mode: `python3 -I -B cac_sign_desktop.py` reads one JSON request from `stdin`, writes one JSON envelope to `stdout`, exits `0`.
   - Persistent session mode: `python3 -I -B cac_sign_desktop.py --serve` reads 1 JSON line per request (`{"id": 1, "op": "...", ...}`) from `stdin` and flushes 1 JSON line per response (`{"id": 1, "ok": true, "data": ...}` or `{"id": 1, "ok": false, "error": "..."}`) on `stdout`. Maintains a private `0700` staging directory (`tempfile.mkdtemp(prefix="cac-sign-desktop-")`) and cleans it up on `EOF`/exit.
2. **Environment isolation (`DesktopBridge.swift`):**
   - Spawn with explicit clean environment ONLY:
     - `HOME`: current user home path
     - `PATH`: `/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin`
     - `LANG`: `en_US.UTF-8`
     - `PYTHONNOUSERSITE`: `1`
     - `PYTHONDONTWRITEBYTECODE`: `1`
     - Optional test override: `CAC_SIGN_APP_SUPPORT` (only if set in parent process for isolated tests)
   - **Never** forward `HTTP_PROXY`, `HTTPS_PROXY`, `ALL_PROXY`, `SSL_CERT_FILE`, or shell secrets.

## 2. Operations (`op`)

### `inspect`
- Request: `{"op": "inspect", "paths": ["/abs/a.pdf", "/abs/b.docx"]}`
- Response `data`: `{"documents": [InspectedDocument]}`
  - Converts `.docx` via `document_inputs.pdf_input` first.
  - Each `InspectedDocument`:
    ```json
    {
      "source_path": "/abs/a.pdf",
      "pdf_path": "/abs/a.pdf",
      "file": "a.pdf",
      "conversion_note": null,
      "sha256": "...",
      "pages": [{"w": 612.0, "h": 792.0}],
      "profile": {"id": "rts-course-request", "description": "..."},
      "reason": "Supervisor approval",
      "next_step": "...",
      "fields": [ /* FormFieldModel — note radio uses "options": [String], choice uses "choice_options": [{"value","label"}] */ ],
      "sign_target": "Signature",
      "suggested_signature": {"page": 0, "box": [0.1, 0.8, 0.45, 0.86], "label": "Provider Signature"},
      "blocked": null,
      "notes": [],
      "warnings": [],
      "profile_fields": ["Date"],
      "autocomplete": [{"key": "full_name", "value": "..."}],
      "signed_before": [{"when": "25 Nov 2026 09:00", "name": "a_signed.pdf"}]
    }
    ```
  - **Important normalization for `FormFieldModel`:** For `kind == "radio"`, emit `"options": ["Opt1", "Opt2"]`; for `kind == "choice"`, emit `"choice_options": [{"value": "...", "label": "..."}]` so Swift `Codable` decodes both cleanly without polymorphic array ambiguity.

### `fill_copy`
- Request: `{"op": "fill_copy", "pdf_path": "/abs/a.pdf", "values": {"Name": "Val"}, "additions": [{"name": "AddedText1", "kind": "text", "label": "Name", "page": 0, "box": [0.1, 0.2, 0.4, 0.24]}]}`
- Response `data`: `{"saved": true, "output": "/abs/a_filled.pdf", "name": "a_filled.pdf", "text": "..."}` (or `{"saved": false, "text": "reason"}`).

### `prepare`
- Request: `{"op": "prepare", "pdf_path": "/abs/a.pdf", "values": {...}, "additions": [...], "mode": "field" | "place", "field": "Signature", "page": 0, "box": [0.1, 0.8, 0.4, 0.86], "opensc": false}`
- Response `data`:
  ```json
  {
    "ready": true,
    "job_id": "hex16",
    "stage_path": "/private/.../stage-xxx.pdf",
    "pages": 1,
    "where": {"page": 0, "box": [0.1, 0.8, 0.4, 0.86]},
    "changes": [{"name": "Date", "from": "(blank)", "to": "25 NOV 2026", "flags": []}],
    "warnings": [],
    "signed_before": [],
    "needs_ack": false,
    "text": null
  }
  ```

### `commit`
- Request: `{"op": "commit", "job_id": "hex16", "ack": true, "reason": "Supervisor approval", "opensc": false, "pin": null, "p12_path": null, "p12_pass": null}`
- Response `data`:
  ```json
  {
    "signed": true,
    "cancelled": false,
    "card_error": false,
    "needs_ack": false,
    "text": "DONE ...",
    "output": "/abs/a_signed.pdf",
    "name": "a_signed.pdf",
    "sha256": "...",
    "signatures": [{"field": "Signature", "signer": "DOE.JANE", "when": "25 Nov 2026 09:05", "ok": true, "detail": ""}],
    "next_step": "..."
  }
  ```

### `discard`
- Request: `{"op": "discard", "job_id": "hex16"}` (if `job_id` omitted, discards all pending staged snapshots).
- Response `data`: `{"discarded": true}`

### `sign_quick`
- Request: `{"op": "sign_quick", "paths": ["/abs/a.pdf"], "dry_run": true, "opensc": false, "pin": null, "p12_path": null, "p12_pass": null}`
- Response `data`:
  `{"items": [{"path": "...", "file": "a.pdf", "can_sign": true, "summary": "...", "warnings": [], "signed": false, "output": null, "error": null}], "stopped_early": false}`

### `verify`
- Request: `{"op": "verify", "path": "/abs/a_signed.pdf"}`
- Response `data`: `{"file": "a_signed.pdf", "reader_problems": [], "warnings": [], "signatures": [SignatureSummary]}`

### `personas_get` / `personas_save` / `identity_autosave`
- `personas_get`: returns `{"active_id": "default", "personas": [{"id": "default", "name": "Primary Signer", "fields": {...}}], "autocomplete": [...]}`
- `personas_save`: `{"op": "personas_save", "active_id": "...", "personas": [...]}` -> writes `0600` `personas.json` AND syncs active persona's `fields` to `0600` `identity.json`. Returns same shape as `personas_get`.
- `identity_autosave`: `{"op": "identity_autosave", "updates": {"npi": "..."}}` -> updates active persona + `identity.json` (`0600`). Returns same shape as `personas_get`.

### `profiles_list` / `profile_save` / `profile_delete`
- `profiles_list`: returns `{"profiles": [FormProfileSpec]}` (`fill` normalized to `{"FieldName": {"value": "...", "if_empty": true|false}}`).
- `profile_save`: `{"op": "profile_save", "profile": FormProfileSpec}` -> writes `0600` `~/Library/Application Support/cac-sign/profiles/<id>.json` and returns `{"profiles": [...]}`.
- `profile_delete`: `{"op": "profile_delete", "id": "..."}` -> deletes user profile only (refuses bundled) and returns `{"profiles": [...]}`.

### `ledger_list`, `render_page` & `diagnostics`
- `ledger_list`: `{"op": "ledger_list", "limit": 100}` -> `{"entries": [LedgerRecord]}`.
- `render_page`: `{"op": "render_page", "pdf_path": "/abs/a.pdf", "page": 0, "scale": 2.0}` -> `{"png_base64": "..."}` (in-memory `pikepdf.flatten_annotations("screen")` + Quartz render so all `/Annots`, filled appearances, and CAC stamps render identically to review).
- `diagnostics`: `{"op": "diagnostics"}` -> `SystemDiagnostics`.
