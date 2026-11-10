"""Data access static analysis test cases."""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING

from liquid import Environment
from liquid.access_analysis import EACH
from liquid.access_analysis import UNKNOWN_SEGMENT
from liquid.access_analysis import AccessOriginKind
from liquid.access_analysis import DataAccessAnalysis
from liquid.access_analysis import UnknownReason
from liquid.builtin import DictLoader

if TYPE_CHECKING:
    from liquid import BoundTemplate


def _analyze(template: BoundTemplate, **kwargs: object) -> DataAccessAnalysis:
    """Assert sync and async analysis agree, then return the result."""
    sync = template.analyze_data_access(**kwargs)  # type: ignore[arg-type]

    async def coro() -> DataAccessAnalysis:
        return await template.analyze_data_access_async(
            **kwargs,  # type: ignore[arg-type]
        )

    assert asyncio.run(coro()) == sync
    return sync


def _kinds(access):  # noqa: ANN001, ANN202
    return tuple(origin.kind for origin in access.origin.chain())


def test_root_input_access() -> None:
    env = Environment()
    template = env.from_string("{{ item.email }}")
    result = _analyze(template)

    assert len(result.accesses) == 1
    access = result.accesses[0]
    assert access.text == "item.email"
    assert access.path.segments == ("item", "email")
    assert _kinds(access) == (AccessOriginKind.ROOT_INPUT,)
    assert result.unknowns == ()
    assert result.root_input_paths() == ["item.email"]


def test_local_shadows_root_input() -> None:
    env = Environment()
    template = env.from_string(
        "{{ item.email }}"
        "{% assign item = 'not a mapping' %}"
        "{{ item.email }}"
    )
    result = _analyze(template)

    first, second = result.accesses
    # Before the assignment, `item` is root input data.
    assert _kinds(first) == (AccessOriginKind.ROOT_INPUT,)
    # After the assignment, the local shadows the root input. The value is
    # opaque, so the path stays rooted at the local name.
    assert _kinds(second) == (AccessOriginKind.LOCAL,)
    assert second.path.segments == ("item", "email")
    # The shadowed access must not be reported as root input.
    assert result.root_input_paths() == ["item.email"]


def test_assign_alias_tracks_source() -> None:
    env = Environment()
    template = env.from_string("{% assign item = user %}{{ item.email }}")
    result = _analyze(template)

    access = result.accesses[-1]
    assert access.path.segments == ("user", "email")
    assert _kinds(access) == (
        AccessOriginKind.LOCAL,
        AccessOriginKind.ROOT_INPUT,
    )


def test_loop_variable_source() -> None:
    env = Environment()
    template = env.from_string(
        "{% for item in items %}{{ item.email }}{% endfor %}"
    )
    result = _analyze(template)

    access = result.accesses[-1]
    assert access.path.segments == ("items", EACH, "email")
    assert str(access.path) == "items[*].email"
    assert _kinds(access) == (
        AccessOriginKind.LOOP_VARIABLE,
        AccessOriginKind.ROOT_INPUT,
    )


def test_include_shared_scope() -> None:
    env = Environment(
        loader=DictLoader(
            {
                "main": (
                    "{% assign customer = account.owner %}"
                    "{% for item in items %}"
                    "{% include 'card', product: item %}"
                    "{% endfor %}"
                ),
                "card": "{{ product.title }} {{ customer.email }}",
            }
        )
    )
    template = env.get_template("main")
    result = _analyze(template)

    by_text = {}
    for access in result.accesses:
        by_text.setdefault(access.text, []).append(access)

    # The partial argument resolves through the caller's loop variable.
    title = by_text["product.title"][0]
    assert title.path.segments == ("items", EACH, "title")
    assert _kinds(title) == (
        AccessOriginKind.PARTIAL_ARGUMENT,
        AccessOriginKind.LOOP_VARIABLE,
        AccessOriginKind.ROOT_INPUT,
    )

    # `include` shares the caller's scope, so the assigned `customer` local
    # is visible inside the partial and resolves to its source.
    customer = by_text["customer.email"][0]
    assert customer.path.segments == ("account", "owner", "email")
    assert _kinds(customer) == (
        AccessOriginKind.LOCAL,
        AccessOriginKind.ROOT_INPUT,
    )


def test_render_isolated_scope() -> None:
    env = Environment(
        loader=DictLoader(
            {
                "main": (
                    "{% assign customer = account.owner %}"
                    "{% for item in items %}"
                    "{% render 'card', product: item %}"
                    "{% endfor %}"
                ),
                "card": "{{ product.title }} {{ customer.email }}",
            }
        )
    )
    template = env.get_template("main")
    result = _analyze(template)

    by_text = {}
    for access in result.accesses:
        by_text.setdefault(access.text, []).append(access)

    title = by_text["product.title"][0]
    assert title.path.segments == ("items", EACH, "title")
    assert _kinds(title) == (
        AccessOriginKind.PARTIAL_ARGUMENT,
        AccessOriginKind.LOOP_VARIABLE,
        AccessOriginKind.ROOT_INPUT,
    )

    # `render` isolates the partial from the caller's template locals, so
    # `customer` inside the partial is root input, not the assigned local.
    customer = by_text["customer.email"][0]
    assert customer.path.segments == ("customer", "email")
    assert _kinds(customer) == (AccessOriginKind.ROOT_INPUT,)


