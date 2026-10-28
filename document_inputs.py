"""Local DOCX -> PDF intake. The Word file is never modified or signed."""
from __future__ import annotations

import os
from pathlib import Path
import shutil
import subprocess
import tempfile
from zipfile import BadZipFile, ZipFile


def supported(path):
    return Path(path).suffix.lower() in (".pdf", ".docx")


def convert_docx(path):
    """Export a private snapshot with LibreOffice, then publish a new, private PDF.

    An isolated profile prevents handing the request to an open LibreOffice window.
    Macros and active embedded content are disabled in that profile.
    """
    source = Path(path).absolute()
    office = next((p for p in (
        "/Applications/LibreOffice.app/Contents/MacOS/soffice",
        os.path.expanduser("~/Applications/LibreOffice.app/Contents/MacOS/soffice"),
        shutil.which("soffice"),
    ) if p and os.path.isfile(p) and os.access(p, os.X_OK)), None)
    if not office:
        raise ValueError("DOCX conversion needs LibreOffice. Install LibreOffice, then try again.")
    with tempfile.TemporaryDirectory(prefix="cac-sign-docx-") as temp:
        work = Path(temp)
        snapshot = work / "document.docx"
        snapshot.write_bytes(source.read_bytes())
        try:
            with ZipFile(snapshot) as package:
                if "word/document.xml" not in package.namelist():
                    raise ValueError("This file is not a Word DOCX document.")
                if any(n.lower().startswith("_xmlsignatures/") for n in package.namelist()):
                    raise ValueError("This Word document has digital signatures. PDF conversion cannot preserve them.")
        except BadZipFile as e:
            raise ValueError("This file is not a readable DOCX document.") from e
        profile = work / "profile"
        profile.mkdir()
        (profile / "registrymodifications.xcu").write_text('''<?xml version="1.0"?>
<oor:items xmlns:oor="http://openoffice.org/2001/registry">
<item oor:path="/org.openoffice.Office.Common/Security/Scripting">
<prop oor:name="DisableMacrosExecution" oor:op="fuse"><value>true</value></prop>
<prop oor:name="DisableActiveContent" oor:op="fuse"><value>true</value></prop>
</item></oor:items>''', encoding="utf-8")
        try:
            result = subprocess.run(
                [office, "-env:UserInstallation=" + profile.as_uri(), "--headless", "--nologo",
                 "--nodefault", "--norestore", "--convert-to", "pdf:writer_pdf_Export",
                 "--outdir", temp, str(snapshot)],
                capture_output=True, text=True, timeout=90,
            )
        except subprocess.TimeoutExpired as e:
            raise ValueError("LibreOffice did not finish converting the document within 90 seconds.") from e
        exported = work / "document.pdf"
        if result.returncode or not exported.is_file():
            raise ValueError("LibreOffice could not convert this DOCX. Open it in Word to check it, then try again.")
        import pikepdf
        with pikepdf.open(exported) as pdf:
            if not len(pdf.pages):
                raise ValueError("LibreOffice produced a PDF with no pages.")
        data = exported.read_bytes()
        # Exclusive creation also refuses pre-existing symlinks. Never replace a PDF.
        number = 1
        while True:
            target = source.with_name(source.stem + (f" {number}" if number > 1 else "") + ".pdf")
            try:
                fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
                break
            except FileExistsError:
                number += 1
        try:
            with os.fdopen(fd, "wb") as out:
                out.write(data)
        except BaseException:
            target.unlink()
            raise
        return str(target)


def pdf_input(path):
    if not os.path.isfile(path):
        raise ValueError("File not found: " + os.fspath(path))
    if not supported(path):
        raise ValueError("Choose a PDF or DOCX file: " + os.path.basename(path))
    return convert_docx(path) if Path(path).suffix.lower() == ".docx" else os.path.abspath(path)


def conversion_note(source, pdf):
    return (f"Converted {os.path.basename(source)} locally with LibreOffice to {os.path.basename(pdf)}. "
            "Check the layout and detected fields, enter information, then review and sign. "
            "The signed output is a PDF; the Word original is unchanged.")
