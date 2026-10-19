# Releasing cac-sign

A release reaches users four ways:
- the curl installer, pinned to a tag;
- the GitHub release;
- PyPI;
- the Homebrew tap.

Each step below has its check. A release isn't done until every route has installed in a
clean place.

1. **Bump the version** in three places:
   - `pyproject.toml` (`version`);
   - `install.sh` (`CAC_SIGN_REF`, the tag it installs);
   - every `vX.Y.Z` in `README.md` and `install.sh`'s comments.
2. **Test.**
   - `uv run --with pytest pytest -q tests` on Python 3.12, and once with `--python 3.14`.
     Homebrew installs on 3.14.
   - `scripts/test-installer.sh <new scratch dir>`: every line must be PASS.
3. **Pull request, then "Rebase and merge"**, so each commit keeps its own author. `main` takes
   changes only through pull requests.
4. **Tag** the merge: create a GitHub release `vX.Y.Z` on `main`. Release tags can't be moved
   or deleted.
5. **Build from a clean copy of the tag:**
   `git archive vX.Y.Z | tar -x -C <empty dir> && cd <that dir> && uv build`.
   The sdist should list the same files as `git ls-files`, plus `PKG-INFO` and minus `AGENTS.md`.
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

Security fixes get a test in `tests/` that fails before the fix. The review rounds before
v0.1.0 are summarised in `CLAUDE.md`.
