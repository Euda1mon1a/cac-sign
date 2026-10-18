#!/bin/bash
# cac-sign installer (macOS, Apple silicon).
#
#   curl -fsSL https://raw.githubusercontent.com/Euda1mon1a/cac-sign/v0.1.0/install.sh | bash
#
# Puts the tool in ~/.cac-sign, its Python environment in ~/.cac-sign/.venv, the two
# Finder Quick Actions in ~/Library/Services, and the `cac-sign` command in ~/.zshrc.
# Your own data (name for forms, signing log) lives in
# ~/Library/Application Support/cac-sign and is never touched by an update or uninstall.
#
# Installs the release this script names (CAC_SIGN_REF below); to update, run the
# install line of a newer release. Uninstall:
#   curl -fsSL https://raw.githubusercontent.com/Euda1mon1a/cac-sign/v0.1.0/install.sh | bash -s -- --uninstall
#
# From a git clone, `./install.sh` links ~/.cac-sign to the clone instead of downloading.
#
# Environment (all optional): CAC_SIGN_REF (tag or branch to download),
# CAC_SIGN_TARBALL (a local .tar.gz to install instead of downloading),
# CAC_SIGN_NO_PROMPT=1 (don't ask for your name and phone).
#
# Everything is inside main(), so a download cut off halfway runs nothing.

main() {
  set -euo pipefail

  REPO="Euda1mon1a/cac-sign"
  REF="${CAC_SIGN_REF:-v0.1.0}"
  APP="$HOME/.cac-sign"
  DATA="$HOME/Library/Application Support/cac-sign"
  SERVICES="$HOME/Library/Services"
  RC="$HOME/.zshrc"
  RC_MARK="# cac-sign (added by install.sh)"
  # Used only if uv is not installed already: a release binary, checked against its pinned hash.
  UV_VERSION="0.10.11"
  UV_SHA256="437a7d498dd6564d5bf986074249ba1fc600e73da55ae04d7bd4c24d5f149b95"
  NEW=""
  OLD=""

  case "${1:-}" in
    -h|--help) sed -n '2,22p' "${BASH_SOURCE[0]:-/dev/null}" 2>/dev/null || true
               echo "Usage: install.sh [--uninstall]"; return 0 ;;
    --uninstall) uninstall; return 0 ;;
    "") ;;
    *) die "unknown option: $1 (try --help)" ;;
  esac

  [ "$(uname -s)" = "Darwin" ] || die "cac-sign needs macOS."
  # A pinned dependency (cryptography) ships macOS wheels for Apple silicon only.
  [ "$(sysctl -n hw.optional.arm64 2>/dev/null)" = "1" ] || die "cac-sign needs an Apple silicon Mac (M1 or later)."
  [ "$(id -u)" != "0" ] || die "run this as yourself, not with sudo."

  # Running from a clone? Then link to it rather than download.
  local src=""
  if [ -n "${BASH_SOURCE[0]:-}" ] && [ -f "${BASH_SOURCE[0]}" ]; then
    local here
    here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd -P)"
    [ -f "$here/cac_sign.py" ] && src="$here"
  fi

  check_quick_actions_are_ours
  mkdir -p "$DATA"
  chmod 700 "$DATA"
  if [ -n "$src" ]; then
    link_checkout "$src"
  else
    download_and_place
  fi

  local uv
  uv="$(find_uv)"
  say "Setting up Python packages (pinned by uv.lock)..."
  (cd "$APP" && "$uv" sync --locked --quiet) || die "uv sync failed; nothing else was changed. Re-run to retry."
  if [ -n "$OLD" ]; then rm -rf "${OLD:?}"; fi
  OLD=""

  install_quick_actions
  install_shell_command
  set_up_identity

  say ""
  say "Done. cac-sign is installed."
  say "  Finder: right-click a PDF > Quick Actions > CAC Sign… (review page) or CAC Sign (quick)."
  say "  Terminal: open a new window, then: cac-sign --help"
  say "  Your data: $DATA"
  say "Signatures from other people verify as trusted only if this Mac trusts the DoD"
  say "root certificates; see the README if --verify reports an untrusted signer."
}

say() { printf '%s\n' "$*"; }
die() { printf 'cac-sign install: %s\n' "$*" >&2; exit 1; }

link_checkout() {
  local src="$1"
  if [ -L "$APP" ]; then
    ln -sfn "$src" "$APP"
  elif [ -e "$APP" ]; then
    die "$APP is a downloaded install. Run install.sh --uninstall first, then this again."
  else
    ln -s "$src" "$APP"
  fi
  say "Linked $APP -> $src"
}

