# cac-sign: fill + CAC-sign + verify any PDF on this Mac -- no AVHE, no Adobe.
#
# Install: install.sh adds this to ~/.zshrc ->  source "$HOME/.cac-sign/cac-sign.zsh"
# Usage:   cac-sign ~/Downloads/form.pdf            (profile auto-detected)
#          cac-sign ~/Downloads/*Renewal.pdf        (batch; macOS asks the PIN per file, or --opensc for one)
#          cac-sign form.pdf --list | --verify | --dry-run | --help
#
# macOS asks for the PIN in its own smart-card window; this tool never sees it.
cac-sign() {
  local dir="${CAC_SIGN_HOME:-$HOME/.cac-sign}"
  local sys_app="${CAC_SIGN_SYSTEM_APP:-/Applications/CACSign.app}"
  local app_bundle="${CAC_SIGN_APP:-$sys_app}"
  if [[ ! -d "$app_bundle" && -z "${CAC_SIGN_APP:-}" && -d "$HOME/Applications/CACSign.app" ]]; then
    app_bundle="$HOME/Applications/CACSign.app"
  fi
  local app_py="$app_bundle/Contents/Resources/python/bin/python3"
  local app_launcher="$app_bundle/Contents/Resources/app/cac_sign_launch.py"
  if [[ -x "$dir/.venv/bin/python" ]]; then
    "$dir/.venv/bin/python" "$dir/cac_sign_launch.py" "$@"
  elif [[ -x "$app_py" && -f "$app_launcher" ]]; then
    "$app_py" "$app_launcher" "$@"
  else
    uv run --project "$dir" python "$dir/cac_sign_launch.py" "$@"
  fi
}
