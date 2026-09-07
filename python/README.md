# AtomDoc

> **This is a conceptual prototype for a collaborative document system
> based on Pydantic models. It is both conceptually and directly based on
> [DocuKit](https://github.com/docukit/docukit). This project is fully
> unsupported and intended for exploration only.**

AtomDoc explores local-first document models for Python with type-safe
schemas, semantic atomicity, and operation tracking.

A companion TypeScript client package is available at
[atomdoc-ts](https://github.com/mhalle/atomdoc-ts).

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

## Features

- **Core document model**: `@node` decorator, `Array[T]` slots, frozen value types (Pydantic), transactions, built-in undo/redo with merge interval and history transfer
- **Server protocol layer**: `Session`, `Transport` (abstract), `WebSocketTransport`
- **Wire protocol**: schema/snapshot/patch messages (server to client), op/create/undo/redo (client to server)
- **Schema export**: `atomdoc_schema()` with `x-atomdoc` extensions (field tiers, slots, references, value types)
- **References**: `Ref[T]` fields point at other nodes in the same document, with a reverse index and referential integrity checked at commit
- **Validation**: full Pydantic validation at transaction commit time
- **Extensions**: bundle node types and normalization hooks
- **Full test suite**: 285 tests

## Requirements

Python 3.12+ and Pydantic 2.

For the server protocol layer, install with the `server` extra:

```bash
pip install atomdoc[server]
```

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
    doc.root.annotations.remove(ann)                # same, by node
    doc.root.annotations.clear()                    # remove all
```

Removal is deletion: a node cannot be detached and kept. To put a node
somewhere else, move it, which is one operation and does not trip
reference integrity. Appending a node that is already in the document
raises; nodes are moved within or across child arrays with `move`. Append
or prepend to a slot on a new parent, or position next to a sibling:

```python
with doc.transaction():
    ann.move(other_page, "annotations")                 # append to a slot
    ann.move(other_page, "annotations", "prepend")
    ann.move(sibling, position="after")                 # next to a sibling
    first.to(last).move(sibling, position="before")     # a contiguous range
```

### References

`Array[T]` is **ownership**: every node has exactly one owner and one
position in the tree. `Ref[T]` is **association**: a field that points at
another node in the same document without controlling its lifetime.

```python
from atomdoc import Ref

@node
class Transform:
    name: str = ""
    parent: Ref["Transform"] | None = None      # self-reference

@node
class Volume:
    transform: Ref[Transform] | None = None     # one target, optional
    sources: list[Ref["Volume"]] = []           # many targets

@node
class Scene:
    transforms: Array[Transform] = []
    volumes: Array[Volume] = []
```

Reading a reference resolves it to the node; assigning accepts a node or
its ID. On the wire and in `dump()` the value is the target's node ID; in
`to_json()`, which carries no IDs, it is the target's document path
(`"/transforms/2"`), or `null` if it does not resolve.

```python
with doc.transaction():
    vol.transform = t2                  # or t2.id
vol.transform is t2                     # True
vol.ref_id("transform")                 # "-4_D.0" — the stored ID
doc.referrers(t2)                       # [vol]
doc.referrers(t2, field="transform")    # only via that field
```

The document keeps a reverse index and checks **referential integrity at
transaction commit**, where whole-document invariants belong:

- every reference must resolve to a node of the declared type, and
- a node that is still referenced cannot be deleted (delete policy
  `restrict`).

A violation raises `RefIntegrityError` and rolls the transaction back.
Because the check runs at commit, re-pointing referrers and deleting the
old target in one transaction is fine. Moving a node is not a delete, so
reparenting never trips the check. Undo restores deleted nodes with their
original IDs, so references survive undo and redo.

The index is derived state: it is never serialized, and `Doc.restore`
rebuilds it. A dump whose references do not resolve fails to restore in
strict mode and warns otherwise, leaving the field reading as `None`.

References never cross documents. To point at something outside the
document — another document's node, a file, an ontology term — use a
[handle](#handles). Choosing `Ref` over a handle asserts that the two
nodes ship together.

### Handles

A `Handle` is a frozen value naming something outside the document. It
is an ordinary atomic field, so undo and sync move only the handle, never
the data behind it. Each handle type declares its **strength**: whether
the document is usable without resolving it.

```python
from atomdoc import Handle

class VoxelData(Handle):          # the document is useless without it
    strength = "strong"

class Terminology(Handle):        # nice to have; weak is the default
    pass

@node
class Volume:
    data: VoxelData | None = None
    term: Terminology | None = None

with doc.transaction():
    vol.data = VoxelData(uri="s3://bucket/vol.nii.gz", digest="sha256:...")

doc.handles(strength="strong")    # [(vol, "data", VoxelData(...))]: the hard
                                  # dependency list, without resolving anything
```

`Handle` has `uri`, and optional `media_type` and `digest`; subclasses may
add fields. Declare `strength` as a plain class attribute, not an
annotated field. Declare `strength` as a plain class attribute, not an
annotated field. Strength is exported per field (`handles` in the schema), so a
service can answer "can I open this?" from the schema and a dump alone.

### Heterogeneous values

Two tools cover values whose type varies:

- **A closed set:** a union of frozen models. It is one atomic value on
  the wire, and with a Pydantic discriminator it exports as a tagged
  `oneOf`.

  ```python
  class Scalar(BaseModel, frozen=True):
      kind: Literal["scalar"] = "scalar"
      v: float = 0.0

  class Vec3(BaseModel, frozen=True):
      kind: Literal["vec3"] = "vec3"
      v: tuple[float, float, float] = (0.0, 0.0, 0.0)

  @node
  class Override:
      target: Ref[Volume] | None = None
      field: str = ""
      value: Annotated[Scalar | Vec3, Field(discriminator="kind")] = Scalar()
  ```

- **An open set:** `JsonValue` (re-exported from Pydantic). Any JSON value,
  stored natively, mergeable, exported as an unconstrained property. The
  schema cannot say more, so validate it yourself where the rule lives —
  for a `(target, field, value)` override, a normalizer that looks up the
  target's field adapter and validates `value` against it.

### Composition

A document is the smallest unit whose references all resolve, and so the
smallest unit that ships. Documents compose by **adoption**: a subtree
dumped from one document is inserted into another, keeping its IDs.

```python
fragment = scene_doc.dump(scene_doc.root)           # or any node
with library.transaction():
    [scene] = library.adopt(fragment, library.root, "scenes")
```

Nothing inside the fragment is rewritten — references between its nodes
stay intact and outside citations of its node IDs remain valid. The only
exception is an ID that already exists in the receiving document, which
is re-minted (and references to it inside the fragment follow). A
reference from the fragment to a node it does not contain must resolve
in the receiving document; the commit-time check enforces that. Adoption
emits ordinary insert operations, so connected clients reproduce it.

Decomposition is not symmetric: pulling a member back out means deciding,
per reference that crosses the boundary, whether it becomes a handle.
Reparenting alone keeps members extractable; promoting handles to
references afterwards is what welds them together.

When two adopted fragments each carry a node for the same external thing
(the same terminology code, the same coordinate frame), both nodes stay.
Sameness is then a handle comparison, not a reference comparison — which
is why such nodes should hold a handle in the first place.

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

Two formats — clean JSON for reading, wire format for persistence. The
clean form nests children and omits node IDs, except that a `Ref` field
is emitted as the target's ID, since that is its value; use `dump()`
when the output must round-trip:

```python
# Clean JSON — no internal IDs, just data
doc.to_json()
# {"title": "Hello", "annotations": [{"label": "Important", "color": {"r": 255, "g": 0, "b": 0}}]}

# Wire format — includes IDs, for dump/restore and operation replay
wire = doc.dump()
doc2 = Doc.restore(wire, root_type=Page)
```

### Undo / redo

Every document owns an undo manager. It is disabled by default; enable it
with `UndoManagerConfig`:

```python
from atomdoc import Doc, UndoManagerConfig

doc = Doc(Page(title="Hello"), undo_manager=UndoManagerConfig(max_steps=100))

with doc.transaction():
    doc.root.title = "Changed"

doc.undo_manager.undo()
assert doc.root.title == "Hello"

doc.undo_manager.redo()
assert doc.root.title == "Changed"
```

`merge_interval` (seconds) collapses transactions committed in quick
succession into one undo step, so keystroke-level edits undo together:

```python
doc = Doc(Page(), undo_manager=UndoManagerConfig(max_steps=100, merge_interval=0.5))
```

Transactions can be excluded from undo history. Use this when applying
operations that came from a remote peer:

```python
doc.apply_operations(remote_ops, skip_undo=True)

with doc.transaction(skip_undo=True):
    ...
```

A `skip_undo` transaction is always isolated. If one is opened while
another transaction is in progress, the open transaction is committed
first, so your own pending edits keep their undo entry and only the
flagged work is excluded.

Undo history can be moved between two documents with the same ID and root
type, for example when a document is rebuilt from a newer snapshot:

```python
history = doc.undo_manager.export_history()
replacement = Doc.restore(snapshot, root_type=Page, undo_manager=UndoManagerConfig(max_steps=100))
replacement.undo_manager.import_history(history)
```

The exported history is plain JSON-serializable data. Its `last_update`
timestamp comes from the exporting manager's clock (wall-clock seconds by
default), so a merge window can continue across the transfer only when
both managers share a clock.

A standalone `UndoManager(doc)` can still be created; it defaults to 100
steps.

### Change events

```python
doc.on_change(lambda event: print(
    "inserted:", event.diff.inserted,
    "deleted:", event.diff.deleted,
    "updated:", event.diff.updated,
))
```

Change events fire once per transaction, after all mutations,
normalization and validation are complete. Each event carries forward and
inverse operations for sync and undo, plus `event.flags`
(`TransactionFlags`), whose `skip_undo` tells listeners the transaction
was excluded from undo.

Listeners are observers of a commit that is already final. Every listener
runs whatever the others do, and a listener that raises cannot roll the
commit back: other listeners (a session broadcasting the change, a UI
store) have already acted on it. Their failures are collected and raised
to the caller afterwards as a `ListenerError`, whose `errors` holds each
exception and whose `__cause__` is the first. Validation that should veto
a commit belongs in model validators or normalizers, which run before.

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

Transactions nest by joining: an inner `with doc.transaction()` (or any
mutation, or `apply_operations`) inside an open one becomes part of it.
There are no savepoints, so a failure inside the inner block propagates
and the outermost transaction rolls back as a whole. Every mutation
validates before it records anything, so an exception you catch inside
a transaction leaves it consistent.

Node handles stay valid across undo and rollback: a node deleted and then
restored is the same Python object.

## Server protocol

AtomDoc includes a server protocol layer for connecting clients to a
shared document over WebSocket (or any custom transport).

### Session and transport

```python
import asyncio
from atomdoc import Doc, Session
from atomdoc._ws_transport import WebSocketTransport

doc = Doc(Page(title="Shared"))
session = Session(doc)

async def main():
    transport = WebSocketTransport(host="localhost", port=8765)
    await session.bind(transport)
    # Server is now accepting WebSocket connections
    await asyncio.Future()  # run forever

asyncio.run(main())
```

A client's `undo` and `redo` requests act on history the session keeps
for it, chosen by the `undo` policy:

```python
Session(doc)                    # "per-client": a client reverts only its own commits
Session(doc, undo="global")     # any client reverts the document's last commit
Session(doc, undo="none")       # undo/redo requests are refused
```

Per-client is the default because it is what a thick client does locally,
so both client kinds agree, and because it is safe with several users: a
step that no longer applies (someone else edited what it would revert) is
rejected and kept for a retry. Global is right when one user looks at the
document through several views. The host's own `doc.undo_manager` is
separate from all of this.

### Wire protocol

Messages from server to client:

| Message | Description |
|---------|-------------|
| `schema` | JSON Schema with `x-atomdoc` extensions (sent on connect) |
| `snapshot` | Full document state (sent on connect) |
| `patch` | Incremental operations (broadcast after each change). `ref` is the `ref` of the client request that produced it (`null` for a host-side change). `source_client` is set only when the patch is the verbatim echo of that client's `op`; a `create`, `undo` or `redo` result, or an `op` a normalizer changed, has `source_client: null` because the requester never applied those operations locally. |
| `error` | Error response. `code` is `unknown_type` or `invalid_op` for a malformed request, `unsupported` for a request kind the session refuses (undo under `undo="none"`), or `rejected` when a well-formed request is invalid against the current document (a dangling reference, a validation failure, a node that is gone, an undo step that no longer applies). A rejected request is rolled back and not broadcast; the sender of a rejected `op` or `create` then receives a fresh `snapshot` to replace its local copy (an undo step applied nothing optimistically, so no snapshot follows). |

Messages from client to server:

| Message | Description |
|---------|-------------|
| `op` | Apply operations to the document |
| `create` | Create a new node and insert it into a slot |
| `undo` | Undo one or more steps of the requester's history (see the `undo` policy) |
| `redo` | Redo one or more steps |

### Custom transports

Implement the `Transport` abstract class and `ClientConnection` to use
any communication channel (HTTP long-polling, WebRTC, etc.):

```python
from atomdoc import Transport, ClientConnection

class MyTransport(Transport):
    async def start(self, on_connect, on_message, on_disconnect):
        ...

    async def stop(self):
        ...
```

## Schema export

`Doc.atomdoc_schema()` produces a JSON Schema document with `x-atomdoc`
extensions that describe field tiers, slots, and value types. This
enables language-agnostic clients to understand the document structure
without importing Python code.

```python
schema = doc.atomdoc_schema()
# Returns a dict with node types, field tiers (mergeable, atomic,
# opaque, ref), slot definitions, reference declarations (target type,
# cardinality, delete policy), field defaults, and frozen value type
# schemas. ``Field(...)`` constraints such as ``ge``/``le`` are included.
```

The export carries the declarative part of the schema only. Validators
written as Python functions (`@field_validator`, `@model_validator`) do not
travel with it, so a remote client can check shape but not every rule:
the document owner commits, and the owner's validators are the gate. The
same split applies to references: **the schema validates shape, the
document validates integrity.**

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
| **Atomic** | union of `frozen=True` models | A tagged union of values. Still one value, replaced as a unit. |
| **Ref** | `Ref[T]`, `list[Ref[T]]` | A node ID. Replaced as a unit; referential integrity checked at commit. |

`Array[T]` is not a field tier: it is a child slot, taken out of the state
before fields are classified, with its own per-node insert, delete, and
move operations.

Bulk data (a large image, a mesh, an external store) should not go in a
`bytes` field: the opaque tier is copied into every snapshot and patch
that touches it. Store a [`Handle`](#handles) instead, so undo and sync
move only the handle.

## Extensions

Bundle node types with a registration hook. `register` receives the
document during construction and may register normalizers, attach change
listeners, and mutate the document:

```python
from atomdoc import Extension

def register(doc):
    def ensure_has_page(diff):
        if not doc.root.pages:
            doc.root.pages.append(doc.create_node(Page))

    doc.on_normalize(ensure_has_page)

ext = Extension(nodes=[Page, Annotation], register=register)

doc = Doc(Document, extensions=[ext])
assert len(doc.root.pages) == 1  # normalizers run on construction
```

Normalizers run after mutations and before the change event, and once
when the document is constructed or restored so invariants hold from the
start. Nothing done during construction enters undo history. In strict
mode (the default), normalizers run twice to verify idempotency.

`Extension(normalize=fn)` is a shorthand for registering one normalizer
that does not need a reference to the document.

## Node IDs

Document IDs are lowercase ULIDs by default, and child nodes get compact
IDs derived from the root's creation timestamp. Supply a
`NodeIdGenerator` to change this:

```python
from atomdoc import NodeIdGenerator

gen = NodeIdGenerator(
    generate=lambda: str(uuid.uuid4()),
    validate=lambda s: len(s) == 36,
)
doc = Doc(Page(), node_id_generator=gen)
```

Without `extract_time`, `generate` is used for every node and every ID is
validated on `Doc.restore`. With `extract_time` (returning milliseconds
since the epoch), child nodes keep the compact scheme and only the
document ID is validated.

All peers of a document must use the same ID scheme. Node IDs travel
inside operations, and a validating generator rejects operations that
carry IDs it does not recognize: `Doc.restore` raises, and
`apply_operations` drops the offending operation set.

## Performance

`benchmarks/bench.py` times Slicer-like scenes (transforms with
matrices and parent references, volumes with references and handles) at
several sizes and prints per-item cost and the scaling ratio between
sizes; `tests/test_scaling.py` asserts that the core operations stay
linear. On a laptop a node costs on the order of 10 microseconds to
create, a field write a few microseconds, a dump or restore a few
microseconds per node.

Things to know when a document gets large:

- Children are a linked list. Iterate them (`for v in root.volumes`) or
  take `list(root.volumes)` once; `len()` walks the list, and `[i]` is
  cheap only for a sequential scan.
- A mergeable field is written as a unit: assigning a 10,000-point list
  serializes the whole list twice (inverse and forward patch). Keep bulk
  data behind a handle.
- Every commit runs model validation for the nodes it touched and copies
  its operations for the change event; batch related writes in one
  transaction.
- A session serializes a broadcast once per client. Snapshots on connect
  are the whole document.

## Development

```bash
uv sync
uv run pytest
uv run mypy src/atomdoc
uv run ruff check src/atomdoc tests
```
