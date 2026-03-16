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

`Array[T]` declares an ordered collection of child nodes. Everything
else is a state field.

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

Node types are auto-discovered from the schema — no registration needed.

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

Arrays support indexing, slicing, iteration, `len`, `bool`, and `in`.

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

### Multiple child arrays

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

Each array is independent — its own ordering, its own operations.

### Nested nodes

Nodes can be nested arbitrarily:

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

### Serialize

Two formats — clean JSON for reading, wire format for persistence:

```python
# Clean JSON — no internal IDs, just data
doc.to_json()
# {"title": "Hello", "annotations": [{"label": "Important", "color": {"r": 255, "g": 0, "b": 0}}]}

# Wire format — includes IDs, for dump/restore and operation replay
wire = doc.dump()
doc2 = Doc.restore(wire, root_type=Page)
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
normalization are complete. Each event carries forward and inverse
operations for sync and undo.

### Transactions

Explicit transactions batch multiple changes into a single event:

```python
with doc.transaction():
    doc.root.title = "A"
    doc.root.annotations.append(ann1)
    ann2.delete()
# one change event fires here
```

Bare assignments auto-commit immediately:

```python
doc.root.title = "B"  # committed on its own
```

Exceptions roll back the entire transaction:

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

Invalid intermediate states within a transaction are fine:

```python
with doc.transaction():
    ann.visible = True
    ann.opacity = 0.0    # invalid here — OK
    ann.visible = False   # fixed before commit
# final state is valid — commit succeeds
```

If validation fails at commit, the entire transaction rolls back.

### Plain classes skip validation

`@node` on a plain class (not a `BaseModel`) works without model-level
validation. Per-field type checking still applies.

## The `@node` decorator

`@node` converts any class into a document node type:

```python
@node
class MyNode:
    title: str = ""
    items: Array[OtherNode] = []

@node
class MyNode(BaseModel):       # with Pydantic validation
    title: str = ""

@node("my_custom_type")        # custom type name
class MyNode:
    title: str = ""
```

The node type name defaults to the class name.

## Tree navigation

Navigation goes through the doc:

```python
doc.parent(ann)
doc.next_sibling(ann)
doc.prev_sibling(ann)

for ancestor in doc.ancestors(ann):
    ...

for desc in doc.descendants(doc.root):
    ...
```

Nodes are pure data — the doc owns the tree structure.

## Field tiers

The tier is inferred automatically from the type annotation:

| Tier | Python type | Behavior |
|------|-------------|----------|
| **Mergeable** | `str`, `int`, `float`, `bool` | One operation per field. Concurrent edits to different fields merge. |
| **Atomic** | `frozen=True` Pydantic model | Replaced as a unit. Last-write-wins on conflict. |
| **Opaque** | `bytes` | Stored as base64, not diffed or merged. |
| **Structure** | `Array[T]` | Ordered child collection. Per-node insert/delete/move operations. |

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
