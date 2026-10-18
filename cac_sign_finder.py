#!/usr/bin/env python3
"""
cac-sign without a terminal: the Finder "CAC Sign" Quick Action (and later a mouse
button) runs this. Same tool and checks as `cac-sign`, with dialogs for I/O.

Usage:
  cac_sign_finder.py FILE.pdf [MORE.pdf ...]   # what the Quick Action runs
  cac_sign_finder.py                           # no files: the PDFs selected in Finder

Flow:
  1. A dry run produces the plan.
  2. A dialog shows the plan and asks "Sign?". It has no password field.
  3. cac_sign.main() signs and verifies, unchanged. macOS asks for the PIN in its
     own smart-card window, and this program never sees it.
  4. A dialog shows the result; "Show in Finder" reveals the output.

Cancel at either prompt and nothing is signed. A refused or cancelled PIN stops
the batch instead of prompting again, because the CAC locks after three wrong
PINs. Dialog text has already been through printable(), and it reaches AppleScript
as an argument, never as script source, so a crafted PDF cannot inject AppleScript.
"""
from __future__ import annotations
import contextlib, io, os, subprocess, sys, time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import cac_sign  # noqa: E402
from cac_signing import printable  # noqa: E402

TITLE = "Sign with CAC"
ICON = "/System/Applications/Preview.app/Contents/Resources/AppIcon.icns"  # reads as "PDF", not "security"


def _icon(fallback):
    return f'icon (POSIX file "{ICON}")' if os.path.exists(ICON) else f"icon {fallback}"


class Cancelled(Exception):
    pass


def _osascript(lines, *args):
    cmd = ["osascript"]
    for line in lines:
        cmd += ["-e", line]
    r = subprocess.run(cmd + list(args), capture_output=True, text=True)
    if r.returncode != 0:
        if "-128" in r.stderr:  # user pressed Cancel
            raise Cancelled()
        raise RuntimeError(f"osascript failed: {r.stderr.strip()}")
    return r.stdout.rstrip("\n")


def confirm(text):
    """Show the plan with Cancel / Sign. No password field: macOS asks for the PIN itself."""
    _osascript([
        "on run argv",
        "activate",
        f'display dialog (item 1 of argv) with title "{TITLE}" buttons {{"Cancel", "Sign"}} '
        f'default button "Sign" cancel button "Cancel" with {_icon("note")}',
        "end run"], text)


def show(text, ok, reveal=()):
    """Result dialog. "Show in Finder" selects the output files."""
    buttons = '{"Show in Finder", "OK"}' if reveal else '{"OK"}'
    try:
        pressed = _osascript([
            "on run argv",
            "activate",
            f'set r to display dialog (item 1 of argv) with title "{TITLE}" buttons {buttons} '
            f'default button "OK" with {_icon("note") if ok else "icon caution"}',
            "return button returned of r",
            "end run"], text)
    except Cancelled:
        return
    if reveal and pressed == "Show in Finder":
        subprocess.run(["open", "-R", *reveal])


def notify(text):
    """Last resort when a dialog cannot be shown."""
    subprocess.run(["osascript", "-e", "on run argv", "-e",
                    f'display notification (item 1 of argv) with title "{TITLE}"', "-e", "end run", text])


def finder_selection():
    out = _osascript(['tell application "Finder" to set sel to selection as alias list',
                      'set out to ""',
                      'repeat with a in sel',
                      'set out to out & POSIX path of a & linefeed',
                      'end repeat',
                      'return out'])
    return [p for p in out.splitlines() if p]


def run_cli(argv):
    """cac_sign.main() with its output captured. Returns (exit code, text)."""
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf), contextlib.redirect_stderr(buf):
        try:
            rc = cac_sign.main(argv)
        except SystemExit as e:
            rc = e.code if isinstance(e.code, int) else 1
            if e.code and not isinstance(e.code, int):
                print(printable(e.code))
        except Exception as e:
            rc = 1
            print(printable(f"{type(e).__name__}: {e}"))
    return rc, buf.getvalue().strip()


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    pdfs = [a for a in argv if a.lower().endswith(".pdf") and os.path.isfile(a)]
    if not pdfs:
        pdfs = [p for p in finder_selection() if p.lower().endswith(".pdf")]
        if not pdfs:
            show("Select one or more PDFs in Finder first.", ok=False)
            return 1
        argv += pdfs

    rc, plan = run_cli(argv + ["--dry-run"])
    if rc != 0:  # nothing to sign: the plan says why
        show(plan, ok=False)
        return 1
    try:
        confirm(plan + "\n\nClick Sign, and macOS will ask for your CAC PIN.")
    except Cancelled:
        return 1

    start = time.time() - 1
    rc, out = run_cli(argv)
    # main() repeats the plan, then a blank line, then DONE/FAIL/STOP lines: show those
    head, sep, tail = out.partition("\n\n")
    result = tail if sep and any(w in tail for w in ("DONE", "FAIL", "STOP")) else out
    made = []
    for p in pdfs:
        stem = os.path.splitext(p)[0]
        for cand in (f"{stem}_signed.pdf", f"{stem}_signed.FAILED.pdf"):
            if os.path.exists(cand) and os.path.getmtime(cand) >= start:
                made.append(cand)
    show(result, ok=rc == 0, reveal=made)
    return rc


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as e:  # e.g. no dialog possible from this context: say so somewhere visible
        notify(printable(f"{type(e).__name__}: {e}"))
        sys.exit(1)
