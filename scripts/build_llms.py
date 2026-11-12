"""Generate the machine-readable docs for AI agents.

Writes ``docs/llms.txt`` (an index following https://llmstxt.org), ``docs/llms-full.txt``
(every page in one file), ``docs/reference/settings.schema.json`` (all settings with types
and defaults) and ``docs/reference/cli.json`` (every command and flag). MkDocs publishes them
at the site root. Output is deterministic.

    uv run python scripts/build_llms.py          # write
    uv run python scripts/build_llms.py --check  # exit 1 when a generated file is stale
"""

from __future__ import annotations

import argparse
import dataclasses
import json
import re
import sys
import typing
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DOCS = ROOT / "docs"
RAW = "https://raw.githubusercontent.com/Pythondeveloper6/redline/main/docs/"
SITE = "https://pythondeveloper6.github.io/redline/"


def read_config() -> tuple[str, str]:
    text = (ROOT / "mkdocs.yml").read_text(encoding="utf-8")
    description = re.search(r'^site_description:\s*"(.*)"\s*$', text, re.M)
    return "Redline", description.group(1) if description else ""


def nav_pages() -> list[tuple[str, str, str]]:
    """``[(section, title, file)]`` in nav order; nested groups keep their top section."""
    text = (ROOT / "mkdocs.yml").read_text(encoding="utf-8")
    nav = text.split("\nnav:\n", 1)[1]
    pages: list[tuple[str, str, str]] = []
    section = "Docs"
    for line in nav.splitlines():
        m = re.match(r"^( *)- (.+?):\s*(\S+\.md)\s*$", line)
        if m:
            indent = len(m.group(1))
            pages.append((section if indent > 2 else "Overview", m.group(2), m.group(3)))
            continue
        s = re.match(r"^  - ([^:]+):\s*$", line)  # a top-level group
        if s:
            section = s.group(1)
    return pages


def summary_of(markdown: str) -> str:
    if markdown.startswith("---\n"):  # front matter
        markdown = markdown.split("\n---\n", 1)[-1]
    skip = ("#", "!", "|", "```", ">", "-", "*", "<", "←", "[←", "{")
    paragraph: list[str] = []
    for line in markdown.splitlines():
        line = line.strip()
        if not line:
            if paragraph:
                break
            continue
        if line.startswith(skip) and not paragraph:
            continue
        paragraph.append(line)
    text = re.sub(r"\[([^\]]+)\]\([^)]*\)", r"\1", " ".join(paragraph)).replace("`", "")
    return re.split(r"(?<=[.!?])\s", text, maxsplit=1)[0][:240]


def build_index() -> str:
    title, description = read_config()
    out = [f"# {title}", "", f"> {description}", ""]
    out += [
        "Redline records real requests in Django, FastAPI, Flask and Litestar apps and reports "
        "N+1 queries, slow queries, blocking calls, and slow external or LLM calls. Agents can "
        "read its findings over MCP (`redline mcp`), get a fix brief (`redline fix-prompt`) and "
        "verify a fix (`redline check verify`). Prompt and completion text is never stored.",
        "",
        f"Full text of every page: {SITE}llms-full.txt",
        f"Settings JSON Schema: {SITE}reference/settings.schema.json",
        f"CLI reference as JSON: {SITE}reference/cli.json",
        "",
    ]
    current = None
    for section, page_title, file in nav_pages():
        if section != current:
            current = section
            out += ([] if out[-1] == "" else [""]) + [f"## {section}", ""]
        body = (DOCS / file).read_text(encoding="utf-8")
        line = f"- [{page_title}]({RAW}{file})"
        if summary_of(body):
            line += f": {summary_of(body)}"
        out.append(line)
    return "\n".join(out) + "\n"


def build_full() -> str:
    title, description = read_config()
    out = [f"# {title}", "", f"> {description}", ""]
    for _, page_title, file in nav_pages():
        body = (DOCS / file).read_text(encoding="utf-8").rstrip()
        out += [
            "",
            "---",
            "",
            f"<!-- source: {RAW}{file} -->",
            f"<!-- page: {page_title} -->",
            "",
            body,
        ]
    return "\n".join(out) + "\n"


