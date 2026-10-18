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
  if [[ -x "$dir/.venv/bin/python" ]]; then
    "$dir/.venv/bin/python" "$dir/cac_sign.py" "$@"
  else
    uv run --project "$dir" python "$dir/cac_sign.py" "$@"
  fi
}
