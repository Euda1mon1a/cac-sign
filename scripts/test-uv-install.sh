#!/bin/bash
# Install cac-sign as a uv tool into a fresh scratch HOME, then exercise the command and --setup.
# Usage: scripts/test-uv-install.sh <scratch dir that does not exist yet> <wheel path, or "cac-sign==X.Y.Z">
# UVFLAGS passes extra flags to uv (e.g. "--refresh-package cac-sign" just after a PyPI upload).
# Behind a TLS-inspecting proxy, export SSL_CERT_FILE; it is passed through.
set -u
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
run cac-sign --help | head -2
run cac-sign --history
run cac-sign --setup </dev/null | sed "s#$R#R#g"
echo "== review Quick Action runs:"
run plutil -extract actions.0.action.ActionParameters.COMMAND_STRING raw -o - \
  "$H/Library/Services/CAC Sign….workflow/Contents/document.wflow" | sed "s#$R#R#g"
run "$H/.local/share/uv/tools/cac-sign/bin/python" -c \
  'import cac_sign_app as a; a._modules(); import cac_sign_editor, cac_sign_finder, pdf_readers, Quartz, Security; print("all modules import")'
run cac-sign --setup --remove | sed "s#$R#R#g"
echo "== Services left: $(ls "$H/Library/Services" | wc -l | tr -d ' ')"
