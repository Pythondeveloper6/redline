# Releasing

The five distributions share one version: `redline-core`, `django-redline`,
`fastapi-redline`, `flask-redline` and `litestar-redline`. Each adapter pins the exact
core version. `.github/workflows/release.yml` builds the commit that passed CI on
`main`; publishing uses PyPI Trusted Publishing without an API token.

## Full-stack qualification before the final merge

Keep all work on the single release branch and run the shared disposable test project:

```bash
docker compose -f examples/compose.yaml up --build \
  --abort-on-container-exit --exit-code-from tests tests
```

Require exit zero and a current `examples/testing/results/summary.json` with
`qualified: true`. Keep its logs/JUnit/screenshots for review. Configuration parsing or
collecting the tests does not qualify a release. This stack includes the previously
socket-dependent tests, live PostgreSQL/MySQL/SQL Server, Redis/Celery prefork and browser
flows. See [the full test-project guide](examples/testing/README.md), including native
Linux amd64 SQL Server requirements. Complete deployment-specific checks in addition.
After validation, use one final merge into `main` and one coordinated release of all five
packages. Do not split features into separate releases to work around a failed test.

## Trigger and authorization policy

CI and Docs run on pushes to `main` only. Release listens for completion of CI on
`main`, and its build runs only when that CI run was a successful `push` from this
repository. It checks out `workflow_run.head_sha`, even if `main` has advanced. There
are no pull-request, tag-push or manual workflow triggers. A failed or cancelled CI
run cannot build or publish a release. A Release run record may still appear with
its jobs skipped after failed CI because GitHub emits completion events regardless
of the conclusion.

Every eligible run rebuilds and checks the UI, verifies lockstep versions, builds
five wheels and five source distributions, runs strict metadata checks and smoke-tests
the wheels. **The default ends with verified build artifacts. It does not publish.**

Publication additionally requires repository Actions variable
`REDLINE_RELEASE_VERSION` to equal the built package version exactly, without `v`
(for example, `0.5.0a1`). An unset, empty or different value skips publication. The
workflow also refuses a version with an existing GitHub release, existing version
tag, or any existing PyPI distribution at that version. It uploads to TestPyPI,
waits for the existing `pypi-*` environment approvals, uploads to PyPI and creates
`vVERSION` plus a GitHub release at the exact CI-tested SHA. Prereleases are marked
as such. The version variable is checked again when each publishing job is scheduled.

