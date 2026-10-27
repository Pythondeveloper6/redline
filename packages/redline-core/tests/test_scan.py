from __future__ import annotations

from pathlib import Path

import pytest

from redline_core.scan import (
    RelationInfo,
    format_table,
    jinja_search_paths,
    scan_jinja_loader,
    scan_source,
    scan_templates,
)


def _chains(src, dialect="django", **kw):
    return [(f.line, f.chain) for f in scan_source(src, "t.html", dialect, **kw)]


class Resolver:
    def __init__(self, **rels):
        self.rels = rels

    def is_relation(self, first_hop):
        return self.rels.get(first_hop)


FK = RelationInfo("fk", "shop.Customer", "select_related")
REV = RelationInfo("reverse_fk", "shop.OrderItem", "prefetch_related")


def test_basic_fk_chain():
    src = "{% for o in orders %}\n  {{ o.customer.name }}\n{% endfor %}"
    [f] = scan_source(src, "a.html")
    assert (f.file, f.line, f.for_line, f.loop_var, f.chain) == (
        "a.html", 2, 1, "o", "o.customer.name"
    )  # fmt: skip
    assert f.kind == "template"
    assert f.first_hop == "customer"
    assert f.to_dict()["chain"] == "o.customer.name"


def test_single_hop_and_outside_loop_ignored():
    assert _chains("{% for o in orders %}{{ o.name }}{% endfor %}{{ o.customer.name }}") == []


def test_queryset_hops_are_high_severity():
    src = "{% for o in x %}{{ o.items.all }}{{ o.items.count }}{{ o.customer.name }}{% endfor %}"
    sev = {f.chain: f.severity for f in scan_source(src)}
    assert sev == {
        "o.items.all": "high", "o.items.count": "high", "o.customer.name": "medium"
    }  # fmt: skip


def test_value_attr_low():
    [f] = scan_source("{% for o in x %}{{ o.created.year }}{% endfor %}")
    assert f.severity == "low"


def test_filters_and_filter_args():
    src = "{% for o in x %}{{ o.customer.name|upper }}{{ a|default:o.customer.email }}{% endfor %}"
    assert [c for _, c in _chains(src)] == ["o.customer.email", "o.customer.name"]


def test_comments_ignored():
    src = (
        "{% for o in x %}{# {{ o.customer.name }} #}"
        "{% comment %}\n{{ o.a.b }}\n{% for q in z %}{% endcomment %}"
        "{{ o.b.c }}{% endfor %}"
    )
    assert _chains(src) == [(3, "o.b.c")]


def test_verbatim_and_raw():
    dj = "{% for o in x %}{% verbatim %}{{ o.a.b }}{% endverbatim %}{{ o.c.d }}{% endfor %}"
    assert _chains(dj) == [(1, "o.c.d")]
    jj = "{% for o in x %}{% raw %}{{ o.a.b }}{% endraw %}{{ o.c.d }}{% endfor %}"
    assert _chains(jj, "jinja") == [(1, "o.c.d")]


def test_jinja_comment_spans_lines_django_does_not():
    src = "{% for o in x %}{# a\n {{ o.a.b }} #}{% endfor %}"
    assert _chains(src, "jinja") == []
    assert _chains(src, "django") == [(2, "o.a.b")]


def test_nested_loops_inner_var_and_path():
    src = (
        "{% for o in orders %}\n"
        "{% for i in o.items.all %}\n"
        "{{ i.product.name }}\n"
        "{% endfor %}\n"
        "{{ i.product.name }}\n"  # i is out of scope here
        "{% endfor %}"
    )
    fs = scan_source(src, resolver=Resolver(items=REV, product=FK))
    by = {f.chain: f for f in fs}
    assert set(by) == {"o.items.all", "i.product.name"}
    assert by["o.items.all"].suggestion == 'prefetch_related("items")'
    assert by["o.items.all"].line == 2 and by["o.items.all"].for_line == 1
    inner = by["i.product.name"]
    assert (inner.line, inner.for_line) == (3, 2)
    assert inner.suggestion == 'prefetch_related("items__product")'


def test_unpacking_targets():
    src = "{% for k, v in d.items %}{{ v.customer.name }}{% endfor %}"
    assert [c for _, c in _chains(src)] == ["v.customer.name"]


def test_with_alias_forms():
    src = (
        "{% for o in x %}{% with c=o.customer n=o.name %}{{ c.name }}{{ n }}{% endwith %}"
        "{% with o.customer as c2 %}{{ c2.email }}{% endwith %}{{ c.name }}{% endfor %}"
    )
    assert [c for _, c in _chains(src)] == ["o.customer.email", "o.customer.name"]


def test_with_shadows_loop_var():
    src = "{% for o in x %}{% with o=other %}{{ o.a.b }}{% endwith %}{{ o.a.b }}{% endfor %}"
    assert len(_chains(src)) == 1


def test_jinja_set_and_subscript_and_macro():
    src = (
        "{% for o in x %}{% set c = o.customer %}{{ c.name }}{{ o['owner'].name }}"
        "{{ o['a']['b'] }}{% endfor %}"
        "{% macro m(o) %}{{ o.a.b }}{% endmacro %}"
    )
    assert [c for _, c in _chains(src, "jinja")] == [
        "o.a.b", "o.customer.name", "o.owner.name"
    ]  # fmt: skip


