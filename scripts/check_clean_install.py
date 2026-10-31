"""Install the distributions into a brand-new virtualenv and smoke-test them.

Unlike CI's ``package`` job (which installs wheels it just built), this reproduces what a
user gets: a fresh venv with no project site-packages, every dependency resolved from the
network, prereleases allowed with ``--pre``.

    # what PyPI serves today
    python scripts/check_clean_install.py --from-pypi
    # an exact released version
    python scripts/check_clean_install.py --from-pypi --version 0.5.0a1
    # locally built wheels (uv build --all-packages --out-dir dist), deps from PyPI
    python scripts/check_clean_install.py --wheels dist

Each distribution is installed into its *own* venv (with its framework extra where it
has one) so a missing dependency in one adapter cannot be masked by another. In each venv
the script imports the modules, runs ``redline --help`` and ``scripts/smoke_wheels.py``-style
checks for the bundled UI. With ``--json`` it prints a machine-readable result.
Exit status 0 = every install resolved and imported.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import tempfile
import venv
from pathlib import Path

#: distribution -> (module, extra pip requirements a user would also have)
DISTRIBUTIONS: dict[str, tuple[str, tuple[str, ...]]] = {
    "redline-core": ("redline_core", ()),
    "django-redline": ("django_redline", ()),
    "fastapi-redline": ("fastapi_redline", ()),
    "flask-redline": ("flask_redline", ()),
    "litestar-redline": ("litestar_redline", ()),
}

SMOKE = """
import importlib, importlib.resources as res, sys
mod = importlib.import_module(sys.argv[1])
core = importlib.import_module("redline_core")
assert "site-packages" in (mod.__file__ or ""), mod.__file__
assert res.files("redline_core").joinpath("ui/static/index.html").is_file(), "UI bundle missing"
assert res.files(sys.argv[1]).joinpath("py.typed").is_file(), "py.typed missing"
print(getattr(mod, "__version__", "?"), getattr(core, "__version__", "?"))
"""


def _run(cmd: list[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(cmd, capture_output=True, text=True, check=False)


def _requirement(dist: str, args: argparse.Namespace) -> list[str]:
    if args.wheels:
        wheel_dir = Path(args.wheels).resolve()
        norm = dist.replace("-", "_")
        wheels = sorted(wheel_dir.glob(f"{norm}-*.whl"))
        core = sorted(wheel_dir.glob("redline_core-*.whl"))
        if not wheels:
            raise SystemExit(f"no wheel for {dist} in {wheel_dir}")
        # The core wheel is passed too so an adapter resolves *this* core, not PyPI's.
        return [str(p) for p in {*wheels[-1:], *core[-1:]}]
    return [f"{dist}=={args.version}" if args.version else dist]


def check(dist: str, args: argparse.Namespace, workdir: Path) -> dict[str, object]:
    module, extra = DISTRIBUTIONS[dist]
    env_dir = workdir / dist
    venv.EnvBuilder(with_pip=True, clear=True).create(env_dir)
    python = str(env_dir / ("Scripts" if sys.platform == "win32" else "bin") / "python")
    result: dict[str, object] = {"distribution": dist, "ok": False}
    install = [python, "-m", "pip", "install", "--disable-pip-version-check", "-q"]
    if args.pre:
        install.append("--pre")
    proc = _run([*install, *_requirement(dist, args), *extra])
    if proc.returncode != 0:
        result["error"] = "install failed: " + (proc.stderr or proc.stdout)[-2000:]
        return result
    proc = _run([python, "-I", "-c", SMOKE, module])
    if proc.returncode != 0:
        result["error"] = "import failed: " + proc.stderr[-2000:]
        return result
    result["versions"] = proc.stdout.split()
    cli = _run([python, "-I", "-m", "redline_core.cli", "--help"])
    if cli.returncode != 0 or "redline" not in cli.stdout:
        result["error"] = "redline --help failed: " + cli.stderr[-2000:]
        return result
    freeze = _run([python, "-m", "pip", "check"])
    if freeze.returncode != 0:
        result["error"] = "pip check: " + freeze.stdout[-2000:]
        return result
    result["ok"] = True
    return result


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--from-pypi", action="store_true", help="install from the index")
    source.add_argument("--wheels", metavar="DIR", help="install local wheels from DIR")
    parser.add_argument("--version", help="exact version to install with --from-pypi")
    parser.add_argument(
        "--no-pre", dest="pre", action="store_false", help="do not allow prereleases"
    )
    parser.add_argument("--only", action="append", choices=sorted(DISTRIBUTIONS))
    parser.add_argument("--json", action="store_true", help="print JSON results")
    args = parser.parse_args(argv)

    results = []
    with tempfile.TemporaryDirectory(prefix="redline-clean-install-") as tmp:
        for dist in args.only or DISTRIBUTIONS:
            results.append(check(dist, args, Path(tmp)))
            if not args.json:
                r = results[-1]
                status = "ok  " if r["ok"] else "FAIL"
                detail = " ".join(r.get("versions", [])) or r.get("error", "")  # type: ignore[arg-type]
                print(f"{status} {dist}: {detail}")
    if args.json:
        print(json.dumps(results, indent=2))
    return 0 if all(r["ok"] for r in results) else 1


if __name__ == "__main__":
    sys.exit(main())
