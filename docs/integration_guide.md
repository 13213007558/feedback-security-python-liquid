# Integration guide: bounding untrusted template work

This guide explains what python-liquid's resource limits — and
`loop_iteration_limit` in particular — actually guarantee when rendering
templates you do not control. The numbers below are reproducible with the
characterization tests in
`tests/test_loop_iteration_limit_semantics.py`, which run every case through
both `Template.render` and `Template.render_async`.

## What `loop_iteration_limit` checks

The check lives in `RenderContext.raise_for_loop_limit`. Whenever a loop tag
is about to run (`for`, `tablerow`, or the loop form of `render` /
`include`), python-liquid computes

```
loop_iteration_carry × product(declared length of every loop on the stack)
```

and raises `LoopIterationLimitError` if that value is strictly greater than
`loop_iteration_limit`. The check uses each loop's *declared* length and runs
before any loop body executes.

- A single `for`/`tablerow` loop is rejected when its declared length exceeds
  the limit.
- Nested loops are rejected when the product of their declared lengths
  exceeds the limit.
- `loop_iteration_carry` carries the parent's product across `render`
  context copies (see below), so partials do not reset the budget while an
  outer loop is active.
- Sibling loops each start from an empty stack, so their counts do not add up.

## Measured behavior

All measurements use `loop_iteration_limit = 100` and count real loop-body
entries with a filter that increments a counter. "Raises before body" means
the exception is thrown on loop entry, so zero body entries execute. The
synchronous and asynchronous results are identical in every row.

| Template structure | Declared work | Body entries | Result |
| --- | --- | --- | --- |
| Two sequential `for` loops, 60 each | 120 cumulative | 120 | renders |
| Nested `for` 10 × 10 | product 100 | 100 | renders (equal to limit) |
| Nested `for` 11 × 10 | product 110 | 0 | raises before body |
| `for` 10 × `include` partial with `for` 20 | product 200 | 0 | raises before body |
| `include` alone, partial `for` 150 | length 150 | 0 | raises before body |
| `for` 10 × `render` partial with `for` 20 | carried product 200 | 0 | raises before body |
| `render` alone, partial `for` 150 | length 150 | 0 | raises before body |
| Five sequential `render` calls, partial `for` 50 each | 250 cumulative | 250 | renders |
| `render 'p' for items`, 100 items | length 100 | 100 | renders |
| `render 'p' for items`, 101 items | length 101 | 0 | raises before body |
| `for` 10 × `render 'p' for items` (10 items) | product 100 | 100 | renders |
| `for` 11 × `render 'p' for items` (10 items) | product 110 | 0 | raises before body |
| Chain `for` 10 → `render` `for` 10 → `render` `for` 10 | product 1000 | 0 | raises before body |
| Flat `tablerow` of 150 | length 150 | 0 | raises before body |
| `for` 11 × `tablerow` 10 | product 110 | 0 | raises before body |
| `for` 150 with `break` on the fifth item | declared 150 | 0 | raises before body |
| Nested `for` 10 × `for` 150 breaking on item 2 | product 1500 | 0 | raises before body |
| `for` 150 with `continue` on the first item | declared 150 | 0 | raises before body |
| Repeated `append` with no loop tag | no loop | 0 counted | renders, unmetered |

### Why `break` does not help

`break` and `continue` are handled inside the loop body, but the limit check
runs when the loop node is entered and uses the length reported by the loop
expression. A loop that would exit immediately still raises if its declared
length is too large. This is conservative: rejecting a template before
running it avoids relying on the template's own control flow to stay within
budget.

## Partials: `render`, `include` and context copies

- `include` extends the current render context. The partial's loops are
  pushed onto the same loop stack, so nesting multiplies with no copy
  involved.
- `render` calls `RenderContext.copy(..., carry_loop_iterations=True)`. The
  copy gets a fresh loop stack, but `loop_iteration_carry` is set to the
  product of the parent's active loop lengths (including any carry the parent
  already had). A loop in the partial is therefore checked against
  `parent product × partial loop length`. The documented carry guarantee
  holds across the copy, including through chains of nested renders.
- A top-level `render` (or `include`) with no active outer loop has nothing
  to carry; its partial gets the full configured budget for its own loops.
- `render 'name' for collection` and `include 'name' for collection` are
  themselves loops and go through the same check, so they multiply with any
  enclosing loop.

## What the option protects against

With a finite `loop_iteration_limit` you are protected against a single
chain of active loops whose nesting product exceeds the limit, regardless of
whether the nesting is expressed with `for`, `tablerow`, `render ... for`,
`include ... for`, or partials reached from inside a loop. This covers the
classic billion-iterations-style nested loop and its partial-based variants.

## Structures that can still do large amounts of work

The limit is a nesting-product bound on loop entries, not a runtime or
total-work budget. It does not cap:

- **Sequential sibling loops.** Any number of loops, each individually at or
  below the limit, run back to back. Measured above: two loops of 60 perform
  120 body entries under a limit of 100; five rendered partials of 50 perform
  250.
- **Repeated partials without an enclosing loop.** Each top-level
  `render`/`include` starts with no carried count.
- **Non-loop work.** Expensive filters, large string building via `append`,
  `join`, `replace`, `map`, `sort`, etc., and deeply nested `if`/`case`
  blocks never call the loop-limit check.
- **Per-iteration cost.** The limit counts loop entries, not what each entry
  does; a body that renders an expensive partial or filter chain on every
  iteration is bounded in entry count but not in per-entry cost.
- **Output size and local memory** on their own; use the dedicated limits
  below.

## Combining limits for defense in depth

Configure all of the following on the same `Environment` subclass rather
than relying on `loop_iteration_limit` alone:

- `loop_iteration_limit` — bounds the active-loop nesting product.
- `context_depth_limit` — bounds recursive `render`/`include` chains
  (`ContextDepthError`). This is what stops a partial that renders itself.
- `block_nesting_limit` — bounds syntactic nesting depth of block tags.
- `local_namespace_limit` — bounds memory held in template local namespaces,
  including values accumulated across sequential loops.
- `output_stream_limit` — bounds total bytes written per render, which
  constrains loops that emit large output even when their entry count is
  allowed.

If templates can invoke custom filters or tags, their cost is outside every
built-in limit; audit or sandbox them separately. For a hard runtime ceiling,
run rendering in a worker with an external deadline (for example a process or
task timeout), since no built-in option measures wall-clock time or total CPU
work. Lowering `loop_iteration_limit` tightens the nesting bound but must not
be described as complete resource isolation: sequential loops and non-loop
work remain available to a template regardless of the configured value.
