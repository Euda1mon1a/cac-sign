#!/bin/bash
# Installer scenarios for install.sh, each in its own scratch HOME. Never touches the real HOME.
#
# Usage: scripts/test-installer.sh <scratch dir that does not exist yet>
# Installs from a tarball of this checkout (tracked and new files), so it tests the working tree.
# Needs uv on PATH (for its cache) and network access for the uv bootstrap and Python packages.
# Behind a TLS-inspecting proxy, export SSL_CERT_FILE and CURL_CA_BUNDLE; they are passed through.
set -u
W="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd -P)"
R="${1:?usage: scripts/test-installer.sh <scratch dir that does not exist yet>}"
mkdir "$R" || exit 1
export UV_CACHE_DIR="$(uv cache dir)" UV_PYTHON_INSTALL_DIR="$(uv python dir)"
CLEANPATH=/usr/bin:/bin:/usr/sbin:/sbin   # no uv: forces the pinned-hash bootstrap

# tarball of the working tree (tracked + new files)
mkdir -p "$R/pkg/cac-sign-x"
(cd "$W" && git ls-files -co --exclude-standard -z | xargs -0 -I{} rsync -R "{}" "$R/pkg/cac-sign-x/")
tar -czf "$R/good.tgz" -C "$R/pkg" cac-sign-x

pass() { printf 'PASS %s\n' "$*"; }
fail() { printf 'FAIL %s\n' "$*"; }
inst() { local h="$1"; shift; env -i HOME="$h" PATH="$CLEANPATH" SHELL=/bin/zsh CAC_SIGN_NO_PROMPT=1 \
  UV_CACHE_DIR="$UV_CACHE_DIR" UV_PYTHON_INSTALL_DIR="$UV_PYTHON_INSTALL_DIR" \
  ${SSL_CERT_FILE:+"SSL_CERT_FILE=$SSL_CERT_FILE"} ${CURL_CA_BUNDLE:+"CURL_CA_BUNDLE=$CURL_CA_BUNDLE"} "$@"; }

echo "== a. fresh install, no uv on PATH (pinned-hash bootstrap)"
H=$R/a; mkdir -p "$H"
inst "$H" CAC_SIGN_TARBALL="$R/good.tgz" bash < "$W/install.sh" > "$R/a.log" 2>&1; rc=$?
[ $rc = 0 ] && [ -x "$H/.local/bin/uv" ] && [ -x "$H/.cac-sign/.venv/bin/python" ] && pass "fresh install with bootstrapped uv" || { fail "fresh install rc=$rc"; tail -5 "$R/a.log"; }
grep -q 'Installing uv ' "$R/a.log" && pass "bootstrap path taken" || fail "bootstrap path not taken"

echo "== a2. bad uv checksum refuses"
H=$R/a2; mkdir -p "$H"
sed 's/^  UV_SHA256="[0-9a-f]*"/  UV_SHA256="0000000000000000000000000000000000000000000000000000000000000000"/' "$W/install.sh" > "$R/badsum.sh"
inst "$H" CAC_SIGN_TARBALL="$R/good.tgz" bash < "$R/badsum.sh" > "$R/a2.log" 2>&1; rc=$?
[ $rc != 0 ] && grep -q 'pinned checksum' "$R/a2.log" && [ ! -e "$H/.local/bin/uv" ] && pass "checksum mismatch refused, no uv installed" || { fail "checksum rc=$rc"; tail -3 "$R/a2.log"; }

echo "== b. update migrates identity.local.json from a downloaded install (I2)"
H=$R/a
printf '{"full_name":"Jane Q. Doe"}' > "$H/.cac-sign/identity.local.json"
inst "$H" CAC_SIGN_TARBALL="$R/good.tgz" bash < "$W/install.sh" > "$R/b.log" 2>&1; rc=$?
ID="$H/Library/Application Support/cac-sign/identity.json"
[ $rc = 0 ] && [ -f "$ID" ] && grep -q 'Jane Q. Doe' "$ID" && [ "$(stat -f %Lp "$ID")" = 600 ] && pass "identity migrated on update, 0600" || { fail "I2 rc=$rc"; tail -3 "$R/b.log"; }
ls -a "$H" | grep -q '\.cac-sign\.old\.' && fail "old install left behind" || pass "no old install left behind"

echo "== c. foreign workflow with our name is refused and untouched (I3)"
H=$R/c; mkdir -p "$H/Library/Services/CAC Sign.workflow/Contents"
echo 'my own automator thing' > "$H/Library/Services/CAC Sign.workflow/Contents/document.wflow"
inst "$H" CAC_SIGN_TARBALL="$R/good.tgz" bash < "$W/install.sh" > "$R/c.log" 2>&1; rc=$?
[ $rc != 0 ] && grep -q 'my own automator thing' "$H/Library/Services/CAC Sign.workflow/Contents/document.wflow" && [ ! -e "$H/.cac-sign" ] && pass "foreign workflow refused before anything was installed" || { fail "I3 rc=$rc"; tail -3 "$R/c.log"; }

