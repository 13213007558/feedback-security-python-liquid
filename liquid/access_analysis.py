"""Conservative static analysis of the data a template might read.

Where `liquid.static_analysis` reports variable, filter and tag usage, this
module tracks _provenance_: for every variable occurrence it records where the
variable's value can come from (root render data, an `assign`/`capture` local,
a `for` loop variable or an `include`/`render` partial argument) and resolves
the access to a path rooted at the outermost data source.

The result is a conservative over-approximation of possible data accesses,
intended to aid manual security review of templates before deployment. It
cannot replace runtime authorization: accesses hidden behind data-dependent
control flow are still listed, and accesses that cannot be determined
statically are reported explicitly as unknown ranges rather than being
silently dropped or guessed.

Analysis is purely static. No expression is evaluated against render data, so
Drop value methods and filters are never called during analysis.
"""

from __future__ import annotations

from dataclasses import dataclass
from dataclasses import field
from enum import Enum
from enum import auto
from typing import TYPE_CHECKING
from typing import Iterable
from typing import Optional
from typing import Union

from .ast import PartialScope
from .builtin.expressions import FilteredExpression
from .builtin.expressions import Identifier
from .builtin.expressions import Literal
from .builtin.expressions import LoopExpression
from .builtin.expressions import Path
from .builtin.tags.assign_tag import AssignNode
from .builtin.tags.capture_tag import CaptureNode
from .context import RenderContext
from .exceptions import TemplateNotFoundError
from .span import Span
from .static_analysis import RE_PROPERTY

if TYPE_CHECKING:
    from .ast import Node
    from .ast import Partial
    from .expression import Expression
    from .template import BoundTemplate


class AccessOriginKind(Enum):
    """The kind of source a resolved data access originates from."""

    ROOT_INPUT = auto()
    """Render data supplied by the application (template "globals")."""

    LOCAL = auto()
    """A template local variable, like one created by `assign` or `capture`."""

    LOOP_VARIABLE = auto()
    """A `for` or `tablerow` tag loop variable."""

    PARTIAL_ARGUMENT = auto()
    """An argument passed to a partial template by `include` or `render`."""

    BUILTIN = auto()
    """An engine-provided variable, like `forloop` or an `increment` counter."""

    UNKNOWN = auto()
    """A source that cannot be determined statically."""


class UnknownReason(Enum):
    """Why a range of possible data accesses cannot be determined statically."""

    DYNAMIC_PARTIAL_NAME = auto()
    """An `include`/`render`-style tag with a non-literal partial name."""

    MISSING_PARTIAL = auto()
    """A statically named partial template that could not be loaded."""

    RECURSION_LIMIT = auto()
    """Partial template recursion exceeded the configured depth limit."""


class UnknownSegment:
    """A path segment that cannot be determined statically.

    For example, the subscript in `x[y]` is unknown until render time.
    """

    _instance: Optional["UnknownSegment"] = None

    def __new__(cls) -> "UnknownSegment":
        if cls._instance is None:
            cls._instance = super().__new__(cls)
        return cls._instance

    def __str__(self) -> str:
        return "?"

    def __repr__(self) -> str:
        return "UNKNOWN_SEGMENT"


class EachElement:
    """A path segment representing each element of an iterated collection."""

    _instance: Optional["EachElement"] = None

    def __new__(cls) -> "EachElement":
        if cls._instance is None:
            cls._instance = super().__new__(cls)
        return cls._instance

    def __str__(self) -> str:
        return "*"

    def __repr__(self) -> str:
        return "EACH"


UNKNOWN_SEGMENT = UnknownSegment()
"""Singleton marker for a statically unknown path segment."""

EACH = EachElement()
"""Singleton marker for each element of an iterated collection."""

Segment = Union[str, int, UnknownSegment, EachElement]


@dataclass(frozen=True)
class AccessPath:
    """A resolved data access path, from an outermost root inwards."""

    segments: tuple[Segment, ...]

    def __str__(self) -> str:
        if not self.segments:
            return ""
        it = iter(self.segments)
        buf = [str(next(it))]
        for segment in it:
            if isinstance(segment, (UnknownSegment, EachElement)):
                buf.append(f"[{segment}]")
            elif isinstance(segment, str):
                if RE_PROPERTY.fullmatch(segment):
                    buf.append(f".{segment}")
                else:
                    buf.append(f"[{segment!r}]")
            else:
                buf.append(f"[{segment}]")
        return "".join(buf)


