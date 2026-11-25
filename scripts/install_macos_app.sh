#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT_DIR="$(cd "${SCRIPT_DIR}/.." && pwd)"
PORTABLE_APP="${ROOT_DIR}/build/CACSign.app"
DEST_APP="${CAC_SIGN_INSTALL_DEST:-/Applications/CACSign.app}"
BUNDLE_ID="com.aaronmontgomery.cacsign"
SERVICES_DIR="${HOME}/Library/Services"
REGISTER_DOCK=1
REBUILD=0

for arg in "$@"; do
  case "$arg" in
    --no-dock) REGISTER_DOCK=0 ;;
    --rebuild) REBUILD=1 ;;
    *) echo "Usage: $0 [--rebuild] [--no-dock]" >&2; exit 1 ;;
  esac
done

if [[ "${REBUILD}" -eq 1 || ! -d "${PORTABLE_APP}" ]]; then
  echo "[install-cacsign] Building ${PORTABLE_APP}..."
  python3 "${ROOT_DIR}/scripts/build_macos_app.py" "${PORTABLE_APP}" --replace
fi

codesign --verify --strict --deep "${PORTABLE_APP}"

if [[ -d "${DEST_APP}" ]]; then
  EXISTING_ID="$(/usr/libexec/PlistBuddy -c "Print :CFBundleIdentifier" "${DEST_APP}/Contents/Info.plist" 2>/dev/null || true)"
  if [[ -n "${EXISTING_ID}" && "${EXISTING_ID}" != "${BUNDLE_ID}" ]]; then
    echo "Refusing to replace ${DEST_APP}: CFBundleIdentifier='${EXISTING_ID}' != '${BUNDLE_ID}'" >&2
    exit 1
  fi
fi

UUID_HEX="$(uuidgen | tr '[:upper:]' '[:lower:]' | tr -d '-')"
DEST_PARENT="$(dirname "${DEST_APP}")"
mkdir -p "${DEST_PARENT}"
STAGED_APP="${DEST_PARENT}/.CACSign-install-${UUID_HEX}.app"
BACKUP_DIR="${ROOT_DIR}/build/previous-apps/${UUID_HEX}"

rm -rf "${STAGED_APP}"
cp -R "${PORTABLE_APP}" "${STAGED_APP}"
codesign --verify --strict --deep "${STAGED_APP}"

if [[ -d "${DEST_APP}" ]]; then
  mkdir -p "${BACKUP_DIR}"
  mv "${DEST_APP}" "${BACKUP_DIR}/CACSign.app"
  echo "[install-cacsign] Backed up prior CACSign.app -> ${BACKUP_DIR}/CACSign.app"
fi

if ! mv "${STAGED_APP}" "${DEST_APP}"; then
  if [[ -d "${BACKUP_DIR}/CACSign.app" ]]; then
    mv "${BACKUP_DIR}/CACSign.app" "${DEST_APP}"
  fi
  exit 1
fi

codesign --verify --strict --deep "${DEST_APP}"

# Cleanly archive legacy Automator Quick Actions owned by cac-sign so NSServices don't duplicate them.
is_our_workflow() {
  grep -aqs -e 'cac_sign_finder\.py' -e 'cac_sign_editor\.py' "$1/Contents/document.wflow"
}
for wf_name in "CAC Sign.workflow" "CAC Sign….workflow"; do
  wf_path="${SERVICES_DIR}/${wf_name}"
  if [[ -d "${wf_path}" ]] && is_our_workflow "${wf_path}"; then
    mkdir -p "${BACKUP_DIR}/services"
    mv "${wf_path}" "${BACKUP_DIR}/services/${wf_name}"
    echo "[install-cacsign] Archived legacy Automator workflow ${wf_name} -> ${BACKUP_DIR}/services/"
  fi
done

LSREGISTER="/System/Library/Frameworks/CoreServices.framework/Frameworks/LaunchServices.framework/Support/lsregister"
if [[ -x "${LSREGISTER}" ]]; then
  "${LSREGISTER}" -f "${DEST_APP}" || true
fi
if [[ -x /System/Library/CoreServices/pbs ]]; then
  /System/Library/CoreServices/pbs -update >/dev/null 2>&1 || true
fi

if [[ "${REGISTER_DOCK}" -eq 1 && "${DEST_APP}" == "/Applications/CACSign.app" ]]; then
  while ! mkdir /tmp/cacsign-dock-register.lock 2>/dev/null; do sleep 0.2; done
  trap 'rmdir /tmp/cacsign-dock-register.lock 2>/dev/null || true' EXIT
  if ! defaults read com.apple.dock persistent-apps 2>/dev/null | grep -Fq "/Applications/CACSign.app"; then
    defaults write com.apple.dock persistent-apps -array-add '<dict><key>tile-data</key><dict><key>file-data</key><dict><key>_CFURLString</key><string>file:///Applications/CACSign.app/</string><key>_CFURLStringType</key><integer>15</integer></dict></dict></dict>'
    killall Dock || true
  fi
fi

echo "[install-cacsign] Installed and verified ${DEST_APP} (${BUNDLE_ID})"