def test_jinja_trim_markers_and_inline_if():
    src = "{%- for o in x if o.owner.active -%}\n{{- o.customer.name -}}\n{%- endfor -%}"
    assert _chains(src, "jinja") == [(1, "o.owner.active"), (2, "o.customer.name")]


def test_strings_not_scanned():
    src = """{% for o in x %}{{ "o.a.b" }}{% trans 'o.c.d' %}{% url 'v' o.e.f %}{% endfor %}"""
    assert _chains(src) == [(1, "o.e.f")]


def test_tag_args_scanned():
    src = "{% for o in x %}{% if o.customer.vip %}y{% elif o.owner.x %}z{% endif %}{% endfor %}"
    assert [c for _, c in _chains(src)] == ["o.customer.vip", "o.owner.x"]


def test_for_else_empty_and_reuse_of_name_after_loop():
    src = "{% for o in x %}{{ o.a.b }}{% empty %}none{% endfor %}{{ o.a.b }}"
    assert len(_chains(src)) == 1


def test_resolver_filters_and_annotates():
    src = "{% for o in x %}{{ o.customer.name }}{{ o.created.year }}{{ o.items.count }}{% endfor %}"
    fs = scan_source(src, resolver=Resolver(customer=FK, items=REV))
    assert [f.chain for f in fs] == ["o.customer.name", "o.items.count"]
    assert fs[0].suggestion == 'select_related("customer")'
    assert fs[0].relation == "fk" and fs[0].model == "shop.Customer"
    assert fs[1].suggestion == 'prefetch_related("items")'


def test_duplicates_collapsed():
    src = "{% for o in x %}{{ o.a.b }} {{ o.a.b }}{% endfor %}"
    assert len(_chains(src)) == 1


@pytest.mark.parametrize(
    "src",
    [
        "{% for %}{{ o.a.b }}",
        "{% for o in %}{% endfor",
        "{% endfor %}{% endwith %}{% endif %}",
        "{% for o in x %}{{ o.a.b ",
        "{% comment %} never closed {{ o.a.b }}",
        "{{ }}{% %}{# #}{%%}",
        "{% with %}{% with = %}{% set %}{% macro %}{% for o in x %}",
        "",
    ],
)
@pytest.mark.parametrize("dialect", ["django", "jinja"])
def test_malformed_never_crashes(src, dialect):
    assert isinstance(scan_source(src, dialect=dialect), list)


def test_scan_templates_files_dirs_and_warnings(tmp_path: Path):
    (tmp_path / "sub").mkdir()
    (tmp_path / "sub" / "a.html").write_text("{% for o in x %}{{ o.a.b }}{% endfor %}")
    (tmp_path / "b.txt").write_text("{% for o in x %}\n{{ o.a.b }}{% endfor %}")
    (tmp_path / "c.png").write_bytes(b"\x89PNG")
    (tmp_path / ".git").mkdir()
    (tmp_path / ".git" / "d.html").write_text("{% for o in x %}{{ o.a.b }}{% endfor %}")
    (tmp_path / "bad.html").write_bytes(b"{% for o in x %}{{ o.a.b }}\xff\xfe{% endfor %}")
    warnings: list[str] = []
    fs = scan_templates([tmp_path, tmp_path / "b.txt", tmp_path / "nope"], warnings=warnings)
    assert sorted(Path(f.file).name for f in fs) == ["a.html", "b.txt", "bad.html"]
    assert any("nope" in w for w in warnings)
    # single path, non-list
    assert len(scan_templates(tmp_path / "b.txt")) == 1


def test_unknown_dialect():
    with pytest.raises(ValueError):
        scan_templates([], "mako")


def test_unreadable_file_is_a_warning(tmp_path: Path, monkeypatch):
    f = tmp_path / "a.html"
    f.write_text("x")

    def boom(self, *a, **k):
        raise OSError("denied")

    monkeypatch.setattr(Path, "read_text", boom)
    warnings: list[str] = []
    assert scan_templates([f], warnings=warnings) == []
    assert "denied" in warnings[0]


def test_jinja_search_paths_duck_typing(tmp_path: Path):
    a, b = tmp_path / "a", tmp_path / "b"
    a.mkdir()
    b.mkdir()
    (a / "t.html").write_text("{% for o in x %}{{ o['c'].n }}{% endfor %}")

    class FS:
        searchpath = [str(a), str(tmp_path / "missing")]

    class Pkg:
        _template_root = b

    class Choice:
        loaders = [FS(), Pkg()]

    class Prefix:
        mapping = {"x": FS()}

    class Env:
        loader = Choice()
        globals: dict = {}

    assert jinja_search_paths(Env()) == [str(a), str(b)]
    assert jinja_search_paths([Prefix(), None]) == [str(a)]
    assert [f.chain for f in scan_jinja_loader(FS())] == ["o.c.n"]


def test_format_table_aligned():
    src = "{% for o in x %}{{ o.customer.name }}\n{{ o.items.all }}{% endfor %}"
    out = format_table(scan_source(src, "t.html")).splitlines()
    assert out[0].split() == ["LOCATION", "KIND", "SEVERITY", "CHAIN", "SUGGESTION"]
    assert out[1].startswith("t.html:1")
    assert out[1].index("template") == out[2].index("template")
    assert format_table([]) == ""
