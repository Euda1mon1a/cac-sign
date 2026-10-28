# Releasing cac-sign

A release reaches users four ways:
- the curl installer, pinned to a tag;
- the GitHub release;
- PyPI;
- the Homebrew tap.

Each step below has its check. A release isn't done until every route has installed in a
clean place.

1. **Bump the version** consistently:
   - `pyproject.toml` (`version`);
   - `install.sh` (`CAC_SIGN_REF`, the tag it installs);
   - every `vX.Y.Z` in `README.md` and `install.sh`'s comments.
   - run `uv lock --offline`; only the project's own version should change unless
     dependency updates were separately intended.
   - add release notes to `CHANGELOG.md` and document feature limits in the user guide.
2. **Test.**
   - `uv run --locked --python 3.12 --with pytest pytest -q tests`, then the same on
     Python 3.14. Use separate `UV_PROJECT_ENVIRONMENT` directories so testing never
     replaces the environment behind an installed Quick Action. Homebrew uses 3.14.
   - `scripts/test-installer.sh <new scratch dir>`: every line must be PASS.
   - Build the wheel and run `scripts/test-uv-install.sh <new scratch dir> <wheel>`
     before uploading. Its smoke check imports both `document_inputs` and
     `field_detection`, checks DOCX service registration and detects a synthetic form.
3. **Pull request, then "Rebase and merge"**, so each commit keeps its own author. `main` takes
   changes only through pull requests.
4. **Tag** the merge: create a GitHub release `vX.Y.Z` on `main`. Release tags can't be moved
   or deleted.
5. **Build from a clean copy of the tag:**
   `git archive vX.Y.Z | tar -x -C <empty dir> && cd <that dir> && uv build`.
   The sdist should list the same files as `git ls-files`, plus `PKG-INFO` and minus `AGENTS.md`.
   Check that no user documents, local test output, identities or signing material
   are included. Keep built artifacts and validation receipts outside the checkout.
6. **Publish to PyPI:** `uv publish dist/*`.
7. **Check PyPI.** Run `scripts/test-uv-install.sh <new scratch dir> cac-sign==X.Y.Z`. PyPI's
   index can lag a minute; `UVFLAGS="--refresh-package cac-sign"` gets past a cached miss.
8. **Homebrew.** In `Euda1mon1a/homebrew-tap`, run
   `.github/scripts/cac_sign_formula.py <cac-sign checkout at the tag> X.Y.Z Formula/cac-sign.rb`
   and open a pull request. The formula installs the exact wheels in `uv.lock`, so regenerate
   it whenever the lock changes. The tap's CI must pass: build, `brew test`, audit. After the
   merge, its `user-install` job must also pass; it runs `brew install` as a user would.
9. **Check the curl installer** from the tag, in a scratch home:
   `curl -fsSL https://raw.githubusercontent.com/Euda1mon1a/cac-sign/vX.Y.Z/install.sh | env HOME=<scratch> bash`.

The macOS PIN prompt and owner-entered form values remain owner actions. Synthetic
certificate tests establish the signing path; do not describe them as live-CAC acceptance.

Security fixes get a test in `tests/` that fails before the fix. The review rounds before
v0.1.0 are summarised in `CLAUDE.md`.
