# AtomDoc Specification

**Version:** 0.1.0-draft
**Status:** Draft

AtomDoc is a Python library for building local-first document models with
type-safe schemas, semantic atomicity, and real-time sync. It is strongly
inspired by [DocNode](https://docukit.dev/docnode) and uses
[Pydantic v2](https://docs.pydantic.dev/) to define document schemas.

---

## 0. Design Philosophy

An AtomDoc node should feel like a **plain Python object** to anyone
reading or writing it. State access is attribute access. Children are
iterable. `isinstance` works. You should be able to hand someone an
AtomDoc node and have them use it without knowing AtomDoc exists.

```python
# This is the experience we're designing for:

print(node.title)                        # just an attribute
node.title = "Hello"                     # just assignment

for child in node.children:              # just iteration
    if isinstance(child, AnnotationNode):
        print(child.color.r)             # nested attribute access

node.color = Color(r=0, g=128, b=255)   # replace atomic value

len(node.children)                       # just len
node.children[0]                         # just indexing
```

Pydantic powers validation, serialization, and schema introspection
underneath — but the surface API is plain Python. The library earns its
keep by invisibly tracking operations, enforcing atomicity boundaries,
and batching changes into transactions. None of that machinery should
leak into the read path.

**Guiding principles:**

- **Reads are plain Python.** Attribute access, iteration, indexing,
  `isinstance`, `len` — no `.get()`, no `.value`, no wrappers.
- **Writes are plain Python by default.** Assignment and method calls.
  The operation tracking is invisible for simple usage.
- **The schema is the API.** Field names on the class *are* the
  attribute names on the instance. What you define is what you get.
- **Pydantic is a clear layer beneath.** Naive consumers see plain
  Python objects. Power users can drop down to Pydantic for validation,
  schema introspection, serialization control, or bulk updates.
  Mutation APIs (state changes, tree operations) may surface Pydantic
  conventions where doing so improves ergonomics, simplifies the
  implementation, or makes the read/write distinction clearer.
- **DocNode compatibility is a design goal.** AtomDoc aims to be
  semantically compatible with DocNode's operation model and data
  structures where possible, so that a future sync layer can bridge
  AtomDoc and DocNode clients on the same document. This is a goal,
  not an absolute requirement — Pythonic ergonomics and the atomicity
  model take precedence where they conflict.

---

## 1. Core Concepts

### 1.1 Documents and Nodes

An AtomDoc **document** is a rooted tree of **nodes**. Each node has:

- A unique **id** (string, assigned at creation)
- A **type** (string, declared by the node class)
- **State** (typed fields, accessed as plain attributes)
- **Children** (accessible as an iterable/indexable sequence)
- A position in the tree: parent, previous sibling, next sibling

Nodes are linked via a doubly-linked sibling list per parent, giving O(1)
insertion, deletion, and traversal in all directions.

### 1.2 Semantic Atomicity

The central design principle of AtomDoc is that **the schema defines the
granularity of change**.

Fields on a node fall into three tiers:

| Tier | Python type | Merge behavior | Example |
|------|-------------|----------------|---------|
| **Mergeable** | Primitive (`str`, `int`, `float`, `bool`) | Independent ops; concurrent edits to different fields merge cleanly | `label`, `opacity`, `visible` |
| **Atomic** | `frozen=True` Pydantic model | Replace as a unit; last-write-wins on conflict | `Color`, `Mat4`, `WindowLevel` |
| **Opaque** | `bytes` | Stored, not diffed or merged | `thumbnail`, `binary_blob` |

The tier is derived from the type annotation — no manual annotation is
required. The operation tracking layer inspects the schema at node
registration time and applies the appropriate diff/merge policy
automatically.

Atomic types prevent invalid transient states. Editing one element of a
transform matrix or one channel of a color is not a valid operation — the
entire value is replaced in a single operation, ensuring every state
observable by any client is semantically valid.

### 1.3 Frozen Models as Atomic Boundaries

Atomic values are standard Pydantic models with `frozen=True`:

```python
class Color(BaseModel, frozen=True):
    r: int = Field(ge=0, le=255)
    g: int = Field(ge=0, le=255)
    b: int = Field(ge=0, le=255)
    a: float = Field(ge=0.0, le=1.0, default=1.0)
```

Because the model is frozen, the only way to change it is wholesale
replacement. This is enforced at both the Python level (immutable instance)
and the operation level (single op per replacement).

Validators on frozen models guarantee that invalid values never enter the
document — not from local edits, not from deserialization, not from sync.

---

## 2. Schema Definition

### 2.1 Defining Nodes

Nodes are defined by subclassing `DocNode` and declaring fields using
standard Pydantic field annotations:

```python
from atomdoc import DocNode

class PageNode(DocNode, node_type="page"):
    title: str = ""
    body: str = ""
```

The `node_type` class keyword argument is required and must be unique
within a document. It is used for serialization and node lookup.

Once a node is live in a document, its fields are plain attributes:

```python
page = doc.root
print(page.title)       # "" — just a string
page.title = "Hello"    # assignment triggers operation tracking
print(type(page.title)) # <class 'str'> — not a wrapper
```

### 2.2 Defining Atomic Types

Atomic types are Pydantic `BaseModel` subclasses with `frozen=True`:

```python
from pydantic import BaseModel, Field

class Vec3(BaseModel, frozen=True):
    x: float = 0.0
    y: float = 0.0
    z: float = 0.0

class WindowLevel(BaseModel, frozen=True):
    window: float = Field(gt=0)
    level: float = 0.0
```

Atomic types may include validators:

```python
class Mat4(BaseModel, frozen=True):
    values: tuple[float, ...] = (1,0,0,0, 0,1,0,0, 0,0,1,0, 0,0,0,1)

    @model_validator(mode="after")
    def check_length(self):
        if len(self.values) != 16:
            raise ValueError("Mat4 requires exactly 16 values")
        return self
```

### 2.3 Composing Nodes

Nodes may use atomic types, primitives, and opaque fields freely:

```python
class AnnotationNode(DocNode, node_type="annotation"):
    # Tier 1: Mergeable
    label: str = ""
    opacity: float = 1.0
    visible: bool = True

    # Tier 2: Atomic
    color: Color = Color(r=255, g=0, b=0)
    transform: Mat4 = Mat4()
    position: Vec3 = Vec3()

    # Tier 3: Opaque
    thumbnail: bytes = b""
```

### 2.4 Node Mixins

Reusable field groups are defined as Pydantic models and composed via
multiple inheritance:

```python
class Timestamped(BaseModel):
    created_at: datetime = Field(default_factory=datetime.now)
    updated_at: datetime = Field(default_factory=datetime.now)

class ProjectNode(DocNode, Timestamped, node_type="project"):
    name: str = ""
```

---

## 3. Document Lifecycle

### 3.1 Creating a Document

```python
from atomdoc import Doc

doc = Doc(root_type="page", nodes=[PageNode, AnnotationNode])
```

- `root_type` identifies the node type for the root node.
- `nodes` registers all node types the document may contain.
- The root node is created automatically with default state.
- The document id is a lowercase ULID, auto-generated or user-supplied.

### 3.2 Creating Nodes

```python
node = doc.create_node(AnnotationNode)
```

Returns a new unattached node with default state. The node must be
inserted into the tree before the transaction commits.

### 3.3 Children as a Sequence

A node's children are accessed via the `children` property, which
behaves like a read-only Python sequence:

```python
len(node.children)       # number of direct children
node.children[0]         # first child (IndexError if empty)
node.children[-1]        # last child
node.children[1:3]       # slice — returns a list

for child in node.children:
    print(child)

if node.children:        # truthy when non-empty
    ...
```

Mutations go through methods on the node, not on the sequence:

```python
parent.append(child)
parent.prepend(child)
node.insert_after(new_sibling)
node.insert_before(new_sibling)
node.delete()
node.move(new_parent, position="append")
```

Range operations on contiguous siblings:

```python
node_a.to(node_c).delete()
node_a.to(node_c).move(new_parent, position="append")
```

### 3.4 State Access and Mutation

**Reads** return plain Python values — not proxies, not wrappers:

```python
title = node.title          # str
color = node.color          # Color (frozen Pydantic model)
r = node.color.r            # int — nested attribute access works
visible = node.visible      # bool
```

The return types are exactly what the schema declares. A `str` field
returns a `str`. A `Color` field returns a `Color` instance. Type
checkers (mypy, pyright) infer the correct types from the class
definition with no extra annotation.

**Writes** use plain assignment. Descriptors intercept the write to
track operations — but this is invisible to the caller:

```python
node.title = "New title"           # generates one mergeable operation
node.color = Color(r=0, g=0, b=0)  # generates one atomic operation
node.visible = False                # generates one mergeable operation
```

Writes are validated through Pydantic before being accepted. Assigning
an invalid value raises `ValidationError` and the state is unchanged.

### 3.5 Transactions

All mutations occur within a transaction. Transactions batch multiple
mutations into a single change event.

**Implicit transactions:** A transaction opens automatically on the first
mutation and commits at the end of the current event-loop tick (via
`asyncio` or synchronous flush).

**Explicit transactions:** Use a context manager for clarity and
guaranteed boundaries:

```python
with doc.transaction():
    node.label = "Updated"
    child.delete()
    new_node = doc.create_node(AnnotationNode)
    doc.root.append(new_node)
# on_change fires here with a single diff
```

If an exception is raised inside a transaction, all mutations are rolled
back and no change event is emitted.

### 3.6 Change Events

```python
@doc.on_change
def handle(event):
    event.operations    # forward operations (for sync)
    event.diff          # summary: inserted, deleted, moved, updated node ids
```

### 3.7 Normalization

Extensions may register normalization hooks that run after mutations and
before the change event:

```python
@doc.on_normalize
def ensure_minimum_children(diff):
    if not doc.root.children:
        doc.root.append(doc.create_node(PageNode))
```

Normalizers may mutate the document. In strict mode, normalization runs
twice to verify idempotency.

---

## 4. Tree Traversal

### 4.1 Children

Direct children are accessible via `node.children` (see 3.3).

### 4.2 Deeper Traversal

Generators provide lazy traversal without allocating lists:

```python
for ancestor in node.ancestors():      # walk up to root
    ...

for desc in node.descendants():        # depth-first, all levels
    ...

for sib in node.next_siblings():       # forward from this node
    ...

for sib in node.prev_siblings():       # backward from this node
    ...
```

### 4.3 Navigation Properties

```python
node.parent         # parent node, or None for root
node.next_sibling   # next sibling, or None
node.prev_sibling   # previous sibling, or None
```

### 4.4 Type Narrowing

`isinstance` works naturally because each node type is a real Python
class:

```python
for child in doc.root.children:
    if isinstance(child, AnnotationNode):
        print(child.color)  # type checker knows this is Color
        print(child.label)  # and this is str
```

No special `.is()` method needed — Python's own type system handles it.

---

## 5. Operations

### 5.1 Operation Types

AtomDoc tracks three structural operation types and one state patch type:

| Op | Code | Payload |
|----|------|---------|
| Insert | `0` | `[node_data, parent_id, prev_id, next_id]` |
| Delete | `1` | `[start_id, end_id]` |
| Move | `2` | `[start_id, end_id, parent_id, prev_id, next_id]` |
| State | — | `{node_id: {field: serialized_value}}` |

Operations are stored as compact tuples for serialization efficiency.

### 5.2 Inverse Operations

Every operation has a corresponding inverse, generated at mutation time
and stored separately. This enables undo without duplicating logic.

### 5.3 Applying Remote Operations

```python
doc.apply_operations(operations)
```

Applies a list of operations received from a sync layer. This triggers
normalization and change events but does not generate inverse operations
(remote changes are not locally undoable by default).

---

## 6. Undo / Redo

```python
from atomdoc import UndoManager

undo = UndoManager(doc, max_steps=100)

undo.undo()
undo.redo()
undo.can_undo  # bool
undo.can_redo  # bool
```

The undo manager listens to change events and stores inverse operations.
Undo applies the inverse; redo applies the inverse of the inverse.

---

## 7. Serialization

### 7.1 JSON Format

Documents serialize to a recursive tuple structure:

```json
[doc_id, root_type, [
  [node_id, node_type, {state}, [
    [child_id, child_type, {state}],
    [child_id, child_type, {state}, [grandchildren...]]
  ]]
]]
```

State values are serialized via Pydantic's `model_dump(mode="json")` for
atomic types and directly for primitives. Default values are excluded via
`exclude_defaults=True` to minimize payload size.

### 7.2 Deserialization

```python
doc = Doc.from_json(data, nodes=[PageNode, AnnotationNode])
```

Deserialization validates all state through Pydantic's `model_validate`,
ensuring no invalid values enter the document from external sources.

### 7.3 Schema Export

```python
Doc.json_schema(nodes=[PageNode, AnnotationNode])
```

Returns a JSON Schema describing the document structure, derived from
Pydantic's `model_json_schema()`. Useful for external tool integration,
documentation generation, and UI scaffolding.

---

## 8. Tier Classification

The operation tracking layer classifies fields automatically at node
registration time:

```python
def classify_field(field_info) -> Literal["mergeable", "atomic", "opaque"]:
    annotation = field_info.annotation
    if annotation is bytes:
        return "opaque"
    if is_frozen_model(annotation):
        return "atomic"
    return "mergeable"
```

This classification determines:

- **Diffing:** Mergeable fields diff by value. Atomic and opaque fields
  diff by identity (changed or not).
- **Merge conflict resolution:** Mergeable fields on different keys merge
  cleanly. Atomic fields use last-write-wins on the whole value.
- **Operation granularity:** Each mergeable field produces its own
  operation. Each atomic field produces one operation regardless of how
  many sub-fields differ.

---

## 9. Node ID Generation

Node IDs use a Lamport-timestamp scheme identical to DocNode's:

- The document id is a lowercase ULID (128 bits).
- Child node ids are `{session_id}.{clock}` where session_id encodes
  time-since-document-creation plus randomness, and clock is a monotonic
  counter in a compact base64 encoding.
- IDs are lexicographically sortable and shorter than full ULIDs.

---

## 10. Extension System

Extensions register node types and normalization hooks:

```python
from atomdoc import Extension

editor_ext = Extension(
    nodes=[PageNode, AnnotationNode],
    normalize=ensure_minimum_children,
)

doc = Doc(root_type="page", extensions=[editor_ext])
```

Extensions are the unit of composition for applications that need to
combine multiple sets of node types and behaviors.

---

## 11. Design Constraints

- **Plain Python surface.** Reading a node must feel like reading a
  dataclass. No `.get()`, no `.value`, no proxy objects on the read
  path. `type(node.title)` returns `str`, not a wrapper. `node.children`
  supports `len`, indexing, slicing, iteration, and truthiness.
- **No partial atomic updates.** If a field's type is a frozen model, the
  only valid mutation is full replacement. The library does not provide
  sub-field setters for atomic types.
- **No implicit merge of atomic types.** Two concurrent edits to the same
  atomic field always resolve via last-write-wins, never by merging
  sub-fields.
- **Validation is mandatory.** All state entering the document — from local
  code, deserialization, or sync — passes through Pydantic validation.
  There is no bypass.
- **Schema is source of truth.** The tier classification, serialization
  format, and merge policy are all derived from the Pydantic schema. No
  separate configuration.
- **Pydantic is optionally visible.** Naive consumers interact with plain
  Python objects and never need to import Pydantic. Power users can drop
  to the Pydantic layer for schema introspection, custom validation, or
  serialization control. Mutation APIs may use Pydantic conventions where
  it improves ergonomics or clarifies the boundary between reading and
  writing.

---

## 12. Dependencies

- **Python** >= 3.12
- **Pydantic** >= 2.0

No other runtime dependencies. Sync, storage, and UI bindings are
separate packages.

---

## 13. Package Structure

```
atomdoc/
    __init__.py          # public API: Doc, DocNode, Extension, UndoManager
    doc.py               # Doc class
    node.py              # DocNode base class, descriptors, tree structure
    operations.py        # operation tracking, inverse generation, apply
    transaction.py       # transaction context manager, batching, rollback
    undo.py              # UndoManager
    tier.py              # field classification (mergeable/atomic/opaque)
    id.py                # node ID generation (Lamport timestamps)
    types.py             # shared type definitions
```

---

## 14. Future Work

Items explicitly out of scope for v0.1 but anticipated:

- **Sync protocol** (equivalent to DocSync) — separate package.
- **Nested documents** (subdocs) — documents as values within documents.
- **Transaction merging** — combining sequential undo steps by time
  interval.
- **Collaborative undo** — per-session undo in multi-client contexts.
- **List and map state types** — ordered collections as a fourth tier with
  CRDT-style element-level merging.
