"""Empirical characterization of `loop_iteration_limit`.

These tests pin down what python-liquid's `loop_iteration_limit` actually
bounds. It is not a global work budget for an entire render. At the point a
loop (`for`, `tablerow`, or the loop form of `render`/`include`) is entered,
the limit is compared against the multiplicative product of the *declared*
lengths of every loop on the current render context's loop stack, including
lengths carried across `render` context copies via
`carry_loop_iterations`. See `liquid.context.RenderContext.raise_for_loop_limit`.

Consequences exercised below:

- Sibling loops each get the full budget; the counter is not cumulative.
- Nested loops are bounded by the product of their declared lengths.
- A partial reached through `render` (which copies the context) keeps the
  parent's count, so the documented guarantee survives the copy.
- `break` and `continue` do not reduce the declared length used in the check;
  a loop that would break immediately still raises before its body runs.
- Work performed outside of a loop tag is not metered at all.

Every case is rendered both synchronously and asynchronously; both code paths
share the same check in `RenderContext.raise_for_loop_limit`.
"""

from __future__ import annotations

import asyncio
from typing import Any
from typing import Mapping
from typing import Optional

import pytest

from liquid import DictLoader
from liquid import Environment
from liquid.exceptions import LoopIterationLimitError

LIMIT = 100


class LimitEnvironment(Environment):
    loop_iteration_limit = LIMIT


def _environment(
    partials: Optional[Mapping[str, str]] = None,
) -> tuple[Environment, dict[str, int]]:
    env = LimitEnvironment(loader=DictLoader(partials or {}))
    entries = {"count": 0}

    def tick(value: object) -> object:
        entries["count"] += 1
        return value

    env.add_filter("tick", tick)
    return env, entries


def _render(
    source: str,
    partials: Optional[Mapping[str, str]] = None,
    asynchronous: bool = False,
    **data: Any,
) -> int:
    env, entries = _environment(partials)
    template = env.from_string(source)
    if asynchronous:
        asyncio.run(template.render_async(**data))
    else:
        template.render(**data)
    return entries["count"]


@pytest.fixture(params=[False, True], ids=["sync", "async"])
def asynchronous(request: pytest.FixtureRequest) -> bool:
    return bool(request.param)


def test_sequential_sibling_loops_each_get_full_budget(asynchronous):
    # Two sibling loops of 60 iterations each: 120 body entries in total,
    # but neither loop stack ever exceeds a single length of 60.
    source = (
        "{% for i in (1..60) %}{{ i | tick }}{% endfor %}"
        "{% for i in (1..60) %}{{ i | tick }}{% endfor %}"
    )
    assert _render(source, asynchronous=asynchronous) == 120


def test_nested_product_at_limit_is_allowed(asynchronous):
    source = (
        "{% for i in (1..10) %}"
        "{% for j in (1..10) %}{{ j | tick }}{% endfor %}"
        "{% endfor %}"
    )
    assert _render(source, asynchronous=asynchronous) == LIMIT


def test_nested_product_over_limit_raises_before_body_runs(asynchronous):
    source = (
        "{% for i in (1..11) %}"
        "{% for j in (1..10) %}{{ j | tick }}{% endfor %}"
        "{% endfor %}"
    )
    with pytest.raises(LoopIterationLimitError):
        _render(source, asynchronous=asynchronous)


def test_include_inside_loop_shares_loop_stack(asynchronous):
    # `include` extends the current context rather than copying it, so the
    # partial's loop is nested on the same loop stack: 10 * 20 = 200.
    source = "{% for i in (1..10) %}{% include 'partial' %}{% endfor %}"
    partials = {
        "partial": "{% for j in (1..20) %}{{ j | tick }}{% endfor %}"
    }
    with pytest.raises(LoopIterationLimitError):
        _render(source, partials, asynchronous=asynchronous)


def test_include_alone_is_checked_against_full_limit(asynchronous):
    # Even with no enclosing loop, the partial's own loop is metered.
    source = "{% include 'partial' %}"
    partials = {
        "partial": "{% for j in (1..150) %}{{ j | tick }}{% endfor %}"
    }
    with pytest.raises(LoopIterationLimitError):
        _render(source, partials, asynchronous=asynchronous)


def test_include_for_inside_loop_uses_product(asynchronous):
    # The `include ... for` loop and the outer `for` multiply: 10 * 10 = 100.
    source = "{% for i in (1..10) %}{% include 'partial' for items %}{% endfor %}"
    partials = {"partial": "{{ item | tick }}"}
    entries = _render(
        source, partials, asynchronous=asynchronous, items=list(range(10))
    )
    assert entries == LIMIT


def test_render_copies_context_but_carries_loop_count(asynchronous):
    # `render` copies the render context, yet carry_loop_iterations=True means
    # the outer length of 10 is carried in; the partial's loop of 20 yields a
    # product of 200 even though the partial alone would be within budget.
    source = "{% for i in (1..10) %}{% render 'partial' %}{% endfor %}"
    partials = {
        "partial": "{% for j in (1..20) %}{{ j | tick }}{% endfor %}"
    }
    with pytest.raises(LoopIterationLimitError):
        _render(source, partials, asynchronous=asynchronous)