def _json_type(tp: typing.Any) -> dict[str, typing.Any]:
    origin = typing.get_origin(tp)
    args = typing.get_args(tp)
    if origin in (typing.Union, getattr(__import__("types"), "UnionType", None)):
        non_null = [a for a in args if a is not type(None)]
        schema = _json_type(non_null[0]) if len(non_null) == 1 else {}
        return {**schema, "nullable": True} if len(non_null) != len(args) else schema
    if tp is bool:
        return {"type": "boolean"}
    if tp is int:
        return {"type": "integer"}
    if tp is float:
        return {"type": "number"}
    if tp is str:
        return {"type": "string"}
    if origin in (tuple, list):
        item = args[0] if args and args[0] is not Ellipsis else str
        return {"type": "array", "items": _json_type(item)}
    return {}


def build_settings_schema() -> str:
    sys.path.insert(0, str(ROOT / "packages" / "redline-core" / "src"))
    from redline_core.conf import Settings

    hints = typing.get_type_hints(Settings)
    props = {}
    for f in dataclasses.fields(Settings):
        schema = _json_type(hints[f.name])
        default = f.default if f.default is not dataclasses.MISSING else None
        if f.default_factory is not dataclasses.MISSING:  # type: ignore[misc]
            default = f.default_factory()  # type: ignore[misc]
        if isinstance(default, tuple):
            default = list(default)
        try:
            json.dumps(default)
            schema["default"] = default
        except TypeError:
            pass
        props[f.name] = schema
    doc = {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "title": "Redline settings",
        "type": "object",
        "properties": dict(sorted(props.items())),
        "additionalProperties": False,
    }
    return json.dumps(doc, indent=2, sort_keys=False) + "\n"


def build_cli() -> str:
    sys.path.insert(0, str(ROOT / "packages" / "redline-core" / "src"))
    from redline_core import cli

    def describe(parser: argparse.ArgumentParser) -> list[dict[str, typing.Any]]:
        flags = []
        for action in parser._actions:
            if isinstance(action, (argparse._HelpAction, argparse._SubParsersAction)):
                continue
            if action.option_strings:
                required = bool(getattr(action, "required", False))
            else:
                # argparse sets `required` on positionals differently across Python versions
                # (REMAINDER and "*" differ between 3.10 and 3.13); derive it from nargs.
                required = action.nargs not in ("?", "*", argparse.REMAINDER)
            flags.append(
                {
                    "name": action.option_strings or [action.dest],
                    "required": required,
                    "help": (action.help or "").replace("%(default)s", str(action.default)),
                }
            )
        return flags

    commands = []
    for name, spec in sorted(cli.COMMANDS.items()):
        parser = argparse.ArgumentParser(prog=f"redline {name}")
        spec[1](parser)
        entry = {"command": f"redline {name}", "help": spec[0], "arguments": describe(parser)}
        subs = []
        for sub, (help_text, configure, _handler) in sorted(spec[3].items()):
            sp = argparse.ArgumentParser(prog=f"redline {name} {sub}")
            configure(sp)
            subs.append(
                {"command": f"redline {name} {sub}", "help": help_text, "arguments": describe(sp)}
            )
        if subs:
            entry["subcommands"] = subs
        commands.append(entry)
    check = []
    for sub, (help_text, configure, _handler) in sorted(cli.CHECK_COMMANDS.items()):
        sp = argparse.ArgumentParser(prog=f"redline check {sub}")
        configure(sp)
        check.append(
            {"command": f"redline check {sub}", "help": help_text, "arguments": describe(sp)}
        )
    return json.dumps({"commands": commands, "check": check}, indent=2) + "\n"


def outputs() -> dict[Path, str]:
    return {
        DOCS / "llms.txt": build_index(),
        DOCS / "llms-full.txt": build_full(),
        DOCS / "reference" / "settings.schema.json": build_settings_schema(),
        DOCS / "reference" / "cli.json": build_cli(),
    }


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--check", action="store_true", help="fail when generated files are stale")
    args = parser.parse_args(argv)
    stale = []
    for path, text in outputs().items():
        current = path.read_text(encoding="utf-8") if path.exists() else None
        if current != text:
            stale.append(path.relative_to(ROOT))
            if not args.check:
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(text, encoding="utf-8")
    if args.check and stale:
        print("stale (run: uv run python scripts/build_llms.py): " + ", ".join(map(str, stale)))
        return 1
    print("up to date" if args.check else f"wrote {len(outputs())} files")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
