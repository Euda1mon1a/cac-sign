#!/usr/bin/env python3
"""Build a self-contained, locally signed CACSign.app bundle.

Bundles the standalone Python 3.12 runtime + pinned packages into Contents/Resources/python,
copies the cac-sign worker modules + bundled profiles into Contents/Resources/app,
compiles the native SwiftUI/AppKit macOS application into Contents/MacOS/CACSign,
generates CACSign.icns programmatically, writes bundle-receipt.json, and verifies
strict inside-out ad-hoc code signing.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import plistlib
import re
import shutil
import stat
import subprocess
import tempfile

ROOT = Path(__file__).resolve().parents[1]
BUNDLE_ID = "com.aaronmontgomery.cacsign"
APP_MODULES = (
    "cac_sign.py",
    "cac_signing.py",
    "cac_sign_desktop.py",
    "cac_sign_editor.py",
    "cac_sign_finder.py",
    "cac_sign_launch.py",
    "pdf_readers.py",
    "document_inputs.py",
    "field_detection.py",
    "identity.example.json",
)
SKIP_PACKAGES = ("pytest", "_pytest", "pluggy", "iniconfig", "pip", "setuptools", "wheel", "pyobjctest")


def run(*args, **kwargs):
    return subprocess.run([str(a) for a in args], check=True, **kwargs)


def macho_kind(file_path: Path) -> str | None:
    """Return 'exec' for MH_EXECUTE (2), 'lib' for MH_DYLIB/MH_BUNDLE (6, 8), or None."""
    if not file_path.is_file() or file_path.is_symlink():
        return None
    try:
        with file_path.open("rb") as f:
            hdr = f.read(16)
    except OSError:
        return None
    if len(hdr) < 16:
        return None
    magic = hdr[:4]
    if magic == b"\xcf\xfa\xed\xfe":
        filetype = int.from_bytes(hdr[12:16], "little")
        return "exec" if filetype == 2 else ("lib" if filetype in (6, 8) else None)
    if magic == b"\xfe\xed\xfa\xcf":
        filetype = int.from_bytes(hdr[12:16], "big")
        return "exec" if filetype == 2 else ("lib" if filetype in (6, 8) else None)
    if magic in (b"\xca\xfe\xba\xbe", b"\xbe\xba\xfe\xca"):
        return "exec" if file_path.parent.name == "bin" else "lib"
    return None


def verify_macos26_symbol_compat(binary_path: Path) -> None:
    nm_out = subprocess.check_output(["xcrun", "nm", "-u", str(binary_path)], text=True)
    if re.search(r"LanguageModelError|GenerationOptionsV12samplingMode", nm_out):
        raise RuntimeError(
            "CACSign binary imports macOS 27-only FoundationModels symbols "
            "(LanguageModelError or GenerationOptions.samplingMode) that fail on macOS 26.x."
        )


def read_project_version() -> str:
    pyproject = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
    m = re.search(r'^version\s*=\s*"([^"]+)"', pyproject, re.M)
    return m.group(1) if m else "0.2.0"


def default_python() -> Path:
    candidates = [
        ROOT / ".venv/bin/python",
        Path.home() / "workspace/cac-sign/.venv/bin/python",
        Path.home() / ".cac-sign/.venv/bin/python",
    ]
    for c in candidates:
        if c.is_file():
            return c.absolute()
    return candidates[0]


def build_icon(staging: Path, dest_icns: Path) -> None:
    icon_png = staging / "icon_1024.png"
    iconset = staging / "CACSign.iconset"
    iconset.mkdir(parents=True, exist_ok=True)
    run("xcrun", "swift", ROOT / "macos/MakeIcon.swift", icon_png)
    for sz in (16, 32, 128, 256, 512):
        run("sips", "-z", sz, sz, icon_png, "--out", iconset / f"icon_{sz}x{sz}.png", stdout=subprocess.DEVNULL)
        sz2 = sz * 2
        run("sips", "-z", sz2, sz2, icon_png, "--out", iconset / f"icon_{sz}x{sz}@2x.png", stdout=subprocess.DEVNULL)
    run("iconutil", "-c", "icns", iconset, "-o", dest_icns)


def make_info_plist(version: str) -> dict:
    return {
        "CFBundleIdentifier": BUNDLE_ID,
        "CFBundleName": "CACSign",
        "CFBundleDisplayName": "CACSign",
        "CFBundleExecutable": "CACSign",
        "CFBundlePackageType": "APPL",
        "CFBundleShortVersionString": version,
        "CFBundleVersion": version,
        "CFBundleIconFile": "CACSign.icns",
        "LSMinimumSystemVersion": "14.0",
        "NSHighResolutionCapable": True,
        "NSPrincipalClass": "NSApplication",
        "LSApplicationCategoryType": "public.app-category.productivity",
        "CFBundleDocumentTypes": [
            {
                "CFBundleTypeName": "PDF Document",
                "CFBundleTypeRole": "Editor",
                "LSHandlerRank": "Alternate",
                "LSItemContentTypes": ["com.adobe.pdf"],
            },
            {
                "CFBundleTypeName": "Word Document",
                "CFBundleTypeRole": "Viewer",
                "LSHandlerRank": "Alternate",
                "LSItemContentTypes": ["org.openxmlformats.wordprocessingml.document"],
            },
        ],
        "NSServices": [
            {
                "NSMenuItem": {"default": "CAC Sign…"},
                "NSMessage": "reviewWithCACSign",
                "NSPortName": "CACSign",
                "NSSendFileTypes": ["com.adobe.pdf", "org.openxmlformats.wordprocessingml.document"],
                "NSSendTypes": ["NSFilenamesPboardType", "public.file-url"],
            },
            {
                "NSMenuItem": {"default": "CAC Sign"},
                "NSMessage": "quickSignWithCAC",
                "NSPortName": "CACSign",
                "NSSendFileTypes": ["com.adobe.pdf", "org.openxmlformats.wordprocessingml.document"],
                "NSSendTypes": ["NSFilenamesPboardType", "public.file-url"],
            },
        ],
    }


def should_skip_site_entry(name: str) -> bool:
    if name == "__pycache__" or name.endswith((".pth", ".dist-info")):
        return name == "__pycache__" or name.endswith(".pth")
    if name.startswith(("_virtualenv", "__editable__")):
        return True
    lower = name.lower()
    return any(lower == p or lower.startswith(p + "-") or lower.startswith(p + "_") for p in SKIP_PACKAGES)


def copy_pruned_python_runtime(base: Path, runtime: Path, py_ver: str) -> None:
    runtime_bin = runtime / "bin"
    runtime_bin.mkdir(parents=True, exist_ok=True)
    src_py = base / f"bin/python{py_ver}"
    if not src_py.is_file():
        src_py = base / "bin/python3"
    dest_py = runtime_bin / f"python{py_ver}"
    shutil.copyfile(src_py, dest_py)
    dest_py.chmod(0o755)
    os.symlink(f"python{py_ver}", runtime_bin / "python3")
    os.symlink(f"python{py_ver}", runtime_bin / "python")

    if (base / "BUILD").is_file():
        shutil.copyfile(base / "BUILD", runtime / "BUILD")

    stdlib_ignore = shutil.ignore_patterns(
        "__pycache__",
        "*.pyc",
        ".DS_Store",
        "*.dSYM",
        "test",
        "tests",
        "idlelib",
        "tkinter",
        "turtledemo",
        "ensurepip",
        "venv",
        "lib2to3",
        "*.a",
        "libtcl*",
        "libtk*",
        "tcl*",
        "tk*",
        "itcl*",
        "thread*",
        "_tkinter*",
    )
    shutil.copytree(base / "lib", runtime / "lib", symlinks=False, ignore=stdlib_ignore)


def sign_app_bundle(app: Path, resources_dir: Path, staging: Path, sign_identity: str, use_timestamp: bool) -> None:
    if sign_identity == "-":
        for macho_file in sorted(
            (p for p in resources_dir.rglob("*") if macho_kind(p) is not None),
            key=lambda p: len(p.parts),
            reverse=True,
        ):
            run("codesign", "--force", "--sign", "-", macho_file, stderr=subprocess.DEVNULL)
        run("codesign", "--force", "--deep", "--sign", "-", app)
        run("codesign", "--verify", "--strict", "--deep", app)
        return

    py_entitlements = staging / "python-entitlements.plist"
    py_entitlements.write_bytes(
        plistlib.dumps(
            {
                "com.apple.security.cs.allow-unsigned-executable-memory": True,
                "com.apple.security.cs.disable-library-validation": True,
            }
        )
    )
    ts_args = ["--timestamp"] if use_timestamp else ["--timestamp=none"]

    nested = [(p, macho_kind(p)) for p in resources_dir.rglob("*")]
    nested_machos = [(p, k) for p, k in nested if k is not None]
    for path_item, kind in sorted(nested_machos, key=lambda pk: (pk[1] == "exec", -len(pk[0].parts))):
        cmd = ["codesign", "--force", *ts_args, "--options", "runtime"]
        if kind == "exec":
            cmd += ["--entitlements", str(py_entitlements)]
        cmd += ["--sign", sign_identity, str(path_item)]
        run(*cmd)

    run("codesign", "--force", *ts_args, "--options", "runtime", "--sign", sign_identity, app)
    run("codesign", "--verify", "--strict", "--deep", app)


def build_app(
    output: Path,
    python_exe: Path,
    replace: bool = False,
    sign_identity: str = "-",
    use_timestamp: bool = True,
    source_commit_override: str | None = None,
    notarization_note: str | None = None,
) -> None:
    if output.suffix != ".app":
        raise ValueError("Output destination must end in .app")
    if (output.exists() or output.is_symlink()) and not replace:
        raise ValueError(f"{output} already exists (pass --replace to stage and replace)")

    probe = json.loads(
        subprocess.check_output(
            [
                str(python_exe),
                "-I",
                "-c",
                "import sys,sysconfig,json,importlib.metadata as m; print(json.dumps(dict("
                "base=sys.base_prefix,ver=f'{sys.version_info.major}.{sys.version_info.minor}',"
                "site=sysconfig.get_path('purelib'),"
                "packages={d.metadata['Name']:d.version for d in m.distributions()})))",
            ],
            text=True,
        )
    )
    base = Path(probe["base"])
    py_ver = probe["ver"]
    if not (base / f"lib/python{py_ver}").is_dir():
        raise ValueError(f"Expected standalone Python installation at {base}")

    output.parent.mkdir(parents=True, exist_ok=True)
    version = read_project_version()

    with tempfile.TemporaryDirectory(prefix="cacsign-build-", dir=output.parent) as temp:
        staging = Path(temp)
        app = staging / "CACSign.app"
        macos_dir = app / "Contents/MacOS"
        resources_dir = app / "Contents/Resources"
        app_code_dir = resources_dir / "app"
        macos_dir.mkdir(parents=True)
        resources_dir.mkdir(parents=True)
        app_code_dir.mkdir(parents=True)

        ignore = shutil.ignore_patterns("__pycache__", "*.pyc", ".DS_Store", "*.dSYM")
        runtime = resources_dir / "python"
        copy_pruned_python_runtime(base, runtime, py_ver)

        site_dest = runtime / f"lib/python{py_ver}/site-packages"
        site_dest.mkdir(parents=True, exist_ok=True)
        for src in Path(probe["site"]).iterdir():
            if should_skip_site_entry(src.name):
                continue
            target = site_dest / src.name
            if src.is_dir():
                shutil.copytree(src, target, dirs_exist_ok=True, symlinks=False, ignore=ignore)
            else:
                shutil.copyfile(src, target)

        stray_dsyms = list(resources_dir.rglob("*.dSYM"))
        if stray_dsyms:
            raise RuntimeError(f"Unsealed .dSYM bundle(s) remained in Resources: {stray_dsyms[:3]}")

        for mod in APP_MODULES:
            shutil.copyfile(ROOT / mod, app_code_dir / mod)
        shutil.copytree(ROOT / "profiles", app_code_dir / "profiles", dirs_exist_ok=True, ignore=ignore)

        bin_dir = resources_dir / "bin"
        bin_dir.mkdir(parents=True, exist_ok=True)
        cli_wrapper = bin_dir / "cac-sign"
        cli_wrapper.write_text(
            "#!/bin/sh\n"
            'DIR="$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)"\n'
            f'exec "$DIR/python/bin/python{py_ver}" -I -B "$DIR/app/cac_sign_launch.py" "$@"\n',
            encoding="utf-8",
        )
        cli_wrapper.chmod(cli_wrapper.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)

        swift_sources = sorted(
            p for p in (ROOT / "macos").glob("*.swift") if p.name != "MakeIcon.swift"
        )
        if not swift_sources:
            raise ValueError("No Swift app sources found in macos/")
        main_bin = macos_dir / "CACSign"
        run(
            "xcrun",
            "swiftc",
            "-parse-as-library",
            "-swift-version",
            "5",
            "-O",
            "-suppress-warnings",
            "-target",
            "arm64-apple-macosx14.0",
            *swift_sources,
            "-o",
            main_bin,
        )
        verify_macos26_symbol_compat(main_bin)

        build_icon(staging, resources_dir / "CACSign.icns")
        (app / "Contents/Info.plist").write_bytes(plistlib.dumps(make_info_plist(version)))

        tracked_sources = (
            [ROOT / m for m in APP_MODULES if (ROOT / m).is_file()]
            + swift_sources
            + [ROOT / "macos/MakeIcon.swift", ROOT / "scripts/build_macos_app.py"]
        )
        if source_commit_override:
            source_commit = source_commit_override
            source_dirty = False
        else:
            source_commit = subprocess.check_output(["git", "-C", str(ROOT), "rev-parse", "HEAD"], text=True).strip()
            source_dirty = bool(subprocess.check_output(["git", "-C", str(ROOT), "status", "--porcelain"], text=True))

        signing_desc = (
            "local ad-hoc; inside-out Mach-O verified"
            if sign_identity == "-"
            else f"Developer ID Hardened Runtime ({sign_identity})"
        )
        if notarization_note:
            signing_desc += f"; {notarization_note}"

        receipt = {
            "bundle_id": BUNDLE_ID,
            "version": version,
            "source_commit": source_commit,
            "source_dirty": source_dirty,
            "source_sha256": {
                str(p.relative_to(ROOT)): hashlib.sha256(p.read_bytes()).hexdigest() for p in tracked_sources
            },
            "python_version": py_ver,
            "packages": probe["packages"],
            "signing": signing_desc,
            "network": "zero network calls; proxy variables stripped at worker boundary",
        }
        (resources_dir / "bundle-receipt.json").write_text(json.dumps(receipt, indent=2) + "\n", encoding="utf-8")
        shutil.copyfile(ROOT / "README.md", resources_dir / "README.md")

        sign_app_bundle(app, resources_dir, staging, sign_identity=sign_identity, use_timestamp=use_timestamp)

        if output.exists() or output.is_symlink():
            old = staging / "old-target.app"
            shutil.move(str(output), str(old))
        shutil.move(str(app), str(output))

    run("codesign", "--verify", "--strict", "--deep", output)
    print(output)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("output", type=Path, help="Destination .app path (e.g. build/CACSign.app)")
    parser.add_argument("--python", type=Path, default=default_python(), help="Source venv Python interpreter")
    parser.add_argument("--replace", action="store_true", help="Atomically replace an existing output .app")
    parser.add_argument("--sign", default="-", help="Code signing identity ('-' for ad-hoc, or Developer ID Application)")
    parser.add_argument("--no-timestamp", action="store_true", help="Omit RFC 3161 timestamp server call during Developer ID signing")
    parser.add_argument("--source-commit", default=None, help="Explicit git commit SHA when building from git archive")
    parser.add_argument("--notarization-note", default=None, help="Optional notarization note recorded in bundle-receipt.json")
    args = parser.parse_args()
    build_app(
        args.output.expanduser().absolute(),
        args.python.expanduser().absolute(),
        replace=args.replace,
        sign_identity=args.sign,
        use_timestamp=not args.no_timestamp,
        source_commit_override=args.source_commit,
        notarization_note=args.notarization_note,
    )


if __name__ == "__main__":
    main()
