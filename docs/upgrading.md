# Upgrading

Redline is pre-1.0: the public API may change between minor versions, and every change is
listed in the [changelog](changelog.md). The five packages (`redline-core`, `django-redline`,
`fastapi-redline`, `flask-redline`, `litestar-redline`) are versioned in lockstep, so upgrade
them together.

## Steps

1. Read the [changelog](changelog.md) entries between your version and the target, in
   particular the *Changed* and *Security* sections.
2. Upgrade the framework package and `redline-core` in the same command:

    ```bash
    pip install --pre --upgrade django-redline   # or fastapi-redline, flask-redline, litestar-redline
    ```

3. Run the configuration check. It loads your settings without changing them and reports
   anything deprecated or unsafe for recording-only deployments:

    ```bash
    redline doctor --production --strict
    ```

4. Restart every worker. The recorder is per process; mixed versions against one store work
   but report their own version in the Meta tab.
5. Open the UI and confirm new traces arrive.

## The trace store

The SQLite store migrates itself in place the first time a newer version opens it, in one
transaction, so concurrent workers start safely. Older traces stay readable. A store written
by a newer version is never opened by an older one; if you must roll back, point the older
version at a fresh `store_path` or delete the file. See [Storage](storage.md) and
[History and releases](history.md#upgrading-an-existing-store).

## Pre-releases

Alpha builds are published as pre-releases and need `--pre`. Pin an exact version in
production (`django-redline==X.Y.ZaN`) and upgrade deliberately. Anything marked experimental
in [API stability](api-stability.md) can change without notice.