download_and_place() {
  if [ -L "$APP" ]; then
    die "$APP links to a git clone ($(readlink "$APP")). Update it with git pull and run its install.sh."
  fi
  if [ -e "$APP" ] && [ ! -f "$APP/.installed-by-cac-sign" ]; then
    die "$APP exists but was not made by this installer; move it away first."
  fi

  NEW="$(mktemp -d "$HOME/.cac-sign.new.XXXXXX")"
  trap on_exit EXIT

  if [ -n "${CAC_SIGN_TARBALL:-}" ]; then
    say "Installing from $CAC_SIGN_TARBALL"
    tar -xzf "$CAC_SIGN_TARBALL" -C "$NEW" --strip-components 1
  else
    say "Downloading cac-sign $REF from github.com/$REPO..."
    curl -fsSL "https://github.com/$REPO/archive/$REF.tar.gz" | tar -xz -C "$NEW" --strip-components 1
  fi
  [ -f "$NEW/cac_sign.py" ] && [ -f "$NEW/uv.lock" ] || die "the download does not look like cac-sign."
  printf 'ref=%s\ninstalled=%s\n' "$REF" "$(date -u +%Y-%m-%dT%H:%M:%SZ)" > "$NEW/.installed-by-cac-sign"

  if [ -d "$APP" ]; then
    migrate_identity "$APP"   # before the old folder can go away
    # Keep the Python environment and any profiles you added.
    local f
    for f in "$APP"/profiles/*.json; do
      [ -e "$f" ] || continue
      [ -e "$NEW/profiles/$(basename "$f")" ] || cp -p "$f" "$NEW/profiles/"
    done
    if [ -d "$APP/.venv" ]; then mv "$APP/.venv" "$NEW/.venv"; fi
    OLD="$APP.old.$$"
    mv "$APP" "$OLD"
  fi
  mv "$NEW" "$APP"
  NEW=""
  say "Installed to $APP"
}

# Interrupted or failed: never lose the Python environment, and put the previous install back.
on_exit() {
  if [ -n "${NEW:-}" ] && [ -d "$NEW" ]; then
    local home="$APP"
    [ -d "$home" ] || home="${OLD:-}"
    if [ -d "$NEW/.venv" ] && [ -n "$home" ] && [ -d "$home" ] && [ ! -e "$home/.venv" ]; then
      mv "$NEW/.venv" "$home/.venv"
    fi
    rm -rf "${NEW:?}"
  fi
  if [ -n "${OLD:-}" ] && [ -d "$OLD" ]; then
    if [ -d "$APP/.venv" ] && [ ! -d "$OLD/.venv" ]; then mv "$APP/.venv" "$OLD/.venv"; fi
    rm -rf "${APP:?}"
    mv "$OLD" "$APP"
    printf 'cac-sign install: restored the previous install.\n' >&2
  fi
}

find_uv() {
  if command -v uv >/dev/null 2>&1; then
    command -v uv
    return 0
  fi
  local bin="$HOME/.local/bin"
  if [ -x "$bin/uv" ]; then
    printf '%s\n' "$bin/uv"
    return 0
  fi
  say "Installing uv $UV_VERSION (Python package manager) to ~/.local/bin..." >&2
  local tmp
  tmp="$(mktemp -d)"
  curl -fsSL -o "$tmp/uv.tar.gz" \
    "https://github.com/astral-sh/uv/releases/download/$UV_VERSION/uv-aarch64-apple-darwin.tar.gz"
  if [ "$(shasum -a 256 "$tmp/uv.tar.gz" | cut -d' ' -f1)" != "$UV_SHA256" ]; then
    rm -rf "${tmp:?}"
    die "the uv download does not match its pinned checksum; nothing was installed."
  fi
  tar -xzf "$tmp/uv.tar.gz" -C "$tmp"
  mkdir -p "$bin"
  mv "$tmp/uv-aarch64-apple-darwin/uv" "$bin/uv"
  rm -rf "${tmp:?}"
  printf '%s\n' "$bin/uv"
}

# A workflow is ours if it runs one of our scripts; anything else with the same name is the user's.
is_our_workflow() {
  grep -aqs -e 'cac_sign_finder\.py' -e 'cac_sign_editor\.py' "$1/Contents/document.wflow"
}

check_quick_actions_are_ours() {
  local name
  for name in "CAC Sign.workflow" "CAC Sign….workflow"; do
    if [ -e "$SERVICES/$name" ] && ! is_our_workflow "$SERVICES/$name"; then
      die "~/Library/Services/$name exists and is not cac-sign's; rename it, then run this again."
    fi
  done
}

install_quick_actions() {
  mkdir -p "$SERVICES"
  local wf name
  for wf in "$APP"/quick-action/*.workflow; do
    [ -d "$wf" ] || continue
    name="$(basename "$wf")"
    if [ -e "$SERVICES/$name" ]; then
      is_our_workflow "$SERVICES/$name" || die "~/Library/Services/$name is not cac-sign's; left it alone."
      rm -rf "${SERVICES:?}/${name:?}"
    fi
    cp -R "$wf" "$SERVICES/$name"
  done
  if [ -x /System/Library/CoreServices/pbs ]; then /System/Library/CoreServices/pbs -update >/dev/null 2>&1 || true; fi
  say "Added the Finder Quick Actions \"CAC Sign\" and \"CAC Sign…\""
}

install_shell_command() {
  if grep -aqs 'cac-sign.zsh' "$RC"; then
    say "~/.zshrc already loads the cac-sign command."
  else
    if [ -s "$RC" ] && [ -n "$(tail -c 1 "$RC")" ]; then printf '\n' >> "$RC"; fi   # finish a last line with no newline
    printf '[ -f "$HOME/.cac-sign/cac-sign.zsh" ] && source "$HOME/.cac-sign/cac-sign.zsh"  %s\n' "$RC_MARK" >> "$RC"
    say "Added the cac-sign command to ~/.zshrc."
  fi
  case "${SHELL:-}" in
    */zsh) ;;
    *) say "Your shell is not zsh: to use cac-sign in Terminal, source ~/.cac-sign/cac-sign.zsh from your shell's startup file." ;;
  esac
}