@dataclass(frozen=True)
class DataAccess:
    """One possible read of template data, with its provenance chain."""

    path: AccessPath
    """The resolved path, with unknown and iterated segments marked."""

    origin: AccessOrigin
    """The first link of this access's provenance chain."""

    span: Span = field(hash=False)
    """Where the access (or binding) occurs."""

    text: str = field(hash=False)
    """The variable or expression as written in the template source."""

    def __str__(self) -> str:
        return str(self.path)


@dataclass(frozen=True)
class AccessOrigin:
    """One link in the provenance chain of a `DataAccess`."""

    kind: AccessOriginKind
    """The kind of source this link represents."""

    name: str
    """The bound name (or root variable name) at this link."""

    span: Optional[Span] = field(default=None, hash=False)
    """Where the name was bound. `None` for root input data."""

    source: Optional[DataAccess] = field(default=None, hash=False)
    """What this binding's value derives from, if statically known."""

    def chain(self) -> tuple[AccessOrigin, ...]:
        """Return this origin followed by every origin it derives from."""
        links = [self]
        source = self.source
        while source is not None:
            links.append(source.origin)
            source = source.origin.source
        return tuple(links)


@dataclass(frozen=True)
class UnknownAccess:
    """A range of possible data accesses that cannot be determined statically."""

    reason: UnknownReason
    """Why the range is unknown."""

    span: Span = field(hash=False)
    """Where the offending tag or expression occurs."""

    detail: str = field(hash=False)
    """A human readable description, like the partial name expression."""


@dataclass(frozen=True)
class DataAccessAnalysis:
    """The result of analyzing a template with `analyze_data_access`.

    Args:
        accesses: Every variable occurrence found, each resolved to a path
            from its outermost data source, with a provenance chain. This is
            a conservative over-approximation of what the template might read.
        unknowns: Ranges of possible accesses that could not be determined
            statically, like those behind a dynamic partial name.
    """

    accesses: tuple[DataAccess, ...]
    unknowns: tuple[UnknownAccess, ...]

    def root_input_paths(self) -> list[str]:
        """Return distinct resolved paths that reach root input data.

        Accesses rooted at opaque locals or unknown sources are excluded, as
        are engine-provided variables. Useful as a review checklist of the
        business data paths a template might read.
        """
        paths: list[str] = []
        seen: set[str] = set()
        for access in self.accesses:
            if access.origin.chain()[-1].kind != AccessOriginKind.ROOT_INPUT:
                continue
            path = str(access.path)
            if path not in seen:
                seen.add(path)
                paths.append(path)
        return paths


@dataclass
class _Binding:
    """A name bound in a static scope, with its provenance."""

    name: str
    kind: AccessOriginKind
    span: Span
    sources: tuple[DataAccess, ...]  # Empty means the value is opaque.


class _ProvenanceScope:
    """A stack of name bindings, innermost last."""

    def __init__(self, bindings: Optional[dict[str, _Binding]] = None) -> None:
        self.stack = [bindings if bindings is not None else {}]

    def lookup(self, name: str) -> Optional[_Binding]:
        for scope in reversed(self.stack):
            if name in scope:
                return scope[name]
        return None

    def push(self, bindings: dict[str, _Binding]) -> None:
        self.stack.append(bindings)

    def pop(self) -> dict[str, _Binding]:
        return self.stack.pop()

    def bind_root(self, binding: _Binding) -> None:
        """Bind a name in the root (template local) scope."""
        self.stack[0][binding.name] = binding


def _path_segments(path: Path) -> tuple[Segment, ...]:
    segments: list[Segment] = []
    for segment in path.path:
        if isinstance(segment, Path):
            # A dynamic subscript. The inner path is recorded as its own
            # access when walking expression children.
            segments.append(UNKNOWN_SEGMENT)
        else:
            segments.append(segment)
    return tuple(segments)


