#!/bin/bash
# Install cac-sign as a uv tool into a fresh scratch HOME, then exercise the command and --setup.
# Usage: scripts/test-uv-install.sh <scratch dir that does not exist yet> <wheel path, or "cac-sign==X.Y.Z">
# UVFLAGS passes extra flags to uv (e.g. "--refresh-package cac-sign" just after a PyPI upload).
# Behind a TLS-inspecting proxy, export SSL_CERT_FILE; it is passed through.
set -euo pipefail
R="${1:?scratch dir}"; SPEC="${2:?wheel path or cac-sign==X.Y.Z}"
mkdir "$R" || exit 1
mkdir -p "$R/home"
H="$R/home"
CACHE="$(uv cache dir)"; PYDIR="$(uv python dir)"
env -i HOME="$H" PATH=/usr/bin:/bin:/usr/sbin:/sbin:/opt/homebrew/bin \
  UV_TOOL_DIR="$H/.local/share/uv/tools" UV_TOOL_BIN_DIR="$H/.local/bin" \
  UV_CACHE_DIR="$CACHE" UV_PYTHON_INSTALL_DIR="$PYDIR" ${SSL_CERT_FILE:+"SSL_CERT_FILE=$SSL_CERT_FILE"} \
  uv tool install --quiet ${UVFLAGS:-} "$SPEC" || { echo "INSTALL FAILED"; exit 1; }
run() { env -i HOME="$H" PATH="$H/.local/bin:/usr/bin:/bin:/usr/sbin:/sbin" "$@"; }
echo "== command: $(ls -l "$H/.local/bin/cac-sign" | sed "s#$R#R#g" | awk '{print $9, $10, $11}')"
run cac-sign --help > "$R/help.txt"
sed -n '1,2p' "$R/help.txt"
run cac-sign --history
run cac-sign --setup </dev/null | sed "s#$R#R#g"
echo "== review Quick Action runs:"
run plutil -extract actions.0.action.ActionParameters.COMMAND_STRING raw -o - \
  "$H/Library/Services/CAC Sign….workflow/Contents/document.wflow" | sed "s#$R#R#g"
run "$H/.local/share/uv/tools/cac-sign/bin/python" -c \
  'import cac_sign_app as a; a._modules(); import cac_sign_editor, cac_sign_finder, pdf_readers, document_inputs, field_detection, Quartz, Security; print("all modules import")'
run "$H/.local/share/uv/tools/cac-sign/bin/python" - <<'PY'
import os, pathlib, plistlib, tempfile
import cac_sign_app as app
app._modules()
import cac_sign_editor
from reportlab.pdfgen import canvas
for name in ("CAC Sign.workflow", "CAC Sign….workflow"):
    path = pathlib.Path.home() / "Library/Services" / name / "Contents/Info.plist"
    assert "org.openxmlformats.wordprocessingml.document" in plistlib.loads(path.read_bytes())["NSServices"][0]["NSSendFileTypes"]
with tempfile.TemporaryDirectory() as temp:
    path = os.path.join(temp, "form.pdf")
    c = canvas.Canvas(path)
    c.drawString(72, 700, "Name: ____________________")
    c.drawString(72, 650, "Parent Signature: ____________________")
    c.save()
    model = cac_sign_editor.review_model(path)
    assert any(f.get("label") == "Name" and f.get("detected") for f in model["fields"])
    assert model["suggested_signature"]["label"] == "Parent Signature"
print("DOCX service registration and synthetic field detection pass")
PY
run cac-sign --setup --remove | sed "s#$R#R#g"
echo "== Services left: $(ls "$H/Library/Services" | wc -l | tr -d ' ')"
