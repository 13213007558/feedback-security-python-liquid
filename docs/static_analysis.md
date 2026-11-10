Instances of [`BoundTemplate`](api/template.md), as returned by [`parse()`](api/convenience.md#liquid.parse), [`from_string()`](api/environment.md#liquid.Environment.from_string) and [`get_template()`](api/environment.md#liquid.Environment.get_template), include several methods for inspecting a template's variable, tag a filter usage, without rendering the template.

By default, all of these methods will try to load and analyze [included](tag_reference.md#include), [rendered](tag_reference.md#render) and [extended](optional_tags.md#extends) templates too. Set the `include_partials` keyword only argument to `False` to disable automatic loading and analysis of partial/parent templates.

## Variables

[`variables()`](api/template.md#liquid.BoundTemplate.variables) and [`variables_async()`](api/template.md#liquid.BoundTemplate.variables_async) return a list of distinct variables used in the template, without [path segments](variables_and_drops.md#paths-to-variables). The list will include variables that are _local_ to the template, like those crated with `{% assign %}` and `{% capture %}`, or are in scope from `{% for %}` tags.

```python
from liquid import parse

source = """\
Hello, {{ you }}!
{% assign x = 'foo' | upcase %}

{% for ch in x %}
    - {{ ch }}
{% endfor %}

Goodbye, {{ you.first_name | capitalize }} {{ you.last_name }}
Goodbye, {{ you.first_name }} {{ you.last_name }}
"""

template = parse(source)
print(template.variables())
```

```plain title="output"
['you', 'x', 'ch']
```

## Variable paths

[`variable_paths()`](api/template.md#liquid.BoundTemplate.variable_paths) and [`variable_paths_async()`](api/template.md#liquid.BoundTemplate.variable_paths_async) return a list of variables used in the template, including all path segments. The list will include variables that are _local_ to the template, like those crated with `{% assign %}` and `{% capture %}`, or are in scope from `{% for %}` tags.

```python
# ... continued from above

print(template.variable_paths())
```

```plain title="output"
['you.first_name', 'you', 'you.last_name', 'x', 'ch']
```

## Variable segments

[`variable_segments()`](api/template.md#liquid.BoundTemplate.variable_segments) and [`variable_segments_async()`](api/template.md#liquid.BoundTemplate.variable_segments_async) return a list of variables used in the template, each as a list of segments. The list will include variables that are _local_ to the template, like those crated with `{% assign %}` and `{% capture %}`, or are in scope from `{% for %}` tags.

```python
# ... continued from above

print(template.variable_segments())
```

```plain title="output"
[
    ["you", "last_name"],
    ["you"],
    ["you", "first_name"],
    ["ch"],
    ["x"],
]
```

## Global variables

[`global_variables()`](api/template.md#liquid.BoundTemplate.global_variables) and [`global_variables_async()`](api/template.md#liquid.BoundTemplate.global_variables_async) return a list of variables used in the template, without path segments and excluding variables that are local to the template.

```python
# ... continued from above

print(template.global_variables())
```

```plain title="output"
['you']
```

## Global variable paths

[`global_variable_paths()`](api/template.md#liquid.BoundTemplate.global_variable_paths) and [`global_variable_paths_async()`](api/template.md#liquid.BoundTemplate.global_variable_paths_async) return a list of variables used in the template, with path segments and excluding variables that are local to the template.

```python
# ... continued from above

print(template.global_variable_paths())
```

```plain title="output"
['you', 'you.first_name', 'you.last_name']
```

## Global variable segments

[`global_variable_segments()`](api/template.md#liquid.BoundTemplate.global_variable_segments) and [`global_variable_segments_async()`](api/template.md#liquid.BoundTemplate.global_variable_segments_async) return a list of variables used in the template, each as a list of segments, excluding variables that are local to the template.

```python
# ... continued from above

print(template.global_variable_segments())
```

```plain title="output"
[
    ['you', 'last_name'],
    ['you', 'first_name'],
    ['you'],
]
```

## Filter names

[`filter_names()`](api/template.md#liquid.BoundTemplate.filter_names) and [`filter_names_async()`](api/template.md#liquid.BoundTemplate.filter_names_async) return names of filters used in the template.

```python
# ... continued from above

print(template.filter_names())
```

```plain title="output"
['upcase', 'capitalize']
```

## Tag names

[`tag_names()`](api/template.md#liquid.BoundTemplate.tag_names) and [`tag_names_async()`](api/template.md#liquid.BoundTemplate.tag_names_async) return the names of tags used in the template.

```python
# ... continued from above

print(template.tag_names())
```

```plain title="output"
['assign', 'for']
```

## Variable, tag and filter locations

[`analyze()`](api/template.md#liquid.BoundTemplate.analyze) and [`analyze_async()`](api/template.md#liquid.BoundTemplate.analyze_async) return an instance of [`TemplateAnalysis`](api/template.md#liquid.static_analysis.TemplateAnalysis). It contains all of the information provided by the methods described above, but includes the location of each variable, tag and filter, each of which can appear many times across many templates.

## Comment and doc nodes

**_New in version 2.2.0_**

[`comments()`](api/template.md#liquid.BoundTemplate.comments) and [`docs()`](api/template.md#liquid.BoundTemplate.docs) return a list of `CommentNode` and `DocNode` instances, respectively. Both have `token` and `text` properties.

```python
from liquid import parse

source = """\
{% doc %}
    some doc comment
{% enddoc %}

Hello!

{% comment %}
    some comment
{% endcomment %}

{% if false %}
    {% # an inline comment %}
{% endif %}
"""

template = parse(source)
print([node.text for node in template.comments()])
print([node.text for node in template.docs()])
```

```plain title="output"
['\n    some comment\n', 'an inline comment']
['\n    some doc comment\n']
```
## Data access paths and provenance

[`analyze_data_access()`](api/template.md#liquid.BoundTemplate.analyze_data_access) and [`analyze_data_access_async()`](api/template.md#liquid.BoundTemplate.analyze_data_access_async) return a [`DataAccessAnalysis`](api/template.md#liquid.access_analysis.DataAccessAnalysis). Where `analyze()` reports variable usage, this reports every variable occurrence resolved to a path from its outermost data source, plus a provenance chain showing _where each value comes from_: root render data, an `assign`/`capture` local, a `for` loop variable or an `include`/`render` partial argument.

This is intended to help review which business data paths a template might read before deploying it. It is additive: `analyze()` and `TemplateAnalysis` are unchanged.

```python
from liquid import Environment
from liquid.builtin import DictLoader

env = Environment(
    loader=DictLoader(
        {
            "main": "{% for item in items %}"
            "{% render 'card', product: item %}"
            "{% endfor %}",
            "card": "{{ product.title }} {{ user.email }}",
        }
    )
)

result = env.get_template("main").analyze_data_access()
for access in result.accesses:
    chain = " <- ".join(f"{o.kind.name}({o.name})" for o in access.origin.chain())
    print(f"{access.text} -> {access.path} : {chain}")

print(result.root_input_paths())
```

```plain title="output"
items -> items : ROOT_INPUT(items)
item -> items[*] : LOOP_VARIABLE(item) <- ROOT_INPUT(items)
product.title -> items[*].title : PARTIAL_ARGUMENT(product) <- LOOP_VARIABLE(item) <- ROOT_INPUT(items)
user.email -> user.email : ROOT_INPUT(user)
['items', 'items[*]', 'items[*].title', 'user.email']
```

Notes on interpretation:

- **Conservative.** The access list is a may-access over-approximation. Accesses inside `{% if %}`/`{% unless %}` branches are always listed, regardless of the condition. It cannot replace runtime authorization.
- **Markers.** `[*]` marks iteration over each element of a collection (a loop variable or a `with`/`for` bound partial argument). `[?]` marks a path segment that is dynamic and cannot be determined statically, like the subscript in `x[y]`.
- **Unknowns.** Ranges that cannot be determined statically — a dynamic partial name (`{% include name %}`), a partial that fails to load, or partial recursion beyond `max_partial_depth` — are reported in `DataAccessAnalysis.unknowns` with a reason and location. They are never silently omitted and never replaced with a guessed field.
- **Shadowing.** A template local that shadows a root input name (for example `{% assign item = ... %}`) is reported with a `LOCAL` origin; it is not mistaken for root input.
- **No evaluation.** Analysis never evaluates expressions against render data. Drop value methods and filters are not called.
- **`with`/`for` arguments.** A partial argument bound with `with` or `for` might be the value itself or each of its elements, depending on render-time data. Both shapes are tracked, so accesses may appear once per shape.