def test_same_partial_with_different_arguments() -> None:
    env = Environment(
        loader=DictLoader(
            {
                "main": (
                    "{% render 'card', product: a %}"
                    "{% render 'card', product: b %}"
                ),
                "card": "{{ product.title }}",
            }
        )
    )
    template = env.get_template("main")
    result = _analyze(template)

    titles = [a for a in result.accesses if a.text == "product.title"]
    paths = {access.path.segments for access in titles}
    assert ("a", "title") in paths
    assert ("b", "title") in paths


def test_recursive_partial() -> None:
    env = Environment(
        loader=DictLoader(
            {
                "main": "{% render 'tree', node: root %}",
                "tree": "{{ node.name }}{% render 'tree', node: node.child %}",
            }
        )
    )
    template = env.get_template("main")
    result = _analyze(template, max_partial_depth=3)

    # Recursion terminates and is reported as an explicit unknown range.
    assert len(result.unknowns) == 1
    assert result.unknowns[0].reason == UnknownReason.RECURSION_LIMIT

    # Accesses up to the depth limit are still listed.
    roots = result.root_input_paths()
    assert "root.name" in roots
    assert "root.child.name" in roots


def test_accesses_in_branches_are_listed() -> None:
    env = Environment()
    template = env.from_string(
        "{% if user.admin %}"
        "{{ audit.secret }}"
        "{% else %}"
        "{{ session.token }}"
        "{% endif %}"
        "{% unless user.active %}{{ account.plan }}{% endunless %}"
    )
    result = _analyze(template)

    roots = result.root_input_paths()
    assert "audit.secret" in roots
    assert "session.token" in roots
    assert "account.plan" in roots


def test_dynamic_subscript_is_an_explicit_unknown() -> None:
    env = Environment()
    template = env.from_string("{{ x[y.z] }}")
    result = _analyze(template)

    by_text = {access.text: access for access in result.accesses}
    outer = by_text["x[y.z]"]
    # The dynamic segment is marked unknown, not fabricated or omitted.
    assert outer.path.segments == ("x", UNKNOWN_SEGMENT)
    assert str(outer.path) == "x[?]"
    # The subscript expression itself is still reported.
    assert by_text["y.z"].path.segments == ("y", "z")


def test_dynamic_partial_name_is_an_explicit_unknown() -> None:
    env = Environment()
    template = env.from_string("{% include tmpl %}")
    result = _analyze(template)

    assert len(result.unknowns) == 1
    unknown = result.unknowns[0]
    assert unknown.reason == UnknownReason.DYNAMIC_PARTIAL_NAME
    assert "tmpl" in unknown.detail
    # The name expression itself is still a reported data access.
    assert any(access.text == "tmpl" for access in result.accesses)


def test_missing_partial_is_an_explicit_unknown() -> None:
    env = Environment(loader=DictLoader({}))
    template = env.from_string("{% include 'nowhere' %}")
    result = _analyze(template)

    assert len(result.unknowns) == 1
    assert result.unknowns[0].reason == UnknownReason.MISSING_PARTIAL


def test_analysis_does_not_evaluate_drops_or_filters() -> None:
    calls = {"filter": 0, "drop": 0}

    class CountingDrop:
        def __getattr__(self, _name: str) -> str:
            calls["drop"] += 1
            return "unreachable"

    def counting_filter(value: object) -> object:
        calls["filter"] += 1
        return value

    env = Environment(globals={"drop": CountingDrop()})
    env.add_filter("counting", counting_filter)
    template = env.from_string("{{ drop.secret | counting }}")

    result = _analyze(template)
    assert calls == {"filter": 0, "drop": 0}
    assert result.accesses[0].path.segments == ("drop", "secret")


def test_include_partials_false() -> None:
    env = Environment(
        loader=DictLoader(
            {
                "main": "{% render 'card', product: a %}",
                "card": "{{ product.title }}",
            }
        )
    )
    template = env.get_template("main")
    result = _analyze(template, include_partials=False)

    assert all(access.text != "product.title" for access in result.accesses)


def test_builtin_variables_are_not_root_input() -> None:
    env = Environment()
    template = env.from_string(
        "{% for item in items %}{{ forloop.index }}{% endfor %}"
        "{% increment counter %}"
    )
    result = _analyze(template)

    by_text = {access.text: access for access in result.accesses}
    assert _kinds(by_text["forloop.index"]) == (AccessOriginKind.BUILTIN,)
    assert "forloop.index" not in result.root_input_paths()


def test_existing_analysis_interface_is_unchanged() -> None:
    env = Environment()
    template = env.from_string("{% assign x = y %}{{ x }}")
    analysis = template.analyze()

    # The pre-existing result type is untouched by data access analysis.
    assert not hasattr(analysis, "accesses")
    assert set(analysis.globals) == {"y"}
    assert set(analysis.locals) == {"x"}