# Older versions kept identity.local.json next to the code; it belongs with the user's data.
migrate_identity() {
  local ident="$DATA/identity.json"
  if [ ! -f "$ident" ] && [ -f "$1/identity.local.json" ]; then
    (umask 077 && cp "$1/identity.local.json" "$ident")
    say "Moved your details for forms to $ident"
  fi
}

# Profiles fill your name, title and duty phone from identity.json. Ask once.
set_up_identity() {
  local ident="$DATA/identity.json"
  migrate_identity "$APP"
  if [ -f "$ident" ]; then
    say "Your details for forms: $ident"
    return 0
  fi
  if [ -n "${CAC_SIGN_NO_PROMPT:-}" ] || ! (: < /dev/tty) 2>/dev/null; then
    say "To pre-fill your name on forms later, copy identity.example.json to $ident and edit it."
    return 0
  fi
  local name title phone
  say ""
  say "Forms you sign often can be pre-filled with your details. Press Enter to skip any."
  printf '  Name as it goes on forms (e.g. Jane Q. Doe): ' > /dev/tty; IFS= read -r name < /dev/tty || name=""
  printf '  Title (e.g. Program Director): ' > /dev/tty;              IFS= read -r title < /dev/tty || title=""
  printf '  Duty phone: ' > /dev/tty;                                 IFS= read -r phone < /dev/tty || phone=""
  if [ -z "$name$title$phone" ]; then
    say "Skipped. You can add them later in $ident (see identity.example.json)."
    return 0
  fi
  CS_NAME="$name" CS_TITLE="$title" CS_PHONE="$phone" "$APP/.venv/bin/python" - "$ident" <<'PY'
import json, os, sys
values = {"full_name": os.environ["CS_NAME"], "title": os.environ["CS_TITLE"],
          "duty_phone": os.environ["CS_PHONE"]}
fd = os.open(sys.argv[1], os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
with os.fdopen(fd, "w", encoding="utf-8") as f:
    json.dump({k: v for k, v in values.items() if v}, f, indent=2)
    f.write("\n")
PY
  say "Saved to $ident (only you can read it)."
}

uninstall() {
  local wf name removed=""
  for name in "CAC Sign.workflow" "CAC Sign….workflow"; do
    wf="$SERVICES/$name"
    if [ -d "$wf" ] && is_our_workflow "$wf"; then
      rm -rf "${wf:?}"
      removed="yes"
    fi
  done
  if [ -n "$removed" ]; then say "Removed the Quick Actions."; fi
  if [ -x /System/Library/CoreServices/pbs ]; then /System/Library/CoreServices/pbs -update >/dev/null 2>&1 || true; fi

  if grep -aqsF "$RC_MARK" "$RC"; then
    local tmp before marked after
    tmp="$(mktemp)"
    grep -avF "$RC_MARK" "$RC" > "$tmp" || true   # -a: a stray binary byte must not turn this into "Binary file matches"
    # grep -c prints 0 but exits 1 on no match; `|| true` keeps set -e from ending the script there
    before="$(grep -ac '' "$RC" || true)"
    marked="$(grep -acF "$RC_MARK" "$RC" || true)"
    after="$(grep -ac '' "$tmp" || true)"
    if [ "$after" -ne $((before - marked)) ]; then
      rm -f "$tmp"
      die "could not remove the cac-sign line from ~/.zshrc safely; it is unchanged. Delete the line ending in \"$RC_MARK\" by hand."
    fi
    cp -p "$RC" "$RC.cac-sign.bak"
    cat "$tmp" > "$RC"   # write through, so a symlinked ~/.zshrc stays a symlink
    rm -f "$tmp"
    say "Removed the cac-sign line from ~/.zshrc (previous copy: ~/.zshrc.cac-sign.bak)."
  fi

  if [ -L "$APP" ]; then
    rm -f "$APP"
    say "Removed the link $APP (the clone itself is untouched)."
  elif [ -f "$APP/.installed-by-cac-sign" ]; then
    rm -rf "${APP:?}"
    say "Removed $APP."
  elif [ -e "$APP" ]; then
    say "Left $APP alone: it was not made by this installer."
  fi
  say "Kept your data in $DATA (name for forms, signing log). Delete that folder too if you want it gone."
}

main "$@"