def _resolve_path(
    path: Path, scope: _ProvenanceScope, template_name: str
) -> tuple[DataAccess, ...]:
    """Resolve a path expression to possible data accesses.

    Returns one access for each value the root binding could plausibly hold.
    """
    segments = _path_segments(path)
    span = Span(template_name, path.token.start_index)
    text = str(path)
    root = segments[0]

    binding = scope.lookup(root) if isinstance(root, str) else None

    if binding is None:
        if isinstance(root, str):
            origin = AccessOrigin(AccessOriginKind.ROOT_INPUT, root)
        else:
            # A path rooted at a dynamic segment, like `{{ [a.b] }}`.
            origin = AccessOrigin(AccessOriginKind.UNKNOWN, str(UNKNOWN_SEGMENT))
        return (DataAccess(AccessPath(segments), origin, span, text),)

    if not binding.sources:
        origin = AccessOrigin(binding.kind, binding.name, binding.span)
        return (DataAccess(AccessPath(segments), origin, span, text),)

    accesses = []
    for source in binding.sources:
        resolved = AccessPath(source.path.segments + segments[1:])
        origin = AccessOrigin(binding.kind, binding.name, binding.span, source)
        accesses.append(DataAccess(resolved, origin, span, text))
    return tuple(accesses)


def _walk_expression(
    expression: Expression,
    scope: _ProvenanceScope,
    template_name: str,
    accesses: list[DataAccess],
) -> None:
    if isinstance(expression, Path):
        accesses.extend(_resolve_path(expression, scope, template_name))

    child_scope = expression.scope()
    if child_scope:
        # Lambda expression parameters, like those used by `where` filter
        # arguments. Their values are opaque to static analysis.
        scope.push(
            {
                str(ident): _Binding(
                    str(ident),
                    AccessOriginKind.UNKNOWN,
                    Span(template_name, ident.token.start_index),
                    (),
                )
                for ident in child_scope
            }
        )
        for expr in expression.children():
            _walk_expression(expr, scope, template_name, accesses)
        scope.pop()
    else:
        for expr in expression.children():
            _walk_expression(expr, scope, template_name, accesses)


def _resolve_expression_sources(
    expression: Expression, scope: _ProvenanceScope, template_name: str
) -> tuple[DataAccess, ...]:
    """Resolve the value sources of a bound expression, if statically known."""
    if isinstance(expression, Path):
        return _resolve_path(expression, scope, template_name)
    if (
        isinstance(expression, FilteredExpression)
        and not expression.filters
        and isinstance(expression.left, Path)
    ):
        return _resolve_path(expression.left, scope, template_name)
    # Literals, filtered or otherwise computed values are opaque.
    return ()


def _with_each(sources: tuple[DataAccess, ...]) -> tuple[DataAccess, ...]:
    """Return _sources_ plus a variant iterating each element of each source."""
    return sources + tuple(
        DataAccess(
            AccessPath(source.path.segments + (EACH,)),
            source.origin,
            source.span,
            source.text,
        )
        for source in sources
    )


def _template_bindings(
    node: Node, scope: _ProvenanceScope, template_name: str
) -> Iterable[_Binding]:
    """Return bindings a node adds to the template local scope."""
    if isinstance(node, AssignNode):
        yield _Binding(
            str(node.name),
            AccessOriginKind.LOCAL,
            Span(template_name, node.name.token.start_index),
            _resolve_expression_sources(node.expression, scope, template_name),
        )
        return

    if isinstance(node, CaptureNode):
        yield _Binding(
            str(node.name),
            AccessOriginKind.LOCAL,
            Span(template_name, node.name.token.start_index),
            (),
        )
        return

    if node.token.value in ("increment", "decrement"):
        for ident in node.template_scope():
            yield _Binding(
                str(ident),
                AccessOriginKind.BUILTIN,
                Span(template_name, ident.token.start_index),
                (),
            )
        return

    # Unrecognized tags that add names to the template scope.
    for ident in node.template_scope():
        yield _Binding(
            str(ident),
            AccessOriginKind.UNKNOWN,
            Span(template_name, ident.token.start_index),
            (),
        )


def _block_bindings(
    node: Node, scope: _ProvenanceScope, template_name: str
) -> dict[str, _Binding]:
    """Return bindings a node adds to its own block scope."""
    bindings: dict[str, _Binding] = {}

    expression = getattr(node, "expression", None)
    if isinstance(expression, LoopExpression):
        sources = tuple(
            DataAccess(
                AccessPath(source.path.segments + (EACH,)),
                source.origin,
                source.span,
                source.text,
            )
            for source in _resolve_expression_sources(
                expression.iterable, scope, template_name
            )
        )
        bindings[expression.identifier] = _Binding(
            expression.identifier,
            AccessOriginKind.LOOP_VARIABLE,
            Span(template_name, expression.token.start_index),
            sources,
        )

    for ident in node.block_scope():
        name = str(ident)
        if name in bindings:
            continue
        kind = (
            AccessOriginKind.BUILTIN
            if name in ("forloop", "tablerowloop")
            else AccessOriginKind.UNKNOWN
        )
        bindings[name] = _Binding(
            name, kind, Span(template_name, ident.token.start_index), ()
        )
    return bindings


