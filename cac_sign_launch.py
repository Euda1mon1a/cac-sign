"""
The `cac-sign` command that pip, uv and Homebrew install. In the wheel this file is the
package `cac_sign_app`, with the tool's modules and data beside it, unchanged.

  cac-sign FORM.pdf ...             the CLI (cac_sign.py); see cac-sign --help
  cac-sign --setup                  add the Finder Quick Actions and your details for forms
  cac-sign --setup --remove         take the Quick Actions out again
  cac-sign --quick-action FILES     what the "CAC Sign" Quick Action runs (cac_sign_finder.py)
  cac-sign --review FILES           what the "CAC Sign..." Quick Action runs (cac_sign_editor.py)

The curl installer doesn't use this: it runs the scripts from ~/.cac-sign directly.
"""
from __future__ import annotations
import json, os, plistlib, shlex, shutil, subprocess, sys

HERE = os.path.dirname(os.path.abspath(__file__))
SERVICES = "~/Library/Services"
# Each Quick Action this sets up names the script it runs, in a comment, so the curl
# installer and `--setup --remove` recognise it as ours (install.sh: is_our_workflow).
ACTIONS = {"CAC Sign.workflow": ("--quick-action", "cac_sign_finder.py"),
           "CAC Sign….workflow": ("--review --background", "cac_sign_editor.py")}


def _modules():
    if HERE not in sys.path:
        sys.path.insert(0, HERE)  # the tool's modules import each other by their plain names


def cli(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    _modules()
    if argv[:1] == ["--setup"]:
        return setup(argv[1:])
    if argv[:1] == ["--quick-action"]:
        import cac_sign_finder
        from cac_signing import printable
        try:
            return cac_sign_finder.main(argv[1:])
        except Exception as e:  # no terminal behind a Quick Action: say so somewhere visible
            cac_sign_finder.notify(printable(f"{type(e).__name__}: {e}"))
            return 1
    if argv[:1] == ["--review"]:
        import cac_sign_editor
        return cac_sign_editor.main(argv[1:])
    import cac_sign
    return cac_sign.main(argv)


def _command():
    """The installed `cac-sign`, by a path that survives upgrades (Homebrew's bin link, uv's ~/.local/bin)."""
    found = shutil.which("cac-sign")
    return found if found and os.path.realpath(found) == os.path.realpath(sys.argv[0]) else os.path.abspath(sys.argv[0])


def _ours(workflow):
    try:
        with open(os.path.join(workflow, "Contents", "document.wflow"), encoding="utf-8") as f:
            text = f.read()
    except OSError:
        return False
    return "cac_sign_finder.py" in text or "cac_sign_editor.py" in text


def setup(argv, refresh=True):
    """Install (or with --remove, take out) the Finder Quick Actions, then ask for your details once."""
    services = os.path.expanduser(SERVICES)
    remove = "--remove" in argv
    for name in ACTIONS:
        dest = os.path.join(services, name)
        if os.path.exists(dest) and not _ours(dest):
            print(f"{dest} exists and is not cac-sign's; rename it, then run this again.", file=sys.stderr)
            return 1
    for name, (args, script) in ACTIONS.items():
        dest = os.path.join(services, name)
        if os.path.exists(dest):
            shutil.rmtree(dest)
        if remove:
            continue
        shutil.copytree(os.path.join(HERE, "quick-action", name), dest)
        doc = os.path.join(dest, "Contents", "document.wflow")
        with open(doc, "rb") as f:
            wf = plistlib.load(f)
        wf["actions"][0]["action"]["ActionParameters"]["COMMAND_STRING"] = (
            f"# cac-sign Quick Action ({script}), set up by `cac-sign --setup`.\n"
            f'exec {shlex.quote(_command())} {args} "$@"\n')
        with open(doc, "wb") as f:
            plistlib.dump(wf, f)
    if refresh and os.path.exists("/System/Library/CoreServices/pbs"):
        subprocess.run(["/System/Library/CoreServices/pbs", "-update"], capture_output=True)
    if remove:
        print("Removed the Finder Quick Actions. Your details and signing log are kept in "
              "~/Library/Application Support/cac-sign.")
        return 0
    print('Added the Finder Quick Actions "CAC Sign" and "CAC Sign…": right-click a PDF or DOCX > Quick Actions.')
    _identity()
    return 0


def _identity():
    import cac_sign
    path = cac_sign.IDENTITY_FILE
    if os.path.exists(path):
        print(f"Your details for forms: {path}")
        return
    if not sys.stdin.isatty():
        print(f"To pre-fill your name on forms, copy {os.path.join(HERE, 'identity.example.json')} to {path}.")
        return
    print("Forms you sign often can be pre-filled with your details. Press Enter to skip any.")
    values = {"full_name": input("  Name as it goes on forms (e.g. Jane Q. Doe): ").strip(),
              "title": input("  Title (e.g. Program Director): ").strip(),
              "duty_phone": input("  Duty phone: ").strip()}
    values = {k: v for k, v in values.items() if v}
    if not values:
        print(f"Skipped. You can add them later in {path} (see identity.example.json).")
        return
    os.makedirs(os.path.dirname(path), mode=0o700, exist_ok=True)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        json.dump(values, f, indent=2)
        f.write("\n")
    print(f"Saved to {path} (only you can read it).")


if __name__ == "__main__":
    sys.exit(cli())
