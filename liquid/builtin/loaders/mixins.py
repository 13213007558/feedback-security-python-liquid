"""Mixin classes that can be used to add common functions to a template loader."""

from __future__ import annotations

from abc import ABC
from contextlib import suppress
from functools import partial
from typing import TYPE_CHECKING
from typing import Awaitable
from typing import Callable
from typing import Mapping
from typing import Optional
from typing import Protocol

from liquid.utils import LRUCache
from liquid.utils import ThreadSafeLRUCache

if TYPE_CHECKING:
    from liquid import BoundTemplate
    from liquid import Environment
    from liquid import RenderContext

# ruff: noqa: D102

# ignoring "safe-super" type errors due to https://github.com/python/mypy/issues/14757


class _CachingLoaderProtocol(Protocol):
    def load(  # pragma: no cover
        self,
        env: Environment,
        name: str,
        *,
        globals: Optional[Mapping[str, object]] = None,  # noqa: A002
        context: Optional[RenderContext] = None,
        **kwargs: object,
    ) -> BoundTemplate: ...

    async def load_async(  # pragma: no cover
        self,
        env: Environment,
        name: str,
        *,
        globals: Optional[Mapping[str, object]] = None,  # noqa: A002
        context: Optional[RenderContext] = None,
        **kwargs: object,
    ) -> BoundTemplate: ...


class CachingLoaderMixin(ABC, _CachingLoaderProtocol):
    """A mixin class that adds caching to a template loader.

    The cache holds compiled templates - parse trees plus template metadata -
    keyed by template name (and optional namespace). Every call to `load()` or
    `load_async()` returns a distinct `BoundTemplate` that shares the cached
    parse tree but is bound to the globals passed to that call.

    Template globals are never stored in, mutated on, or read from the cached
    template, so one caller's globals can not leak into a template already
    held by another caller. The globals contract is:

    - `globals=None` (the default): the returned template is bound to an empty
      mapping. When loading through `Environment.get_template()`, the
      environment's globals are merged in by the environment before the loader
      is called.
    - An empty mapping: equivalent to `None`.
    - A non-empty mapping: attached to the returned template as-is. The
      mapping passed by `Environment.get_template()` is always a fresh copy,
      so it is never shared between calls.
    """

    caching_loader = True

    def __init__(
        self,
        *,
        auto_reload: bool = True,
        namespace_key: str = "",
        capacity: int = 300,
        thread_safe: bool = False,
    ):
        self.auto_reload = auto_reload
        self.cache = (
            ThreadSafeLRUCache[str, "BoundTemplate"](capacity=capacity)
            if thread_safe
            else LRUCache[str, "BoundTemplate"](capacity=capacity)
        )
        self.namespace_key = namespace_key

    def _check_cache(
        self,
        env: Environment,  # noqa: ARG002
        cache_key: str,
        globals: Optional[Mapping[str, object]],  # noqa: A002
        load_func: Callable[[], BoundTemplate],
    ) -> BoundTemplate:
        try:
            compiled = self.cache[cache_key]
        except KeyError:
            compiled = load_func()
            self.cache[cache_key] = compiled
        else:
            if self.auto_reload and not compiled.is_up_to_date():
                compiled = load_func()
                self.cache[cache_key] = compiled

        # The cached template is never handed out or mutated. Each caller gets
        # a new binding to the shared parse tree, with its own globals.
        return compiled.bind(globals)

    async def _check_cache_async(
        self,
        env: Environment,  # noqa: ARG002
        cache_key: str,
        globals: Optional[Mapping[str, object]],  # noqa: A002
        load_func: Callable[[], Awaitable[BoundTemplate]],
    ) -> BoundTemplate:
        try:
            compiled = self.cache[cache_key]
        except KeyError:
            compiled = await load_func()
            self.cache[cache_key] = compiled
        else:
            if self.auto_reload and not await compiled.is_up_to_date_async():
                compiled = await load_func()
                self.cache[cache_key] = compiled

        # The cached template is never handed out or mutated. Each caller gets
        # a new binding to the shared parse tree, with its own globals.
        return compiled.bind(globals)

    def load(
        self,
        env: Environment,
        name: str,
        *,
        globals: Optional[Mapping[str, object]] = None,  # noqa: A002
        context: Optional[RenderContext] = None,
        **kwargs: object,
    ) -> BoundTemplate:
        cache_key = self.cache_key(name, context, kwargs)
        return self._check_cache(
            env,
            cache_key,
            globals,
            partial(
                super().load,  # type: ignore
                env,
                name,
                # The cached, compiled template is parsed without per-load
                # globals. Callers get a new binding via `BoundTemplate.bind()`.
                globals=None,
                context=context,
                **kwargs,
            ),
        )

    async def load_async(
        self,
        env: Environment,
        name: str,
        *,
        globals: Optional[Mapping[str, object]] = None,  # noqa: A002
        context: Optional[RenderContext] = None,
        **kwargs: object,
    ) -> BoundTemplate:
        cache_key = self.cache_key(name, context, kwargs)
        return await self._check_cache_async(
            env,
            cache_key,
            globals,
            partial(
                super().load_async,  # type: ignore
                env,
                name,
                # The cached, compiled template is parsed without per-load
                # globals. Callers get a new binding via `BoundTemplate.bind()`.
                globals=None,
                context=context,
                **kwargs,
            ),
        )

    def cache_key(
        self,
        name: str,
        context: Optional[RenderContext],
        args: Mapping[str, object],
    ) -> str:
        if not self.namespace_key:
            return name

        # Args take priority over context variables.
        with suppress(KeyError):
            return f"{args[self.namespace_key]}/{name}"

        if context is None:
            return name

        try:
            return f"{context.globals[self.namespace_key]}/{name}"
        except KeyError:
            return name
