#!/bin/bash
# Build Developer ID signed, Apple-notarized, and stapled CACSign-apple.zip / CACSign-apple.pkg
# + CACSign-apple.latest.json from a clean git HEAD archive.
#
# Example:
#   ./scripts/package-macos.sh \
#     --sign 'Developer ID Application: Your Name (TEAMID)' \
#     --notary-profile "<notarytool-keychain-profile>" \
#     --output ~/Downloads/CACSign/CACSign-apple.zip \
#     --pkg-output ~/Downloads/CACSign/CACSign-apple.pkg
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd -P)"
SIGN=""
PKG_SIGN=""
OUTPUT=""
PKG_OUTPUT=""
STAGE_ONLY=""
NOTARY_PROFILE="${CACSIGN_NOTARY_PROFILE:-}"
NO_TIMESTAMP=0

fail() { printf 'cac-sign package-macos: %s\n' "$*" >&2; exit 1; }

while [ "$#" -gt 0 ]; do
  case "$1" in
    --sign) [ "$#" -ge 2 ] || fail "Missing signing identity."; SIGN="$2"; shift 2 ;;
    --pkg-sign) [ "$#" -ge 2 ] || fail "Missing installer signing identity."; PKG_SIGN="$2"; shift 2 ;;
    --output) [ "$#" -ge 2 ] || fail "Missing output (.zip or .pkg) path."; OUTPUT="$2"; shift 2 ;;
    --pkg-output) [ "$#" -ge 2 ] || fail "Missing .pkg output path."; PKG_OUTPUT="$2"; shift 2 ;;
    --stage-only) [ "$#" -ge 2 ] || fail "Missing staging directory."; STAGE_ONLY="$2"; shift 2 ;;
    --notary-profile) [ "$#" -ge 2 ] || fail "Missing notarytool keychain profile."; NOTARY_PROFILE="$2"; shift 2 ;;
    --no-timestamp) NO_TIMESTAMP=1; shift 1 ;;
    *) fail "Unknown argument: $1" ;;
  esac
done

[ -n "$SIGN" ] && [ "$SIGN" != "-" ] || fail "Supply --sign with a Developer ID Application identity."

