# AtomDoc

> **This is a conceptual prototype for a collaborative document system
> based on Pydantic models. It is both conceptually and directly based on
> [DocuKit](https://github.com/docukit/docukit). This project is fully
> unsupported and intended for exploration only.**

AtomDoc explores local-first document models for Python with type-safe
schemas, semantic atomicity, and operation tracking.

## What makes this distinct

An AtomDoc schema **dictates the shape of a document** — its node types,
their fields, their allowed children, and their default values. Pydantic
is used throughout: to define the schema, to serialize and deserialize
documents, and to validate state at transaction boundaries.

The key idea is **semantic atomicity**: Pydantic `frozen=True` models
define the boundary of mutation. A frozen model like `Color` must be
replaced as a whole — there is no operation that changes just the red
channel. This prevents invalid transient states: a user cannot produce a
malformed color by editing one component at a time or by typing half a
hex string. The operation layer enforces that the smallest unit of change
for a `Color` is the entire `Color`. (An editing UI must enforce the
same boundary — presenting color edits as a single atomic action, not
as three independent field edits.)

This means the schema does double duty: it describes the data **and** it
describes the granularity of change. Primitive fields (`str`, `int`,
`float`, `bool`) are independently editable — concurrent edits to
different fields merge cleanly. Frozen model fields are atomic — they
are replaced as a unit, with last-write-wins on conflict. The merge
semantics are derived from the type annotations, not configured
separately.

## Requirements

Python 3.12+ and Pydantic 2.

## Quick start

### Define your schema

```python
from pydantic import BaseModel
from atomdoc import node, Array, Doc

# Atomic value type — frozen, replaced as a unit
class Color(BaseModel, frozen=True):
    r: int = 0
    g: int = 0
    b: int = 0

# Node types — @node turns a class into a document node
@node
class Annotation:
    label: str = ""
    color: Color = Color()

@node
class Page:
    title: str = ""
    annotations: Array[Annotation] = []
```

`Array[T]` declares a named child slot — an ordered collection of child
nodes. Everything else is a state field.

### Create a document

```python
doc = Doc(Page(
    title="Hello",
    annotations=[
        Annotation(label="Important", color=Color(r=255)),
        Annotation(label="Draft"),
    ],
))
```

Node types are auto-discovered from slot declarations — no registration
needed.

### Read like plain Python

```python
doc.root.title                          # "Hello" — just a str
type(doc.root.title)                    # <class 'str'>
len(doc.root.annotations)              # 2
doc.root.annotations[0].label          # "Important"
doc.root.annotations[0].color.r        # 255
isinstance(doc.root.annotations[0], Annotation)  # True

for ann in doc.root.annotations:
    print(ann.label, ann.color)
```

Slot access supports indexing, slicing, iteration, `len`, `bool`, and
`in` — it looks and feels like a list.

### Mutate

After creation, mutations happen inside transactions:

```python
with doc.transaction():
    doc.root.title = "Updated"
    new_ann = doc.create_node(Annotation, label="New")
    doc.root.annotations.append(new_ann)            # add to end
    doc.root.annotations.prepend(another)           # add to start
    doc.root.annotations.insert(2, mid)             # insert at index
    doc.root.annotations[0].delete()                # remove node
    doc.root.annotations.clear()                    # remove all
```

### Multiple named slots

A node can have multiple independently managed child collections:

```python
@node
class Note:
    text: str = ""

@node
class Slide:
    title: str = ""
    annotations: Array[Annotation] = []
    notes: Array[Note] = []

doc = Doc(Slide(
    annotations=[Annotation(label="x")],
    notes=[Note(text="y")],
))

len(doc.root.annotations)  # 1
len(doc.root.notes)        # 1
```

Slots are independent — each has its own ordering. Siblings within one
slot don't cross into another.

### Nested nodes

Nodes with slots can be nested arbitrarily:

```python
@node
class Section:
    heading: str = ""
    pages: Array[Page] = []

@node
class Document:
    title: str = ""
    sections: Array[Section] = []

doc = Doc(Document(
    title="My Doc",
    sections=[
        Section(
            heading="Chapter 1",
            pages=[
                Page(title="Intro", annotations=[Annotation(label="note")]),
                Page(title="Details"),
            ],
        ),
    ],
))

doc.root.sections[0].pages[0].annotations[0].label  # "note"
```

### Serialize and restore

```python
data = doc.to_json()

doc2 = Doc.from_json(data, root_type=Page)
assert doc2.root.title == "Hello"
assert doc2.root.annotations[0].color.r == 255
```

The JSON format uses a dict for slot children:

```json
["doc-id", "Page", {"title": "\"Hello\""}, {
  "annotations": [
    ["ann-id", "Annotation", {"label": "\"Important\"", "color": "{\"r\": 255, \"g\": 0, \"b\": 0}"}]
  ]
}]
```

### Undo / redo

```python
from atomdoc import UndoManager

undo = UndoManager(doc)

with doc.transaction():
    doc.root.title = "Changed"

undo.undo()
assert doc.root.title == "Hello"

undo.redo()
assert doc.root.title == "Changed"
```

### Change events

```python
doc.on_change(lambda event: print(
    "inserted:", event.diff.inserted,
    "deleted:", event.diff.deleted,
    "updated:", event.diff.updated,
))
```

Change events fire once per transaction, after all mutations and
normalization are complete.

### Transactions

All mutations occur within a transaction. Explicit transactions batch
multiple changes into a single event:

```python
with doc.transaction():
    doc.root.title = "A"
    doc.root.annotations.append(ann1)
    ann2.delete()
# one change event fires here
```

Bare assignments open and commit an implicit transaction:

```python
doc.root.title = "B"  # auto-committed immediately
```

If an exception occurs inside a transaction, all mutations roll back:

```python
with doc.transaction():
    doc.root.title = "Temporary"
    raise ValueError("oops")
# doc.root.title is unchanged
```

## Validation

When `@node` decorates a Pydantic `BaseModel`, the full Pydantic
validation language is available. Validation runs at **transaction commit
time** — not on every field write — so intermediate states don't need to
be valid.

### Field constraints

```python
@node
class Annotation(BaseModel):
    opacity: float = Field(ge=0.0, le=1.0, default=1.0)
```

`Field(ge=0, le=1)` is enforced both per-write (via `TypeAdapter`) and
at commit time (via `model_validate`).

### Field validators

```python
@node
class Note(BaseModel):
    text: str = ""

    @field_validator("text")
    @classmethod
    def no_forbidden(cls, v):
        if "FORBIDDEN" in v:
            raise ValueError("forbidden content")
        return v
```

### Cross-field model validators

```python
@node
class Annotation(BaseModel):
    visible: bool = True
    opacity: float = 1.0

    @model_validator(mode="after")
    def check(self):
        if self.visible and self.opacity == 0:
            raise ValueError("visible nodes must have opacity > 0")
        return self
```

This allows transient invalid states within a transaction:

```python
with doc.transaction():
    ann.visible = True
    ann.opacity = 0.0    # invalid intermediate state — OK
    ann.visible = False   # fixed before commit
# final state is valid — commit succeeds
```

If validation fails at commit, the entire transaction rolls back and no
change event fires.

### Plain classes skip validation

`@node` on a plain class (not a `BaseModel`) creates nodes without
model-level validation. Per-field type checking via `TypeAdapter` still
applies.

```python
@node
class Simple:
    x: int = 0
    y: str = ""
```

## The `@node` decorator

`@node` converts any class into a document node type:

```python
# Plain class — no Pydantic validation
@node
class MyNode:
    title: str = ""
    items: Array[OtherNode] = []

# BaseModel — full Pydantic validation at commit time
@node
class MyNode(BaseModel):
    title: str = ""
    items: Array[OtherNode] = []

# Custom type name
@node("my_custom_type")
class MyNode:
    title: str = ""
```

The node type name defaults to the class name. The class can also extend
`AtomNode` directly if you prefer:

```python
from atomdoc import AtomNode

class MyNode(AtomNode, node_type="MyNode"):
    title: str = ""
    items: Array[OtherNode] = []
```

## Tree navigation

Navigation is through the doc, not on the node:

```python
doc.parent(ann)               # parent node
doc.next_sibling(ann)         # next sibling within the same slot
doc.prev_sibling(ann)         # previous sibling within the same slot

for ancestor in doc.ancestors(ann):       # walk up to root
    ...

for desc in doc.descendants(doc.root):    # depth-first, all slots
    ...

for sib in doc.next_siblings(ann):        # forward within slot
    ...

for sib in doc.prev_siblings(ann):        # backward within slot
    ...
```

`descendants()` traverses all slots in declaration order. Nodes
themselves are pure data — the doc owns the tree structure.

## Field tiers

The schema defines the granularity of change. The tier is inferred
automatically from the type annotation:

| Tier | Python type | Behavior |
|------|-------------|----------|
| **Mergeable** | `str`, `int`, `float`, `bool` | One operation per field. Concurrent edits to different fields merge. |
| **Atomic** | `frozen=True` Pydantic model | Replaced as a unit. Last-write-wins on conflict. |
| **Opaque** | `bytes` | Stored as base64, not diffed or merged. |
| **Structure** | `Array[T]` | Named child slot. Per-node insert/delete/move operations. |

## Extensions

Bundle node types and normalization hooks:

```python
from atomdoc import Extension

def ensure_has_page(diff):
    if not doc.root.pages:
        doc.root.pages.append(doc.create_node(Page))

ext = Extension(
    nodes=[Page, Annotation],
    normalize=ensure_has_page,
)

doc = Doc(Document, extensions=[ext])
```

Normalizers run after mutations and before the change event. In strict
mode (the default), they run twice to verify idempotency.

## Development

```bash
uv sync
uv run pytest
uv run mypy src/atomdoc
uv run ruff check src/atomdoc tests
```