These controls use GitHub's documented
[workflow completion event](https://docs.github.com/en/actions/reference/workflows-and-actions/events-that-trigger-workflows#workflow_run)
and [PyPI OIDC publishing](https://docs.github.com/en/actions/how-tos/secure-your-work/security-harden-deployments/oidc-in-pypi).

## Required repository and publisher settings

These remote settings must be reviewed by a maintainer; editing workflow files does
not configure them:

- Make `main` the default branch. GitHub requires the Release workflow to exist on
  the default branch before its `workflow_run` trigger is eligible.
- Protect `main`: require pull requests and restrict bypasses/direct pushes. A `push`
  workflow cannot itself distinguish an approved merge from a direct push. Without
  this branch rule, direct pushes to `main` also run CI and Docs.
- Align required status checks with post-merge CI. There are no PR checks or merge-queue
  checks in this policy; requiring those checks before merge would block it. Run the
  documented local checks before approving a merge.
- Keep `REDLINE_RELEASE_VERSION` absent except during an explicitly approved release.
  Only maintainers authorized to publish should be able to change repository variables.
- Configure Pages with **GitHub Actions** as its source and retain `github-pages`.
  Docs deploys on each `main` push independently of release publication and CI completion.
- Retain the environments below. Configure every `pypi-*` environment with required
  reviewers, prevent self-review where supported, and allow deployments from `main`.
  Previous tag-only deployment restrictions must be updated for this workflow. A
  variable match alone is not a substitute for required reviewers.
- On PyPI and TestPyPI, verify trusted publishers: owner `Pythondeveloper6`, repository
  `redline`, workflow `release.yml`, and the exact environment from this table. Existing
  projects use their publishing settings; pending publishers are for projects that do
  not yet exist. No new token is needed.

| Project | PyPI environment | TestPyPI environment |
|---|---|---|
| `redline-core` | `pypi-redline-core` | `testpypi-redline-core` |
| `django-redline` | `pypi-django-redline` | `testpypi-django-redline` |
| `fastapi-redline` | `pypi-fastapi-redline` | `testpypi-fastapi-redline` |
| `flask-redline` | `pypi-flask-redline` | `testpypi-flask-redline` |
| `litestar-redline` | `pypi-litestar-redline` | `testpypi-litestar-redline` |

Trusted Publishing and environment approvals cannot be validated by local build
checks. Confirm their current remote configuration before authorizing publication.

## Prepare and verify one release

1. Set the same PEP 440 version in all five `packages/*/pyproject.toml` files, each
   package's `__version__`, and all adapters' regular and optional core dependency pins.
   Update the changelog with the version and release date, and document the features.

   ```bash
   uv lock
   python3 scripts/check_versions.py --expect 0.5.0a1
   ```

2. Rebuild the committed UI with `scripts/build_ui.sh` when frontend sources change.
   Never hand-edit the generated bundle. Run the full documented test commands plus:

   ```bash
   uv run ruff check .
   uv run ruff format --check .
   uv build --all-packages --out-dir /tmp/redline-release-dist
   uvx twine check --strict /tmp/redline-release-dist/*
   ```

   Use an empty output directory for each build. Review the live database/worker
   validation limits in `docs/production.md` before publishing.

3. Merge the reviewed release to `main` after authorization. With the repository variable
   unset, successful CI automatically exercises Release's build and verification steps
   without uploading or creating a tag. Local merges alone start no remote workflows.
   Inspect the successful run's tested SHA, package version and `dist` artifact.

4. Obtain explicit authorization to publish that exact version and commit. Set repository
   Actions variable `REDLINE_RELEASE_VERSION` to that version, for example `0.5.0a1`, only
   after that approval. Setting the variable does not start a workflow.

5. For the same tested commit, re-run its original **CI** run. Its successful completion
   starts Release again. Alternatively, an explicitly approved subsequent merge to `main`
   starts its own CI/Release chain; inspect that new SHA before approving publication.
   No tag push or **Run workflow** action is needed or available. A version with an
   existing tag/release/PyPI upload is skipped rather than published by a fresh run.

6. Inspect TestPyPI, the exact tested SHA and all five artifacts, then approve the
   `pypi-*` deployments in Actions. The workflow publishes the same build artifacts,
   creates the tag at the tested SHA, and attaches the distributions to its GitHub release.

7. Delete `REDLINE_RELEASE_VERSION` promptly after success or cancellation. This workflow
   does not clear repository variables automatically and has no variable-write permission.
   Clearing the variable prevents later publishing jobs from being scheduled, but cannot
   revoke a job already admitted or an upload already running. To withdraw authorization,
   also cancel the Release run and reject pending environment approvals; confirm no upload
   already completed. Review other queued runs before authorizing any retry.

8. Verify installation from PyPI in a fresh environment using the exact version. Add
   `--prerelease=allow` for alpha, beta, release-candidate or development versions:

   ```bash
   uv venv /tmp/redline-release-smoke
   VIRTUAL_ENV=/tmp/redline-release-smoke uv pip install --prerelease=allow \
     django-redline==0.5.0a1 fastapi-redline==0.5.0a1 \
     flask-redline==0.5.0a1 litestar-redline==0.5.0a1
   /tmp/redline-release-smoke/bin/python -c \
     'import redline_core, django_redline; print(redline_core.__version__)'
   ```

## Recovery

- **Partial upload:** re-run only failed jobs of the original Release run while its
  exact-version authorization is still approved. Successful authorization/build jobs and
  their artifacts are retained; existing files are skipped. Do not rebuild different code
  under the same version. Starting a fresh Release run finds existing PyPI files and skips
  publication to prevent mixing builds. Expired artifacts require maintainer investigation
  and usually a new version.
- **GitHub release creation or attachment failure:** inspect the tag, release and artifacts
  before retrying. The final job refuses a tag/release that appeared after authorization;
  if GitHub created the release but attachment upload failed, recover its missing assets
  separately with explicit authorization. Do not remove a published version's tag to
  bypass the duplicate guard.
- **Publisher or environment failure:** check the exact owner, repository, workflow and
  environment identities above, main-branch deployment permissions and required reviewers.
  Retry failed jobs from the original run after fixing the settings.
- **Broken published version:** yank it on PyPI and release a fix with a new version.
  Already published files cannot be replaced. Neither merging a fix nor pushing a tag
  republishes anything without a matching opt-in and environment approvals.

Release runs are serialized without cancelling active uploads. Workflow validation
and local builds do not exercise GitHub's event delivery, remote variable evaluation,
environment protection or PyPI OIDC exchange; those remain remote verification steps.