def _partial_name(
    node: Node, partial: Partial
) -> tuple[Optional[str], Optional[str]]:
    """Return a (name, dynamic detail) pair for a partial template.

    The name is `None` when it cannot be determined statically, in which case
    _detail_ describes the dynamic name expression. Never evaluates an
    arbitrary expression: only string literals are read directly.
    """
    name = getattr(node, "name", partial.name)

    if isinstance(name, Identifier):
        # An inline snippet, rendered from a context variable. Analyzed with
        # the parent template's name.
        return "", None
    if isinstance(name, str):
        return name, None
    if isinstance(name, Literal):
        return str(name.value), None
    return None, str(name)


def _partial_bindings(
    node: Node, scope: _ProvenanceScope, template_name: str, partial_name: str
) -> dict[str, _Binding]:
    """Return bindings introduced by an `include`/`render`-style tag."""
    bindings: dict[str, _Binding] = {}

    args = getattr(node, "args", None)
    if args is not None:
        for arg in args:
            bindings[arg.name] = _Binding(
                arg.name,
                AccessOriginKind.PARTIAL_ARGUMENT,
                Span(template_name, arg.token.start_index),
                _resolve_expression_sources(arg.value, scope, template_name),
            )

    var = getattr(node, "var", None)
    if var is not None:
        alias = getattr(node, "alias", None)
        if alias is not None:
            key = str(alias)
        elif partial_name:
            key = partial_name.split(".", 1)[0]
        else:
            key = ""
        if key:
            # A `with`/`for` bound variable might be the value itself or,
            # when array-like, each of its elements. Record both, so accesses
            # through either shape are covered.
            sources = _with_each(
                _resolve_expression_sources(var, scope, template_name)
            )
            bindings[key] = _Binding(
                key,
                AccessOriginKind.PARTIAL_ARGUMENT,
                Span(template_name, var.token.start_index),
                sources,
            )

    if args is None and var is None:
        # An unrecognized partial-loading tag. Bind its documented in-scope
        # names as opaque.
        partial = node.partial_scope()
        if partial is not None:
            for ident in partial.in_scope:
                bindings[str(ident)] = _Binding(
                    str(ident),
                    AccessOriginKind.UNKNOWN,
                    Span(template_name, ident.token.start_index),
                    (),
                )
    return bindings


def _partial_signature(
    partial_name: str, bindings: dict[str, _Binding]
) -> tuple[str, tuple[tuple[str, tuple[str, ...]], ...]]:
    """Return a hashable key for a partial name plus its resolved arguments."""
    return (
        partial_name,
        tuple(
            sorted(
                (
                    name,
                    tuple(str(source.path) for source in binding.sources),
                )
                for name, binding in bindings.items()
            )
        ),
    )


