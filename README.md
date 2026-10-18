# cac-sign

Sign PDF forms with your CAC on a Mac, without Adobe and without AVHE. Fill the
form, sign it, and get a file whose signatures, including the ones already on it,
are checked before the tool calls it done.

It exists because Acrobat inside AVHE, saving to the Mac through Citrix drive
mapping, produces signatures whose document hash does not match the file
("General failure"). The card was never the problem. cac-sign signs locally, with
macOS's own smart-card support, and appends every change as a new revision, so an
applicant's signature stays valid when you counter-sign.

This is a personal project. It is not an official DoD or Army tool.

## Install

You need an Apple silicon Mac (M1 or later), a CAC reader, and the DoD
certificates in your keychain (see [Requirements](#requirements)). Then, in Terminal:

```
curl -fsSL https://raw.githubusercontent.com/Euda1mon1a/cac-sign/v0.1.0/install.sh | bash
```

That installs release v0.1.0 in `~/.cac-sign`, adds two Finder Quick Actions and a
`cac-sign` command, and asks for the name, title and duty phone you want pre-filled
on forms (press Enter to skip). To update, run the install line of a newer release.
To remove it:

```
curl -fsSL https://raw.githubusercontent.com/Euda1mon1a/cac-sign/v0.1.0/install.sh | bash -s -- --uninstall
```

Your details and signing log stay in `~/Library/Application Support/cac-sign`
through updates and uninstalls. Delete that folder if you want them gone.

## Sign a form from Finder

Right-click a PDF, then **Quick Actions > CAC Sign…** (with the dots). If it isn't
there, look under **Services**. The form opens in your browser with every field
outlined and listed:

| Color / badge | Means |
|---|---|
| Green, *Filled* | Already in the PDF. |
| Blue, *From your profile* | Suggested by the form's profile (your phone, today's date). Change or clear it. |
| Purple, *Edited* | You changed it. **Undo** puts back what the PDF had. |
| Grey, *Blank* | Empty. **N/A** fills it with "N/A". |
| Amber, *Marked required* | Empty, and the PDF flags it as required. Only a warning. |
| Grey, *Locked* | Read-only, or locked by an existing signature. |

Pick the signature box on the right or click it on the page. If the form has none,
use **Draw a signature box**. **Sign** opens **Review exactly what you'll sign**:
the file with your edits written in, drawn the way any PDF viewer shows it, and a
list of every change. Changes that look unintended are flagged in amber (they
replace something already there, sit in an earlier signer's part of the form, or
aren't fields the profile normally fills), and **Sign this** stays disabled until
you tick **I meant to make the flagged changes**.

**Sign this** hands off to macOS, which asks for your PIN in its own smart-card
window. The signed file is then drawn again and compared with what you reviewed. If
anything visible differs outside the signature box, it is kept as
`..._signed.FAILED.pdf` and you are told not to send it. Nothing is ever
overwritten; the output is `..._signed.pdf` next to the original.

**Quick Actions > CAC Sign** (no dots) skips the review page: a dialog shows the
plan, **Sign** hands off to macOS for the PIN, and a last dialog shows the result.

The first time, macOS may ask whether the Quick Action may use your Downloads
folder. Choose Allow.

## Sign from Terminal

```
cac-sign ~/Downloads/"Doe_BLS Renewal.pdf"
```

It recognises the form, shows what it will fill and sign, asks macOS for the PIN,
and checks the result:

```
Doe_BLS Renewal.pdf  [RTS course request (BLS/ALS/PALS), supervisor block]
    already signed: Applicant Signature by DOE.JANE.Q.1234567890, 13 Oct 2026 15:29 (valid)
    kept Head Nurse or  OIC Print Full Name = "John Smith"
    will fill:  Yes = x;  Yes_2 = x;  Supervisor Duty Phone = ...;  Date = 14 OCT 2026
    will sign:  Signature
DONE  Doe_BLS Renewal_signed.pdf  -- signed by SMITH.JOHN.A.0987654321, all 2 signature(s) verified intact
```

| Command | Does |
|---------|------|
| `cac-sign form.pdf --list` | Lists fields, values, signatures and page sizes. Changes nothing. |
| `cac-sign form.pdf --set "Date=14 OCT 2026" --set "Approve=yes"` | Fills fields, then signs the only empty signature field. |
| `cac-sign form.pdf --field "Supervisor Signature"` | Picks a signature field when there are several. |
| `cac-sign memo.pdf --place 1:400,60,580,95` | No signature field: adds one (page:x1,y1,x2,y2 in points from the bottom-left), then signs. |
| `cac-sign form.pdf --dry-run` | Shows the plan and stops. |
| `cac-sign form.pdf --no-sign --set ...` | Fills only, writing `..._filled.pdf`. |
| `cac-sign file.pdf --verify` | Checks every signature: intact, signer trusted on this Mac, later changes only form filling. Exits 1 if one fails. |
| `cac-sign --list-certs` | Lists the certificates on the inserted CAC. |
| `cac-sign --history` | Lists what you've signed with this tool, newest first. |

Checkboxes take `yes`/`no`, radio buttons the option name, text boxes any text.
Several files at once work too (`cac-sign ~/Downloads/*Renewal.pdf`). The CAC's
signing key needs the PIN for every signature, so macOS asks once per file;
`--opensc` instead takes one PIN in Terminal for the whole batch (needs
`brew install opensc`). A cancelled or refused PIN stops the batch rather than
asking again, because the card locks after three wrong PINs.

## Profiles: forms you sign often

A profile in `profiles/` says how to recognise a form by its field names, what to
fill, and which field to sign. `profiles/rts-course-request.json` is the example.
`${today}` becomes today's date (`14 OCT 2026`); `${full_name}`, `${title}` and
`${duty_phone}` come from `~/Library/Application Support/cac-sign/identity.json`,
which the installer creates (template: `identity.example.json`).
`"if_empty": true` keeps whatever the applicant already typed.

To add a form, run `cac-sign form.pdf --list`, copy the example profile, and change
`match_fields`, `fill` and `sign_field`. Profiles you add to `~/.cac-sign/profiles/`
are kept when you update.

## What it refuses, and why

- **An existing signature does not check out**: it is broken, its certificate does
  not chain to a root this Mac trusts (a self-made certificate can carry anyone's
  name), or the form was changed after signing beyond filling in boxes.
- **PDF readers disagree on the file**: it needed repair, it is an XFA (LiveCycle)
  form, or qpdf, pyHanko and Apple's PDF reader read or draw it differently. What
  you check might not be what Acrobat shows. Acrobat itself can't be run for this
  check, so a file crafted to fool only Acrobat could still pass; if a form from an
  unknown sender matters, open your signed copy in Acrobat too. That goes double for
  PDFs with layers: the review draws them with Apple's renderer alone, and some layer
  settings are left to each viewer, so a layered PDF always asks you to check it in
  Acrobat first.
- **Something is hidden on screen but prints**: an annotation flagged that way,
  layers switched off, or a box shown only on screen that covers printed text. You
  would be signing content you never saw.
- **The applicant has not signed yet**, when the profile requires it.
- **The document is certified "no changes allowed"**, or the field is locked by a
  signature.
- **The field is read-only**, or **the output already exists.**

In Terminal, `--force` overrides the first three (after you have checked the file in
Acrobat) and the last two. The applicant check, locked fields and certified
documents can never be forced. The Finder actions never force anything.

Some files are signable but may look different elsewhere: Acrobat scripts that can
reformat values, a form that asks viewers to redraw its fields, or page areas outside
what viewers show. The plan says so in a `WARNING:` line, and the review page won't
sign until you tick that you've read the warning.

After writing, it checks that every signature is intact, that the new signature
covers the whole file, and that the new revision touched only the fields it filled
and signed. If any check fails, the file is kept as `..._signed.FAILED.pdf` and the
exit code is 1.

## Security

- **Your PIN never reaches this tool.** macOS's built-in smart-card support signs
  with the card and shows its own PIN window. (`--opensc` is the exception: you
  type the PIN into Terminal and it is passed to OpenSC.)
- **The review page** runs on `127.0.0.1` only, makes no network requests, needs a
  one-time key that only the opened tab has, rejects requests whose Host header is
  not its own, and stops 10 minutes after the tab closes. It signs the exact bytes
  you reviewed, from a private copy, even if the file on disk changes afterwards.
- **Text from a PDF** (field values, signer names) is escaped before it reaches the
  terminal, a dialog or the page, so a crafted form cannot fake a result.
- **The signing log** (`ledger.jsonl`) is readable only by you and holds times,
  SHA-256 fingerprints, the output path, signer and field, never form content.
- **What gets installed is pinned.** The install line names a release tag; Python
  packages are pinned with hashes in `uv.lock` and installed exactly; and if the
  installer has to fetch uv, it checks the download against a pinned SHA-256.

Found a problem? Open an issue. For anything exploitable, report it privately
through the repository's **Security** tab instead.

## Requirements

- **An Apple silicon Mac** on a recent macOS. One pinned dependency
  (`cryptography`) ships macOS builds for Apple silicon only.
- **A CAC reader**, with the card readable by macOS (it shows up in Keychain
  Access, and CAC websites work in Safari).
- **The DoD root and intermediate certificates in your keychain.** If CAC websites
  open without certificate warnings, you already have them. They let cac-sign tell
  a real CAC signature from a self-made one, and embed your issuing CA so anyone
  can check your signature offline. Without them it refuses to counter-sign a form
  that someone else has signed, and `--verify` reports the signer as untrusted.
- Nothing else: the installer brings its own Python through
  [uv](https://docs.astral.sh/uv/).

## Development

```
git clone https://github.com/Euda1mon1a/cac-sign && cd cac-sign
./install.sh                          # links ~/.cac-sign to this clone
uv run --with pytest pytest -q tests  # synthetic forms and throwaway certificates; no CAC needed
```

`CLAUDE.md` holds the design notes: why each check exists, and what earlier
security reviews changed.

## License

MIT