def test_render_alone_resets_to_full_budget(asynchronous):
    # With no enclosing loop there is nothing to carry, so a standalone
    # rendered partial gets the whole budget and fails on its own length.
    source = "{% render 'partial' %}"
    partials = {
        "partial": "{% for j in (1..150) %}{{ j | tick }}{% endfor %}"
    }
    with pytest.raises(LoopIterationLimitError):
        _render(source, partials, asynchronous=asynchronous)


def test_sequential_renders_each_get_full_budget(asynchronous):
    # Five sibling renders, each running a 50-iteration loop, perform 250 body
    # entries overall because each render's loop stack resets once its parent
    # has no active loop.
    source = "{% render 'partial' %}" * 5
    partials = {
        "partial": "{% for j in (1..50) %}{{ j | tick }}{% endfor %}"
    }
    assert _render(source, partials, asynchronous=asynchronous) == 250


def test_render_for_uses_bound_collection_length(asynchronous):
    source = "{% render 'partial' for items %}"
    partials = {"partial": "{{ item | tick }}"}
    assert (
        _render(
            source,
            partials,
            asynchronous=asynchronous,
            items=list(range(LIMIT)),
        )
        == LIMIT
    )
    with pytest.raises(LoopIterationLimitError):
        _render(
            source,
            partials,
            asynchronous=asynchronous,
            items=list(range(LIMIT + 1)),
        )


def test_render_for_inside_loop_uses_product(asynchronous):
    source = "{% for i in (1..10) %}{% render 'partial' for items %}{% endfor %}"
    partials = {"partial": "{{ item | tick }}"}
    entries = _render(
        source, partials, asynchronous=asynchronous, items=list(range(10))
    )
    assert entries == LIMIT

    over = (
        "{% for i in (1..11) %}{% render 'partial' for items %}{% endfor %}"
    )
    with pytest.raises(LoopIterationLimitError):
        _render(
            over,
            partials,
            asynchronous=asynchronous,
            items=list(range(10)),
        )


def test_nested_renders_multiply_across_context_copies(asynchronous):
    # Outer 10 -> rendered partial's loop 10 -> second render's loop 10.
    # The carry propagates through both context copies: 10 * 10 * 10 = 1000.
    source = "{% for i in (1..10) %}{% render 'foo' %}{% endfor %}"
    partials = {
        "foo": "{% for j in (1..10) %}{% render 'bar' %}{% endfor %}",
        "bar": "{% for k in (1..10) %}{{ k | tick }}{% endfor %}",
    }
    with pytest.raises(LoopIterationLimitError):
        _render(source, partials, asynchronous=asynchronous)

    # The same chain sized so the product equals the limit renders fully.
    ok_source = "{% for i in (1..5) %}{% render 'foo' %}{% endfor %}"
    ok_partials = {
        "foo": "{% for j in (1..5) %}{% render 'bar' %}{% endfor %}",
        "bar": "{% for k in (1..4) %}{{ k | tick }}{% endfor %}",
    }
    assert (
        _render(ok_source, ok_partials, asynchronous=asynchronous) == LIMIT
    )


def test_tablerow_flat_and_nested(asynchronous):
    with pytest.raises(LoopIterationLimitError):
        _render(
            "{% tablerow i in (1..150) %}{{ i | tick }}{% endtablerow %}",
            asynchronous=asynchronous,
        )

    nested = (
        "{% for i in (1..11) %}"
        "{% tablerow j in (1..10) %}{{ j | tick }}{% endtablerow %}"
        "{% endfor %}"
    )
    with pytest.raises(LoopIterationLimitError):
        _render(nested, asynchronous=asynchronous)


def test_break_does_not_reduce_declared_length(asynchronous):
    # The check happens on loop entry using the declared length of 150, before
    # any body iteration (and therefore before the break) can run.
    source = (
        "{% for i in (1..150) %}"
        "{% if i == 5 %}{% break %}{% endif %}{{ i | tick }}"
        "{% endfor %}"
    )
    with pytest.raises(LoopIterationLimitError):
        _render(source, asynchronous=asynchronous)

    nested = (
        "{% for i in (1..10) %}{% for j in (1..150) %}"
        "{% if j == 2 %}{% break %}{% endif %}{{ j | tick }}"
        "{% endfor %}{% endfor %}"
    )
    with pytest.raises(LoopIterationLimitError):
        _render(nested, asynchronous=asynchronous)


def test_continue_uses_declared_length(asynchronous):
    source = (
        "{% for i in (1..150) %}"
        "{% if i == 1 %}{% continue %}{% endif %}{{ i | tick }}"
        "{% endfor %}"
    )
    with pytest.raises(LoopIterationLimitError):
        _render(source, asynchronous=asynchronous)


def test_non_loop_work_is_not_metered(asynchronous):
    # No loop tag means raise_for_loop_limit is never called regardless of how
    # much string processing the body does.
    source = (
        "{% assign s = 'xxxxxxxxxx' %}"
        "{{ s | append: s | append: s | append: s | append: s }}"
    )
    assert _render(source, asynchronous=asynchronous) == 0