def analyze_data_access(
    template: BoundTemplate,
    *,
    include_partials: bool = True,
    max_partial_depth: int = 10,
) -> DataAccessAnalysis:
    """Report the data the given template might read, with provenance.

    This is used by `BoundTemplate.analyze_data_access`.

    Args:
        template: The template to analyze.
        include_partials: If `True`, load and analyze partial templates too.
        max_partial_depth: The maximum depth of nested partial templates to
            follow before reporting an unknown range instead.

    The result is a conservative over-approximation. It is intended to aid
    security review and cannot replace runtime authorization.
    """
    accesses: list[DataAccess] = []
    unknowns: list[UnknownAccess] = []

    root_scope = _ProvenanceScope()
    static_context = RenderContext(template)
    seen: set[tuple[str, tuple[tuple[str, tuple[str, ...]], ...]]] = set()

    def _visit(
        node: Node, template_name: str, scope: _ProvenanceScope, depth: int
    ) -> None:
        for expr in node.expressions():
            _walk_expression(expr, scope, template_name, accesses)

        for binding in _template_bindings(node, scope, template_name):
            scope.bind_root(binding)

        partial = node.partial_scope()
        if partial is None:
            scope.push(_block_bindings(node, scope, template_name))
            for child in node.children(
                static_context, include_partials=include_partials
            ):
                _visit(child, template_name, scope, depth)
            scope.pop()
            return

        partial_name, dynamic_detail = _partial_name(node, partial)
        span = Span(template_name, node.token.start_index)

        if partial_name is None:
            # A dynamic partial name. Record the name expression's own data
            # access (done above, via node.expressions()) and report the
            # partial's accesses as an explicit unknown range.
            unknowns.append(
                UnknownAccess(
                    UnknownReason.DYNAMIC_PARTIAL_NAME,
                    span,
                    f"dynamic partial name {dynamic_detail!r}",
                )
            )
            return

        partial_name = partial_name or template_name
        bindings = _partial_bindings(node, scope, template_name, partial_name)
        signature = _partial_signature(partial_name, bindings)

        if signature in seen:
            return
        seen.add(signature)

        if depth >= max_partial_depth:
            unknowns.append(
                UnknownAccess(
                    UnknownReason.RECURSION_LIMIT,
                    span,
                    f"partial {partial_name!r} exceeded the maximum depth",
                )
            )
            return

        if partial.scope == PartialScope.ISOLATED:
            partial_scope = _ProvenanceScope(dict(bindings))
        else:
            partial_scope = scope
            partial_scope.push(bindings)

        try:
            children = node.children(
                static_context, include_partials=include_partials
            )
            for child in children:
                _visit(child, partial_name, partial_scope, depth + 1)
        except TemplateNotFoundError:
            unknowns.append(
                UnknownAccess(
                    UnknownReason.MISSING_PARTIAL,
                    span,
                    f"partial {partial_name!r} could not be loaded",
                )
            )
        finally:
            if partial.scope != PartialScope.ISOLATED:
                partial_scope.pop()

    for node in template.nodes:
        _visit(node, template.name, root_scope, 0)

    return DataAccessAnalysis(accesses=tuple(accesses), unknowns=tuple(unknowns))


async def analyze_data_access_async(
    template: BoundTemplate,
    *,
    include_partials: bool = True,
    max_partial_depth: int = 10,
) -> DataAccessAnalysis:
    """An async version of `analyze_data_access`."""
    accesses: list[DataAccess] = []
    unknowns: list[UnknownAccess] = []

    root_scope = _ProvenanceScope()
    static_context = RenderContext(template)
    seen: set[tuple[str, tuple[tuple[str, tuple[str, ...]], ...]]] = set()

    async def _visit(
        node: Node, template_name: str, scope: _ProvenanceScope, depth: int
    ) -> None:
        for expr in node.expressions():
            _walk_expression(expr, scope, template_name, accesses)

        for binding in _template_bindings(node, scope, template_name):
            scope.bind_root(binding)

        partial = node.partial_scope()
        if partial is None:
            scope.push(_block_bindings(node, scope, template_name))
            for child in await node.children_async(
                static_context, include_partials=include_partials
            ):
                await _visit(child, template_name, scope, depth)
            scope.pop()
            return

        partial_name, dynamic_detail = _partial_name(node, partial)
        span = Span(template_name, node.token.start_index)

        if partial_name is None:
            unknowns.append(
                UnknownAccess(
                    UnknownReason.DYNAMIC_PARTIAL_NAME,
                    span,
                    f"dynamic partial name {dynamic_detail!r}",
                )
            )
            return

        partial_name = partial_name or template_name
        bindings = _partial_bindings(node, scope, template_name, partial_name)
        signature = _partial_signature(partial_name, bindings)

        if signature in seen:
            return
        seen.add(signature)

        if depth >= max_partial_depth:
            unknowns.append(
                UnknownAccess(
                    UnknownReason.RECURSION_LIMIT,
                    span,
                    f"partial {partial_name!r} exceeded the maximum depth",
                )
            )
            return

        if partial.scope == PartialScope.ISOLATED:
            partial_scope = _ProvenanceScope(dict(bindings))
        else:
            partial_scope = scope
            partial_scope.push(bindings)

        try:
            children = await node.children_async(
                static_context, include_partials=include_partials
            )
            for child in children:
                await _visit(child, partial_name, partial_scope, depth + 1)
        except TemplateNotFoundError:
            unknowns.append(
                UnknownAccess(
                    UnknownReason.MISSING_PARTIAL,
                    span,
                    f"partial {partial_name!r} could not be loaded",
                )
            )
        finally:
            if partial.scope != PartialScope.ISOLATED:
                partial_scope.pop()

    for node in template.nodes:
        await _visit(node, template.name, root_scope, 0)

    return DataAccessAnalysis(accesses=tuple(accesses), unknowns=tuple(unknowns))
