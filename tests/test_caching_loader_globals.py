"""Tests for template globals isolation with caching loaders."""

from __future__ import annotations

import asyncio
import threading
from typing import TYPE_CHECKING

from liquid import CachingDictLoader
from liquid import Environment
from liquid.loader import TemplateSource
from liquid.utils import ThreadSafeLRUCache

if TYPE_CHECKING:
    from liquid import RenderContext


class CountingCachingDictLoader(CachingDictLoader):
    """A caching loader that counts how many times template source is parsed."""

    def __init__(self, templates: dict[str, str]) -> None:
        super().__init__(templates)
        self.parse_count = 0

    def get_source(
        self,
        env: Environment,
        template_name: str,
        *,
        context: RenderContext | None = None,
        **kwargs: object,
    ) -> TemplateSource:
        self.parse_count += 1
        return super().get_source(env, template_name, context=context, **kwargs)


class ThreadSafeCachingDictLoader(CountingCachingDictLoader):
    """A counting loader backed by a thread-safe cache."""

    def __init__(self, templates: dict[str, str]) -> None:
        super().__init__(templates)
        self.cache = ThreadSafeLRUCache(capacity=300)


def test_cached_template_globals_are_isolated() -> None:
    """A template held by one caller is not changed by another caller's load."""
    loader = CountingCachingDictLoader({"page": "[{{ tenant }}]"})
    env = Environment(loader=loader)

    # Tenant A loads the template and holds on to it without rendering.
    template_a = env.get_template("page", globals={"tenant": "A"})
    # Tenant B loads the same template before A has rendered.
    template_b = env.get_template("page", globals={"tenant": "B"})

    # Each load returns a distinct binding...
    assert template_a is not template_b
    assert template_a.globals is not template_b.globals
    # ... sharing the same compiled parse tree, parsed only once.
    assert template_a.nodes is template_b.nodes
    assert loader.parse_count == 1

    # The cached, compiled template is never handed out and carries no
    # per-load globals.
    compiled = loader.cache["page"]
    assert compiled is not template_a
    assert compiled is not template_b
    assert compiled.globals == {}

    # A renders after B's load and still sees its own globals.
    assert template_a.render() == "[A]"
    assert template_b.render() == "[B]"
    assert template_a.render() == "[A]"
    assert loader.parse_count == 1


def test_template_globals_contract() -> None:
    """Undefined, empty, default and overriding globals each have a meaning."""
    loader = CountingCachingDictLoader({"page": "{{ tenant }}|{{ shared }}"})
    env = Environment(loader=loader, globals={"tenant": "default", "shared": "env"})

    # Not passing globals applies the environment's default globals.
    assert env.get_template("page").render() == "default|env"
    # An empty mapping is equivalent to not passing globals at all.
    assert env.get_template("page", globals={}).render() == "default|env"
    # Explicit globals override environment globals per key.
    assert env.get_template("page", globals={"tenant": "B"}).render() == "B|env"
    # Environment defaults are unchanged by an explicit override.
    assert env.get_template("page").render() == "default|env"
    # Every load reused the same compiled template.
    assert loader.parse_count == 1


def test_loader_level_globals_contract() -> None:
    """Calling the loader directly without globals binds an empty mapping."""
    loader = CountingCachingDictLoader({"page": "[{{ tenant | default: 'none' }}]"})
    env = Environment(loader=loader)

    template = loader.load(env, "page")
    assert template.globals == {}
    assert template.render() == "[none]"

    # A later load with explicit globals does not leak into the first binding.
    assert loader.load(env, "page", globals={"tenant": "B"}).render() == "[B]"
    assert template.render() == "[none]"
    assert loader.parse_count == 1


def test_cached_template_globals_are_isolated_async() -> None:
    """Async loads follow the same globals contract as sync loads."""
    loader = CountingCachingDictLoader({"page": "[{{ tenant }}]"})
    env = Environment(loader=loader)

    async def coro() -> None:
        template_a = await env.get_template_async("page", globals={"tenant": "A"})
        template_b = await env.get_template_async("page", globals={"tenant": "B"})
        # Mix sync and async loads of the same cached template.
        template_c = env.get_template("page", globals={"tenant": "C"})

        assert template_a is not template_b
        assert template_a.nodes is template_b.nodes is template_c.nodes

        assert await template_a.render_async() == "[A]"
        assert await template_b.render_async() == "[B]"
        assert template_c.render() == "[C]"
        assert await template_a.render_async() == "[A]"

    asyncio.run(coro())
    assert loader.parse_count == 1


def test_partial_render_does_not_clobber_template_globals() -> None:
    """Loading a partial without explicit globals must not leak between callers."""
    loader = CountingCachingDictLoader(
        {
            "snippet": "[{{ tenant }}]",
            "page": "{% render 'snippet' %}",
        }
    )
    env = Environment(loader=loader, globals={"tenant": "default"})

    # Tenant A loads the snippet directly and holds on to it.
    snippet_a = env.get_template("snippet", globals={"tenant": "A"})

    # Tenant B renders a page that renders the same snippet as a partial. The
    # render tag loads "snippet" without explicit globals, so environment
    # defaults apply to that binding, and the partial inherits tenant B's
    # render context.
    page_b = env.get_template("page", globals={"tenant": "B"})
    assert page_b.render() == "[B]"

    # Tenant A's binding is unaffected by the partial load.
    assert snippet_a.render() == "[A]"

    # "page" and "snippet" were each parsed once.
    assert loader.parse_count == 2  # noqa: PLR2004


def test_async_loads_and_partial_render_are_isolated() -> None:
    """Interleaved async loads and partial rendering keep globals isolated."""
    loader = CountingCachingDictLoader(
        {
            "snippet": "<{{ tenant }}>",
            "page": "{% render 'snippet' %}",
        }
    )
    env = Environment(loader=loader, globals={"tenant": "default"})

    async def coro() -> None:
        # Tenant A loads the page and holds on to it without rendering.
        page_a = await env.get_template_async("page", globals={"tenant": "A"})
        # Tenant B loads the same page before A has rendered.
        page_b = await env.get_template_async("page", globals={"tenant": "B"})

        assert page_a is not page_b
        assert page_a.nodes is page_b.nodes

        # Rendering each page loads the shared partial through the cache.
        assert await page_a.render_async() == "<A>"
        assert await page_b.render_async() == "<B>"
        assert await page_a.render_async() == "<A>"

    asyncio.run(coro())
    assert loader.parse_count == 2  # noqa: PLR2004


def test_thread_safe_cache_globals_are_isolated() -> None:
    """A thread-safe cache follows the same globals contract."""
    loader = ThreadSafeCachingDictLoader({"page": "[{{ tenant }}]"})
    env = Environment(loader=loader)

    # Warm the cache so worker threads only ever bind to the compiled template.
    warmup = env.get_template("page", globals={"tenant": "warmup"})
    assert warmup.render() == "[warmup]"
    assert loader.parse_count == 1

    results: dict[str, str] = {}

    def work(tenant: str) -> None:
        template = env.get_template("page", globals={"tenant": tenant})
        results[tenant] = template.render()

    tenants = [f"T{n}" for n in range(8)]
    threads = [threading.Thread(target=work, args=(tenant,)) for tenant in tenants]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert results == {tenant: f"[{tenant}]" for tenant in tenants}
    # The warmup binding is unchanged and nothing was re-parsed.
    assert warmup.render() == "[warmup]"
    assert loader.parse_count == 1