OUT_KIND=""
if [ -n "$STAGE_ONLY" ]; then
  [ -z "$OUTPUT" ] || fail "Choose --output or --stage-only."
  [[ "$STAGE_ONLY" = /* ]] && [ ! -e "$STAGE_ONLY" ] || fail "Staging directory must be absolute and new."
else
  case "$OUTPUT" in
    /*.zip) OUT_KIND="zip" ;;
    /*.pkg) OUT_KIND="pkg" ;;
    *) fail "Output must be an absolute new .zip or .pkg path." ;;
  esac
  [ ! -e "$OUTPUT" ] || fail "Output already exists: $OUTPUT"
  if [ -n "$PKG_OUTPUT" ]; then
    [ "$OUT_KIND" = "zip" ] || fail "--pkg-output requires --output <path.zip>."
    case "$PKG_OUTPUT" in
      /*.pkg) ;;
      *) fail "--pkg-output must be an absolute new .pkg path." ;;
    esac
    [ ! -e "$PKG_OUTPUT" ] || fail "Package output already exists: $PKG_OUTPUT"
  fi
  if [ "$OUT_KIND" = "pkg" ] || [ -n "$PKG_OUTPUT" ]; then
    if [ -z "$PKG_SIGN" ] && [[ "$SIGN" == "Developer ID Application: "* ]]; then
      PKG_SIGN="Developer ID Installer: ${SIGN#Developer ID Application: }"
    fi
    [ -n "$PKG_SIGN" ] && [ "$PKG_SIGN" != "-" ] || fail "Supply --pkg-sign with a Developer ID Installer identity."
  fi
fi

git -C "$ROOT" diff --quiet && git -C "$ROOT" diff --cached --quiet || fail "Commit tracked changes first; packaging uses git archive HEAD."
SOURCE="$(git -C "$ROOT" rev-parse HEAD)"

PYTHON_EXE=""
for cand in "$ROOT/.venv/bin/python" "$HOME/workspace/cac-sign/.venv/bin/python" "$HOME/.cac-sign/.venv/bin/python"; do
  if [ -x "$cand" ]; then PYTHON_EXE="$cand"; break; fi
done
[ -n "$PYTHON_EXE" ] || fail "Could not find standalone Python venv (.venv/bin/python); run uv sync --locked first."

if [ -n "$STAGE_ONLY" ]; then
  STAGE="$STAGE_ONLY"
  mkdir -p "$STAGE"
else
  STAGE="$(mktemp -d "${TMPDIR:-/tmp}/cacsign-package.XXXXXX")"
fi
success=false
trap 'if [ -z "$STAGE_ONLY" ] || ! $success; then rm -rf "$STAGE"; fi' EXIT

SRC_EXPORT="$STAGE/src"
mkdir -p "$SRC_EXPORT"
git -C "$ROOT" archive --format=tar HEAD | tar -xf - -C "$SRC_EXPORT"

if [ -n "$NOTARY_PROFILE" ]; then
  NOTARY_NOTE="notarized via $NOTARY_PROFILE"
else
  NOTARY_NOTE="signed with Developer ID (notarization not requested)"
fi

APP_BUNDLE="$STAGE/CACSign.app"
BUILD_ARGS=(
  "$APP_BUNDLE"
  --python "$PYTHON_EXE"
  --sign "$SIGN"
  --source-commit "$SOURCE"
  --notarization-note "$NOTARY_NOTE"
)
if [ "$NO_TIMESTAMP" -eq 1 ]; then BUILD_ARGS+=(--no-timestamp); fi
"$PYTHON_EXE" "$SRC_EXPORT/scripts/build_macos_app.py" "${BUILD_ARGS[@]}"

APP_EXEC="$APP_BUNDLE/Contents/MacOS/CACSign"
if xcrun nm -u "$APP_EXEC" | grep -Eq 'LanguageModelError|GenerationOptionsV12samplingMode'; then
  fail "CACSign binary imports macOS 27-only FoundationModels symbols that fail on macOS 26.x."
fi
/usr/bin/codesign --verify --strict --deep "$APP_BUNDLE"
APP_SHA="$(shasum -a 256 "$APP_EXEC" | awk '{print $1}')"
VERSION="$("$PYTHON_EXE" -c 'import re,sys; m=re.search(r"^version\s*=\s*\"([^\"]+)\"", open(sys.argv[1]).read(), re.M); print(m.group(1) if m else "0.2.0")' "$SRC_EXPORT/pyproject.toml")"

write_latest_json() {
  [ "$#" -eq 10 ] || fail "write_latest_json takes 10 arguments, got $#."
  local json_path="$1" version="$2" commit="$3" built_at="$4" notarized="$5" app_sha="$6"
  local zip_name="$7" zip_sha="$8" pkg_name="$9" pkg_sha="${10}"
  "$PYTHON_EXE" - "$json_path" "$version" "$commit" "$built_at" "$notarized" "$app_sha" "$zip_name" "$zip_sha" "$pkg_name" "$pkg_sha" <<'PY'
import json, sys
out, version, commit, built_at, notarized, app_sha, zip_name, zip_sha, pkg_name, pkg_sha = sys.argv[1:11]
data = {
    "version": version,
    "commit": commit,
    "built_at": built_at,
    "min_macos": "14.0",
    "app_target": "arm64-apple-macosx14.0",
    "notarized": notarized == "true",
}
if zip_name and zip_sha:
    data["zip_filename"] = zip_name
    data["zip_sha256"] = zip_sha
if pkg_name and pkg_sha:
    data["pkg_filename"] = pkg_name
    data["pkg_sha256"] = pkg_sha
data["app_sha256"] = app_sha
with open(out, "w", encoding="utf-8") as fh:
    fh.write(json.dumps(data, indent=2) + "\n")
PY
}

notarize_and_verify() {
  local target_file="$1"
  local submit_json="$STAGE/notary-submit-$$.json"
  xcrun notarytool submit "$target_file" --keychain-profile "$NOTARY_PROFILE" --wait --output-format json > "$submit_json"
  local sub_id sub_status
  sub_id="$("$PYTHON_EXE" -c 'import json,sys; print(json.load(open(sys.argv[1])).get("id",""))' "$submit_json")"
  sub_status="$("$PYTHON_EXE" -c 'import json,sys; print(json.load(open(sys.argv[1])).get("status",""))' "$submit_json")"
  printf 'Notarization submission %s (%s): %s\n' "$sub_id" "$(basename "$target_file")" "$sub_status"
  if [ "$sub_status" != "Accepted" ]; then
    if [ -n "$sub_id" ]; then
      xcrun notarytool log "$sub_id" --keychain-profile "$NOTARY_PROFILE" >&2 || true
    fi
    fail "Apple notarization did not accept $(basename "$target_file") (status: $sub_status)."
  fi
}

APP_NOTARIZED="false"
if [ -n "$NOTARY_PROFILE" ]; then
  NOTARY_ZIP="$STAGE/CACSign-notary-upload.zip"
  /usr/bin/ditto -c -k --sequesterRsrc --keepParent "$APP_BUNDLE" "$NOTARY_ZIP"
  notarize_and_verify "$NOTARY_ZIP"
  rm -f "$NOTARY_ZIP"
  xcrun stapler staple "$APP_BUNDLE"
  xcrun stapler validate "$APP_BUNDLE"
  /usr/sbin/spctl --assess --type execute --verbose "$APP_BUNDLE"
  APP_NOTARIZED="true"
fi

build_pkg_installer() {
  local pkg_path="$1"
  mkdir -p "$(dirname "$pkg_path")"
  PKG_ROOT="$STAGE/pkg-root"
  PKG_SCRIPTS="$STAGE/pkg-scripts"
  PKG_RES="$STAGE/pkg-resources"
  rm -rf "$PKG_ROOT" "$PKG_SCRIPTS" "$PKG_RES"
  mkdir -p "$PKG_ROOT" "$PKG_SCRIPTS" "$PKG_RES"
  cp -R "$APP_BUNDLE" "$PKG_ROOT/CACSign.app"

  cat > "$PKG_SCRIPTS/postinstall" <<'EOF'
#!/bin/bash
set -euo pipefail
TARGET_DIR="${2:-/Applications}"
DEST_APP="${TARGET_DIR%/}/CACSign.app"
if [ ! -d "$DEST_APP" ] && [ -d "$HOME/Applications/CACSign.app" ]; then
  DEST_APP="$HOME/Applications/CACSign.app"
elif [ ! -d "$DEST_APP" ] && [ -d "/Applications/CACSign.app" ]; then
  DEST_APP="/Applications/CACSign.app"
fi
if [ -d "$DEST_APP" ]; then
  /usr/bin/xattr -dr com.apple.quarantine "$DEST_APP" 2>/dev/null || true
  LSREG="/System/Library/Frameworks/CoreServices.framework/Frameworks/LaunchServices.framework/Support/lsregister"
  if [ -x "$LSREG" ]; then "$LSREG" -f "$DEST_APP" >/dev/null 2>&1 || true; fi
fi
if [ -x /System/Library/CoreServices/pbs ]; then
  /System/Library/CoreServices/pbs -update >/dev/null 2>&1 || true
fi
exit 0
EOF
  chmod 755 "$PKG_SCRIPTS/postinstall"

  cat > "$PKG_RES/welcome.html" <<EOF
<!doctype html>
<html><body style="font-family:-apple-system,sans-serif;font-size:13px;line-height:1.5;color:#1e293b;">
  <h2 style="margin-top:0;">Install CACSign (v$VERSION)</h2>
  <p>This installs <strong>CACSign.app</strong> — fill, review, and CAC-sign PDF and Word (<code>.docx</code>) forms locally on your Apple silicon Mac without Adobe Acrobat or Citrix/AVHE, while keeping existing signatures intact.</p>
  <p>Installs into <code>~/Applications</code> by default (no administrator password required) or <code>/Applications</code>.</p>
</body></html>
EOF

  cat > "$PKG_RES/conclusion.html" <<EOF
<!doctype html>
<html><body style="font-family:-apple-system,sans-serif;font-size:13px;line-height:1.5;color:#1e293b;">
  <h2 style="margin-top:0;">CACSign is ready</h2>
  <ol>
    <li>Open <strong>CACSign</strong> from Spotlight, Launchpad, or <code>~/Applications/CACSign.app</code>.</li>
    <li>Press <code>&#8984;,</code> to add your name, title, NPI, or phone for auto-filling forms.</li>
    <li>Drop any PDF or <code>.docx</code> onto the window — or right-click a file in Finder &rarr; <strong>Services / Quick Actions &rarr; CAC Sign&hellip;</strong>.</li>
  </ol>
</body></html>
EOF

  cat > "$STAGE/distribution.xml" <<EOF
<?xml version="1.0" encoding="utf-8"?>
<installer-gui-script minSpecVersion="2">
  <title>CACSign</title>
  <domains enable_anywhere="false" enable_currentUserHome="true" enable_localSystem="false"/>
  <options customize="never" require-scripts="false" hostArchitectures="arm64"/>
  <welcome file="welcome.html" mime-type="text/html"/>
  <conclusion file="conclusion.html" mime-type="text/html"/>
  <pkg-ref id="com.aaronmontgomery.cacsign.pkg"/>
  <choices-outline>
    <line choice="default">
      <line choice="com.aaronmontgomery.cacsign.pkg"/>
    </line>
  </choices-outline>
  <choice id="default"/>
  <choice id="com.aaronmontgomery.cacsign.pkg" visible="false">
    <pkg-ref id="com.aaronmontgomery.cacsign.pkg"/>
  </choice>
  <pkg-ref id="com.aaronmontgomery.cacsign.pkg" version="$VERSION" onConclusion="none">CACSign-component.pkg</pkg-ref>
</installer-gui-script>
EOF

  cat > "$STAGE/component.plist" <<'EOF'
<?xml version="1.0" encoding="utf-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<array>
  <dict>
    <key>BundleHasStrictIdentifier</key>
    <true/>
    <key>BundleIsRelocatable</key>
    <false/>
    <key>BundleIsVersionChecked</key>
    <false/>
    <key>BundleOverwriteAction</key>
    <string>upgrade</string>
    <key>RootRelativeBundlePath</key>
    <string>CACSign.app</string>
  </dict>
</array>
</plist>
EOF

  /usr/bin/pkgbuild \
    --root "$PKG_ROOT" \
    --component-plist "$STAGE/component.plist" \
    --identifier "com.aaronmontgomery.cacsign.pkg" \
    --version "$VERSION" \
    --ownership recommended \
    --install-location "/Applications" \
    --scripts "$PKG_SCRIPTS" \
    "$STAGE/CACSign-component.pkg"

  PROD_SIGN_ARGS=(--sign "$PKG_SIGN")
  if [ "$NO_TIMESTAMP" -eq 0 ]; then PROD_SIGN_ARGS+=(--timestamp); fi
  /usr/bin/productbuild \
    --distribution "$STAGE/distribution.xml" \
    --package-path "$STAGE" \
    --resources "$PKG_RES" \
    "${PROD_SIGN_ARGS[@]}" \
    "$pkg_path"

  /usr/sbin/pkgutil --check-signature "$pkg_path"
  if [ -n "$NOTARY_PROFILE" ]; then
    notarize_and_verify "$pkg_path"
    xcrun stapler staple "$pkg_path"
    xcrun stapler validate "$pkg_path"
    /usr/sbin/spctl --assess --type install --verbose "$pkg_path"
    PKG_NOTARIZED="true"
  else
    PKG_NOTARIZED="false"
  fi
  PKG_SHA="$(shasum -a 256 "$pkg_path" | awk '{print $1}')"
}

if [ -n "$OUTPUT" ]; then
  mkdir -p "$(dirname "$OUTPUT")"
  BUILT_AT="$(date -u +"%Y-%m-%dT%H:%M:%SZ")"
  NOTARIZED_JSON="$APP_NOTARIZED"
  ZIP_NAME="" ZIP_SHA="" PKG_NAME="" PKG_SHA="" PKG_NOTARIZED="false" TARGET_PKG=""

  if [ "$OUT_KIND" = "zip" ]; then
    /usr/bin/ditto -c -k --sequesterRsrc --keepParent "$APP_BUNDLE" "$OUTPUT"
    ZIP_NAME="$(basename "$OUTPUT")"
    ZIP_SHA="$(shasum -a 256 "$OUTPUT" | awk '{print $1}')"
  fi

  if [ "$OUT_KIND" = "pkg" ] || [ -n "$PKG_OUTPUT" ]; then
    TARGET_PKG="${PKG_OUTPUT:-$OUTPUT}"
    build_pkg_installer "$TARGET_PKG"
    PKG_NAME="$(basename "$TARGET_PKG")"
    NOTARIZED_JSON="$PKG_NOTARIZED"
  fi

  if [ "$OUT_KIND" = "zip" ]; then
    LATEST_JSON="${OUTPUT%.zip}.latest.json"
    write_latest_json "$LATEST_JSON" "$VERSION" "$SOURCE" "$BUILT_AT" "$NOTARIZED_JSON" "$APP_SHA" "$ZIP_NAME" "$ZIP_SHA" "$PKG_NAME" "$PKG_SHA"
    if [ -n "$TARGET_PKG" ] && [ "${TARGET_PKG%.pkg}.latest.json" != "$LATEST_JSON" ]; then
      write_latest_json "${TARGET_PKG%.pkg}.latest.json" "$VERSION" "$SOURCE" "$BUILT_AT" "$PKG_NOTARIZED" "$APP_SHA" "$ZIP_NAME" "$ZIP_SHA" "$PKG_NAME" "$PKG_SHA"
    fi
    printf 'Created %s%s (and %s) from %s. Notarized: %s.\n' "$OUTPUT" "${TARGET_PKG:+, $TARGET_PKG}" "$LATEST_JSON" "$SOURCE" "$NOTARIZED_JSON"
  else
    LATEST_JSON="${TARGET_PKG%.pkg}.latest.json"
    write_latest_json "$LATEST_JSON" "$VERSION" "$SOURCE" "$BUILT_AT" "$PKG_NOTARIZED" "$APP_SHA" "" "" "$PKG_NAME" "$PKG_SHA"
    printf 'Created %s (and %s) from %s. Notarized: %s.\n' "$TARGET_PKG" "$LATEST_JSON" "$SOURCE" "$PKG_NOTARIZED"
  fi
else
  printf 'Staged %s from %s. No package created; notarized: %s.\n' "$APP_BUNDLE" "$SOURCE" "$APP_NOTARIZED"
fi
success=true
