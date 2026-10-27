# Releasing

How a version of Redline gets from this repository to PyPI. Five distributions are
released in lockstep from one tag: `redline-core`, `django-redline`, `fastapi-redline`,
`flask-redline` and `litestar-redline`. They always share one version, and every adapter
pins `redline-core==<that version>`.

Publishing uses **PyPI Trusted Publishing** from `.github/workflows/release.yml`. No API
token exists anywhere, and none should be created.

---

## Once, before the first release

1. **Check the names are still free.** Each of these should be a 404 on
   <https://pypi.org/project/NAME/>: `redline-core`, `django-redline`, `fastapi-redline`,
   `flask-redline`, `litestar-redline`. If one is taken, stop: renaming touches that
   package's `pyproject.toml`, the pins in the adapters, and the README install lines.

2. **Register a pending publisher for each of the five projects**, on
   <https://pypi.org/manage/account/publishing/> and again on
   <https://test.pypi.org/manage/account/publishing/>. That is ten registrations. For
   every one, Owner is `Pythondeveloper6`, Repository name is `redline`, Workflow name is
   `release.yml`; only the project name and environment vary:

   | PyPI project name | pypi.org environment | test.pypi.org environment |
   |---|---|---|
   | `redline-core` | `pypi` | `testpypi` |
   | `django-redline` | `pypi` | `testpypi` |
   | `fastapi-redline` | `pypi` | `testpypi` |
   | `flask-redline` | `pypi` | `testpypi` |
   | `litestar-redline` | `pypi` | `testpypi` |

   A pending publisher turns into a normal one when the first upload creates the project.
   It also reserves nothing until then, so register all ten before tagging.

3. **Create the GitHub environments** in the repository's **Settings → Environments**:
   `pypi` (add yourself as a required reviewer, so every real upload waits for one
   click) and `testpypi` (no reviewer needed).

4. **Dry-run the build half.** Actions → Release → *Run workflow* on `main` runs the
   version check, UI bundle check, build, `twine check` and wheel smoke test without
   publishing anything.

5. **Rehearse with a pre-release.** Release `0.2.0a1` (or a `.dev`/`rc`) first and watch
   it land on both indexes before tagging anything final. A version can never be reused
   on PyPI, only yanked.

## Every release

1. **Versions, in lockstep.** Set the same version, in PEP 440 spelling (`0.2.0a1`,
   `0.2.0b1`, `0.2.0rc1`, `0.2.0`), in all of:
   - `version` in each `packages/*/pyproject.toml` (five files);
   - the `redline-core==<version>` pins in the four adapters' `dependencies` and in their
     `redline-core[profile]==<version>` extras;
   - `__version__` in each `packages/*/src/<module>/__init__.py`.

   Then run `uv lock` and check it all agrees:

   ```bash
   uv lock
   python3 scripts/check_versions.py --expect 0.2.0a1
   ```

2. **Changelog.** Rename the `Unreleased` heading in `CHANGELOG.md` to the version and the
   date you tag, and open a new empty `Unreleased` section above it.

3. **Rebuild the UI** if anything under `frontend/` changed, and commit the result. The
   wheels ship the committed bundle and the release refuses a stale one:

   ```bash
   scripts/build_ui.sh
   ```

4. **Check locally.**

   ```bash
   uv run ruff check . && uv run ruff format --check .
   uv build --all-packages --out-dir dist
   uvx twine check --strict dist/*
   rm -rf dist
   ```

5. **Merge to `main`** through a pull request and let CI go green.

6. **Tag and push** the merge commit:

   ```bash
   git tag -a v0.2.0a1 -m "Redline 0.2.0a1"
   git push origin v0.2.0a1
   ```

   `release.yml` then: refuses a tag that disagrees with any package version, rebuilds the
   UI and refuses a stale committed bundle, builds all five wheels and sdists, runs
   `twine check --strict`, installs the wheels into a clean venv and smoke-tests them,
   uploads to TestPyPI, **waits for your approval of the `pypi` deployment**, uploads to
   PyPI with attestations, and creates the GitHub release with the distributions attached
   (marked as a pre-release when the version contains `a`, `b`, `rc` or `dev`).

7. **Approve the `pypi` deployment** when the workflow pauses (Actions → the run →
   *Review deployments*), after glancing at the TestPyPI pages.

8. **Verify the install from PyPI**, from a fresh directory. `--pre` is needed for an
   alpha, beta or rc, since pip skips pre-releases otherwise:

   ```bash
   uv venv /tmp/redline-smoke && source /tmp/redline-smoke/bin/activate
   uv pip install --pre django-redline==0.2.0a1 fastapi-redline==0.2.0a1 \
       flask-redline==0.2.0a1 litestar-redline==0.2.0a1
   python -c "import redline_core, django_redline; print(redline_core.__version__)"
   ```

## If something goes wrong

- **The upload failed halfway.** Re-run the failed jobs (or the workflow). Both uploads
  tolerate files that already landed: TestPyPI uses `skip-existing`, and PyPI rejects
  duplicates of the same file without changing them.
- **A trusted-publisher error (`invalid-publisher`).** The registration does not match
  exactly: project name, owner, repository `redline`, workflow `release.yml` and the
  environment name must all be as in the table above.
- **A broken version reached PyPI.** Yank it (PyPI → Manage → Release → Options → Yank)
  and release the fix under the next number. A version can't be deleted and re-uploaded.