echo "== c2. an older cac-sign workflow (another checkout path) is recognised and replaced"
H=$R/c2; mkdir -p "$H/Library/Services"
cp -R "$W/quick-action/CAC Sign.workflow" "$H/Library/Services/"
sed -i '' 's#\$HOME/\.cac-sign#$HOME/old-clone/cac-sign#g' "$H/Library/Services/CAC Sign.workflow/Contents/document.wflow"
inst "$H" CAC_SIGN_TARBALL="$R/good.tgz" bash < "$W/install.sh" > "$R/c2.log" 2>&1; rc=$?
[ $rc = 0 ] && grep -q '\$HOME/\.cac-sign/cac_sign_finder' "$H/Library/Services/CAC Sign.workflow/Contents/document.wflow" && pass "old workflow replaced" || { fail "c2 rc=$rc"; tail -3 "$R/c2.log"; }

echo "== d. uninstall with a NUL byte in .zshrc keeps every other line (I1)"
H=$R/a
{ printf 'export A=1\n'; printf 'alias x=\000y\n'; cat "$H/.zshrc"; printf 'export B=2\n'; } > "$R/zshrc.new"; cat "$R/zshrc.new" > "$H/.zshrc"
cp "$H/.zshrc" "$R/zshrc.before"
inst "$H" bash -s -- --uninstall < "$W/install.sh" > "$R/d.log" 2>&1; rc=$?
grep -avF '# cac-sign (added by install.sh)' "$R/zshrc.before" > "$R/zshrc.expected"
[ $rc = 0 ] && cmp -s "$H/.zshrc" "$R/zshrc.expected" && cmp -s "$H/.zshrc.cac-sign.bak" "$R/zshrc.before" && pass "zshrc keeps all other lines (NUL included); backup exact" || { fail "I1 rc=$rc"; tail -3 "$R/d.log"; od -c "$H/.zshrc" | head -5; }
[ ! -e "$H/.cac-sign" ] && [ ! -e "$H/Library/Services/CAC Sign.workflow" ] && [ -f "$ID" ] && pass "uninstall removed code + actions, kept data" || fail "uninstall leftovers"

echo "== e. interrupted swap: on_exit puts .venv and the install back (I4)"
sed '$d' "$W/install.sh" > "$R/funcs.sh"
for state in before_old_set after_mv_app; do
  H=$R/e-$state; mkdir -p "$H/.cac-sign/.venv" "$H/new"
  echo marker > "$H/.cac-sign/.venv/v"; echo code > "$H/.cac-sign/c"; mv "$H/.cac-sign/.venv" "$H/new/.venv"
  OLDP=""
  if [ $state = after_mv_app ]; then OLDP="$H/.cac-sign.old.1"; mv "$H/.cac-sign" "$OLDP"; fi
  HOME="$H" bash -c 'set -u; source "$1"; APP="$HOME/.cac-sign"; NEW="$2"; OLD="$3"; on_exit' _ "$R/funcs.sh" "$H/new" "$OLDP" 2>/dev/null
  [ -f "$H/.cac-sign/.venv/v" ] && [ -f "$H/.cac-sign/c" ] && [ ! -e "$H/new" ] && pass "on_exit restores ($state)" || { fail "on_exit ($state)"; ls -laR "$H" | head -20; }
done

echo "== f. clone mode + identity prompt skipped + uninstall leaves clone"
H=$R/f; mkdir -p "$H"; cp -R "$R/pkg/cac-sign-x" "$R/clone"
inst "$H" bash "$R/clone/install.sh" > "$R/f.log" 2>&1 </dev/null; rc=$?
[ $rc = 0 ] && [ -L "$H/.cac-sign" ] && pass "clone linked" || { fail "clone rc=$rc"; tail -3 "$R/f.log"; }
inst "$H" bash "$R/clone/install.sh" --uninstall > /dev/null 2>&1
[ ! -e "$H/.cac-sign" ] && [ -f "$R/clone/cac_sign.py" ] && pass "clone uninstall leaves clone" || fail "clone uninstall"

echo "== g. uninstall when .zshrc holds only our line (fresh colleague Mac)"
H=$R/g; mkdir -p "$H"
inst "$H" CAC_SIGN_TARBALL="$R/good.tgz" bash < "$W/install.sh" > "$R/g.log" 2>&1
[ "$(grep -c '' "$H/.zshrc")" = 1 ] && pass "fresh .zshrc has just our line" || fail "fresh zshrc shape"
inst "$H" bash -s -- --uninstall < "$W/install.sh" > "$R/g2.log" 2>&1; rc=$?
[ $rc = 0 ] && [ ! -s "$H/.zshrc" ] && [ ! -e "$H/.cac-sign" ] && [ ! -e "$H/Library/Services/CAC Sign….workflow" ] && pass "uninstall completes, .zshrc emptied" || { fail "g rc=$rc"; cat "$R/g2.log"; }
