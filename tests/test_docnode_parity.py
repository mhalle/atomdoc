"""Tests for behavior ported from DocNode v0.4.

Covers: move replay keeping position, undo eviction order, Extension.register,
normalizers on init, transaction flags (skip_undo), the doc-owned undo manager,
merge interval, history export/import, pluggable node ID generators and
merge_operations.
"""

from __future__ import annotations

import json

import pytest

from atomdoc import (
    Array,
    AtomNode,
    Doc,
    Extension,
    NodeIdGenerator,
    Session,
    TransactionFlags,
    UndoManager,
    UndoManagerConfig,
    merge_operations,
    node,
)


@node
class Item:
    value: str = ""
    children: Array["Item"] = []


from pydantic import BaseModel, Field, ValidationError  # noqa: E402


def make_board_classes():
    """Board/Note node classes defined inside a function.

    This module uses ``from __future__ import annotations``, so ``Array[Note]``
    is a string here and ``Note`` is not in module globals; the resolver must
    still turn it into a slot.
    """

    @node
    class Note(BaseModel):
        text: str = ""
        opacity: float = Field(ge=0.0, le=1.0, default=1.0)

    @node
    class Board(BaseModel):
        notes: Array[Note] = []

    return Board, Note


@node
class ModuleNote(BaseModel):
    text: str = ""


UNDO = UndoManagerConfig(max_steps=10)


def make_doc(**kwargs):
    return Doc(root_type="Item", nodes=[Item], **kwargs)


def items(doc, *vals):
    return [doc.create_node(Item, value=v) for v in vals]


def values(n):
    return [c.value for c in n.children]


def seeded(*vals, **kwargs):
    doc = make_doc(**kwargs)
    with doc.transaction():
        doc.root.children.append(*items(doc, *vals))
    return doc


class FakeClock:
    def __init__(self, t: float = 0.0) -> None:
        self.t = t

    def __call__(self) -> float:
        return self.t


# ---------------------------------------------------------------------------
# Move replay keeps position
# ---------------------------------------------------------------------------


class TestMoveReplay:
    def test_undo_move_restores_mid_slot_position(self):
        doc = seeded("a", "b", "c", "d", undo_manager=UNDO)
        b, c = doc.root.children[1], doc.root.children[2]
        with doc.transaction():
            b.move(c, position="after")
        assert values(doc.root) == ["a", "c", "b", "d"]

        doc.undo_manager.undo()
        assert values(doc.root) == ["a", "b", "c", "d"]

        doc.undo_manager.redo()
        assert values(doc.root) == ["a", "c", "b", "d"]

    def test_undo_move_before_first(self):
        doc = seeded("a", "b", "c", undo_manager=UNDO)
        a, c = doc.root.children[0], doc.root.children[2]
        with doc.transaction():
            c.move(a, position="before")
        assert values(doc.root) == ["c", "a", "b"]
        doc.undo_manager.undo()
        assert values(doc.root) == ["a", "b", "c"]

    def test_undo_move_into_other_parent_and_back(self):
        doc = seeded("a", "b", "c", undo_manager=UNDO)
        a, b = doc.root.children[0], doc.root.children[1]
        with doc.transaction():
            b.move(a, "children")
        assert values(doc.root) == ["a", "c"]
        assert values(a) == ["b"]
        doc.undo_manager.undo()
        assert values(doc.root) == ["a", "b", "c"]

    def test_remote_move_replays_position(self):
        source = seeded("a", "b", "c")
        replica = Doc.restore(source.dump(), nodes=[Item])
        events = []
        source.on_change(events.append)
        a, b = source.root.children[0], source.root.children[1]
        with source.transaction():
            a.move(b, position="after")
        assert values(source.root) == ["b", "a", "c"]

        replica.apply_operations(events[-1].operations)
        assert values(replica.root) == ["b", "a", "c"]

    def test_range_move_after(self):
        doc = seeded("a", "b", "c", "d")
        a, b, d = doc.root.children[0], doc.root.children[1], doc.root.children[3]
        with doc.transaction():
            a.to(b).move(d, position="after")
        assert values(doc.root) == ["c", "d", "a", "b"]

    def test_move_after_is_noop_when_already_there(self):
        doc = seeded("a", "b")
        events = []
        doc.on_change(events.append)
        a, b = doc.root.children[0], doc.root.children[1]
        with doc.transaction():
            b.move(a, position="after")
        assert events == []

    def test_move_before_target_in_range_raises(self):
        doc = seeded("a", "b", "c")
        a, b = doc.root.children[0], doc.root.children[1]
        with pytest.raises(ValueError, match="in the range"):
            with doc.transaction():
                a.to(b).move(b, position="before")

    def test_move_next_to_root_raises(self):
        doc = seeded("a")
        a = doc.root.children[0]
        with pytest.raises(ValueError, match="root"):
            with doc.transaction():
                a.move(doc.root, position="after")

    def test_append_requires_slot_name(self):
        doc = seeded("a", "b")
        a, b = doc.root.children[0], doc.root.children[1]
        with pytest.raises(ValueError, match="slot_name"):
            with doc.transaction():
                b.move(a)


# ---------------------------------------------------------------------------
# Undo stack eviction
# ---------------------------------------------------------------------------


class TestEviction:
    def test_full_stack_drops_oldest(self):
        doc = make_doc()
        undo = UndoManager(doc, max_steps=2)
        for i in range(5):
            with doc.transaction():
                doc.root.value = f"v{i}"
        undo.undo()
        assert doc.root.value == "v3"
        undo.undo()
        assert doc.root.value == "v2"
        assert not undo.can_undo


# ---------------------------------------------------------------------------
# Extension.register and normalizers on init
# ---------------------------------------------------------------------------


def ensure_default_child(doc_ref):
    def normalize(diff):
        if not doc_ref.root.children:
            doc_ref.root.children.append(doc_ref.create_node(Item, value="default"))

    doc_ref.on_normalize(normalize)


class TestRegisterAndInit:
    def test_register_receives_doc_and_can_register_normalizer(self):
        ext = Extension(nodes=[Item], register=ensure_default_child)
        doc = Doc(root_type="Item", extensions=[ext])
        assert values(doc.root) == ["default"]

    def test_register_can_mutate_document(self):
        def register(doc_ref):
            doc_ref.root.children.append(doc_ref.create_node(Item, value="seeded"))

        ext = Extension(nodes=[Item], register=register)
        doc = Doc(root_type="Item", extensions=[ext], undo_manager=UNDO)
        assert values(doc.root) == ["seeded"]
        assert not doc.undo_manager.can_undo

    def test_init_normalization_is_not_undoable(self):
        ext = Extension(nodes=[Item], register=ensure_default_child)
        doc = Doc(root_type="Item", extensions=[ext], undo_manager=UNDO)
        assert not doc.undo_manager.can_undo

    def test_change_listener_registered_in_register_sees_init_commit(self):
        events = []

        def register(doc_ref):
            doc_ref.on_change(events.append)
            doc_ref.root.children.append(doc_ref.create_node(Item, value="x"))

        Doc(root_type="Item", extensions=[Extension(nodes=[Item], register=register)])
        assert len(events) == 1
        assert len(events[0].diff.inserted) == 1

    def test_normalizer_runs_after_restore(self):
        source = make_doc()
        ext = Extension(nodes=[Item], register=ensure_default_child)
        doc = Doc.restore(source.dump(), extensions=[ext], undo_manager=UNDO)
        assert values(doc.root) == ["default"]
        assert not doc.undo_manager.can_undo

    def test_normalizer_after_restore_sees_restored_tree(self):
        source = seeded("kept")
        ext = Extension(nodes=[Item], register=ensure_default_child)
        doc = Doc.restore(source.dump(), extensions=[ext])
        assert values(doc.root) == ["kept"]

    def test_restore_does_not_fire_change_event(self):
        # Change listeners cannot be attached before restore returns, and the
        # restored tree is not recorded as operations.
        source = seeded("a", "b")
        doc = Doc.restore(source.dump(), nodes=[Item], undo_manager=UNDO)
        assert values(doc.root) == ["a", "b"]
        assert not doc.undo_manager.can_undo

    def test_on_normalize_outside_register_still_raises(self):
        doc = make_doc()
        with pytest.raises(RuntimeError):
            doc.on_normalize(lambda diff: None)


# ---------------------------------------------------------------------------
# Transaction flags
# ---------------------------------------------------------------------------


class TestTransactionFlags:
    def test_default_flags(self):
        doc = make_doc()
        events = []
        doc.on_change(events.append)
        with doc.transaction():
            doc.root.value = "x"
        assert events[0].flags == TransactionFlags()
        assert events[0].flags.skip_undo is False

    def test_transaction_skip_undo(self):
        doc = make_doc(undo_manager=UNDO)
        events = []
        doc.on_change(events.append)
        with doc.transaction(skip_undo=True):
            doc.root.value = "remote"
        assert events[0].flags.skip_undo is True
        assert doc.root.value == "remote"
        assert not doc.undo_manager.can_undo

    def test_flags_reset_after_commit(self):
        doc = make_doc(undo_manager=UNDO)
        with doc.transaction(skip_undo=True):
            doc.root.value = "remote"
        with doc.transaction():
            doc.root.value = "local"
        assert doc.undo_manager.can_undo
        doc.undo_manager.undo()
        assert doc.root.value == "remote"

    def test_flags_reset_after_abort(self):
        doc = make_doc(undo_manager=UNDO)
        with pytest.raises(RuntimeError):
            with doc.transaction(skip_undo=True):
                doc.root.value = "remote"
                raise RuntimeError("boom")
        with doc.transaction():
            doc.root.value = "local"
        assert doc.undo_manager.can_undo

    def test_apply_operations_skip_undo(self):
        source = make_doc()
        replica = Doc.restore(source.dump(), nodes=[Item], undo_manager=UNDO)
        events = []
        source.on_change(events.append)
        with source.transaction():
            source.root.value = "remote"

        replica.apply_operations(events[0].operations, skip_undo=True)
        assert replica.root.value == "remote"
        assert not replica.undo_manager.can_undo

        with replica.transaction():
            replica.root.value = "local"
        assert replica.undo_manager.can_undo
        replica.undo_manager.undo()
        assert replica.root.value == "remote"

    def test_apply_operations_skip_undo_isolates_open_transaction(self):
        source = make_doc()
        replica = Doc.restore(source.dump(), nodes=[Item], undo_manager=UNDO)
        source_events = []
        source.on_change(source_events.append)
        with source.transaction():
            source.root.children.append(source.create_node(Item, value="r"))

        replica_events = []
        replica.on_change(replica_events.append)
        with replica.transaction():
            replica.root.value = "local"
            replica.apply_operations(source_events[0].operations, skip_undo=True)
            replica.root.value = "local2"

        assert [e.flags.skip_undo for e in replica_events] == [False, True, False]
        assert values(replica.root) == ["r"]
        assert replica.root.value == "local2"
        # Two undoable steps, the remote insert is not one of them
        replica.undo_manager.undo()
        assert replica.root.value == "local"
        replica.undo_manager.undo()
        assert replica.root.value == ""
        assert values(replica.root) == ["r"]
        assert not replica.undo_manager.can_undo

    def test_standalone_manager_respects_flags(self):
        doc = make_doc()
        undo = UndoManager(doc)
        with doc.transaction(skip_undo=True):
            doc.root.value = "x"
        assert not undo.can_undo


# ---------------------------------------------------------------------------
# Doc-owned undo manager
# ---------------------------------------------------------------------------


class TestDocOwnedUndo:
    def test_disabled_by_default(self):
        doc = make_doc()
        assert not doc.undo_manager.is_enabled
        with doc.transaction():
            doc.root.value = "x"
        assert not doc.undo_manager.can_undo
        doc.undo_manager.undo()  # no-op
        assert doc.root.value == "x"

    def test_enabled_via_config(self):
        doc = make_doc(undo_manager=UndoManagerConfig(max_steps=3))
        assert doc.undo_manager.is_enabled
        assert doc.undo_manager.max_steps == 3
        with doc.transaction():
            doc.root.value = "x"
        doc.undo_manager.undo()
        assert doc.root.value == ""
        doc.undo_manager.redo()
        assert doc.root.value == "x"

    def test_undo_commits_pending_transaction_first(self):
        doc = make_doc(undo_manager=UNDO)
        with doc.transaction():
            doc.root.value = "a"
        with doc.transaction():
            doc.root.value = "b"
            doc.undo_manager.undo()
            assert doc.root.value == "a"
        assert doc.root.value == "a"

    def test_global_session_uses_enabled_doc_manager(self):
        doc = make_doc(undo_manager=UNDO)
        session = Session(doc, undo="global")
        assert session._undo is doc.undo_manager

    def test_global_session_falls_back_to_standalone_when_disabled(self):
        doc = make_doc()
        session = Session(doc, undo="global")
        assert session._undo is not doc.undo_manager
        assert session._undo.is_enabled

    def test_per_client_session_leaves_doc_manager_alone(self):
        doc = make_doc(undo_manager=UNDO)
        session = Session(doc)
        assert session._undo is None
        assert session.undo_policy == "per-client"

    def test_dispose_stops_recording(self):
        doc = make_doc(undo_manager=UNDO)
        doc.undo_manager.dispose()
        with doc.transaction():
            doc.root.value = "x"
        assert not doc.undo_manager.can_undo

    def test_clear(self):
        doc = make_doc(undo_manager=UNDO)
        with doc.transaction():
            doc.root.value = "x"
        doc.undo_manager.clear()
        assert not doc.undo_manager.can_undo


# ---------------------------------------------------------------------------
# Merge interval
# ---------------------------------------------------------------------------


class TestMergeInterval:
    def test_transactions_within_interval_collapse(self):
        doc = make_doc()
        clock = FakeClock()
        undo = UndoManager(doc, max_steps=10, merge_interval=0.5, clock=clock)
        with doc.transaction():
            doc.root.value = "a"
        clock.t = 0.1
        with doc.transaction():
            doc.root.value = "ab"
        clock.t = 0.2
        with doc.transaction():
            doc.root.children.append(doc.create_node(Item, value="c"))

        undo.undo()
        assert doc.root.value == ""
        assert values(doc.root) == []
        assert not undo.can_undo

        undo.redo()
        assert doc.root.value == "ab"
        assert values(doc.root) == ["c"]

    def test_transactions_outside_interval_stay_separate(self):
        doc = make_doc()
        clock = FakeClock()
        undo = UndoManager(doc, max_steps=10, merge_interval=0.5, clock=clock)
        with doc.transaction():
            doc.root.value = "a"
        clock.t = 1.0
        with doc.transaction():
            doc.root.value = "b"
        undo.undo()
        assert doc.root.value == "a"
        undo.undo()
        assert doc.root.value == ""

    def test_no_merge_across_undo(self):
        doc = make_doc()
        clock = FakeClock()
        undo = UndoManager(doc, max_steps=10, merge_interval=10.0, clock=clock)
        with doc.transaction():
            doc.root.value = "a"
        undo.undo()
        with doc.transaction():
            doc.root.value = "b"
        undo.undo()
        assert doc.root.value == ""
        assert not undo.can_undo

    def test_merge_disabled_by_default(self):
        doc = make_doc(undo_manager=UNDO)
        with doc.transaction():
            doc.root.value = "a"
        with doc.transaction():
            doc.root.value = "b"
        doc.undo_manager.undo()
        assert doc.root.value == "a"

    def test_doc_config_merge_interval(self):
        doc = make_doc(undo_manager=UndoManagerConfig(max_steps=5, merge_interval=60.0))
        assert doc.undo_manager.merge_interval == 60.0
        with doc.transaction():
            doc.root.value = "a"
        with doc.transaction():
            doc.root.value = "b"
        doc.undo_manager.undo()
        assert doc.root.value == ""


# ---------------------------------------------------------------------------
# History export / import
# ---------------------------------------------------------------------------


class TestHistoryTransfer:
    def _edited(self):
        doc = seeded("a", "b", undo_manager=UNDO)
        with doc.transaction():
            doc.root.value = "title"
        with doc.transaction():
            doc.root.children[0].delete()
        doc.undo_manager.undo()  # leaves one redo entry
        return doc

    def test_roundtrip_into_replacement_document(self):
        doc = self._edited()
        history = doc.undo_manager.export_history()
        assert history["doc_id"] == doc.id
        assert history["doc_type"] == "Item"
        assert len(history["undo_stack"]) == 2
        assert len(history["redo_stack"]) == 1

        replacement = Doc.restore(doc.dump(), nodes=[Item], undo_manager=UNDO)
        replacement.undo_manager.import_history(history)
        assert replacement.undo_manager.can_undo
        assert replacement.undo_manager.can_redo

        replacement.undo_manager.redo()
        assert values(replacement.root) == ["b"]
        replacement.undo_manager.undo()
        assert values(replacement.root) == ["a", "b"]
        replacement.undo_manager.undo()
        assert replacement.root.value == ""
        replacement.undo_manager.undo()
        assert values(replacement.root) == []

    def test_export_is_json_serializable_and_importable(self):
        doc = self._edited()
        payload = json.loads(json.dumps(doc.undo_manager.export_history()))
        replacement = Doc.restore(doc.dump(), nodes=[Item], undo_manager=UNDO)
        replacement.undo_manager.import_history(payload)
        replacement.undo_manager.undo()
        assert replacement.root.value == ""

    def test_export_commits_pending_transaction(self):
        doc = make_doc(undo_manager=UNDO)
        with doc.transaction():
            doc.root.value = "pending"
            history = doc.undo_manager.export_history()
        assert len(history["undo_stack"]) == 1

    def test_export_is_a_copy(self):
        doc = self._edited()
        history = doc.undo_manager.export_history()
        history["undo_stack"][0]["operations"][1].clear()
        doc.undo_manager.undo()
        assert doc.root.value == ""

    def test_import_rejects_other_document(self):
        doc = self._edited()
        other = make_doc(undo_manager=UNDO)
        with pytest.raises(ValueError, match="different document"):
            other.undo_manager.import_history(doc.undo_manager.export_history())

    @pytest.mark.parametrize(
        "bad",
        [
            None,
            {},
            {"doc_id": "x", "doc_type": "Item", "undo_stack": [], "redo_stack": "no"},
            {
                "doc_id": "x",
                "doc_type": "Item",
                "undo_stack": [{"operations": [[[9, "a"]], {}], "meta": {}}],
                "redo_stack": [],
            },
        ],
    )
    def test_import_rejects_invalid(self, bad):
        doc = make_doc(undo_manager=UNDO)
        with pytest.raises(TypeError, match="Invalid undo history"):
            doc.undo_manager.import_history(bad)

    def test_import_truncates_to_max_steps(self):
        doc = make_doc(undo_manager=UNDO)
        for i in range(5):
            with doc.transaction():
                doc.root.value = f"v{i}"
        history = doc.undo_manager.export_history()
        small = Doc.restore(doc.dump(), nodes=[Item], undo_manager=UndoManagerConfig(max_steps=2))
        small.undo_manager.import_history(history)
        small.undo_manager.undo()
        assert small.root.value == "v3"
        small.undo_manager.undo()
        assert small.root.value == "v2"
        assert not small.undo_manager.can_undo

    def test_import_into_disabled_manager_keeps_nothing(self):
        doc = self._edited()
        target = Doc.restore(doc.dump(), nodes=[Item])
        target.undo_manager.import_history(doc.undo_manager.export_history())
        assert not target.undo_manager.can_undo


# ---------------------------------------------------------------------------
# Node ID generators
# ---------------------------------------------------------------------------


def counter_generator(prefix="n"):
    count = 0

    def generate():
        nonlocal count
        count += 1
        return f"{prefix}{count}"

    return NodeIdGenerator(generate=generate, validate=lambda s: s.startswith(prefix))


class TestNodeIdGenerator:
    def test_generator_without_extract_time_is_used_for_every_node(self):
        doc = make_doc(node_id_generator=counter_generator())
        assert doc.id == "n1"
        child = doc.create_node(Item)
        assert child.id == "n2"

    def test_custom_validate_applies_to_doc_id(self):
        with pytest.raises(ValueError, match="Invalid document id"):
            make_doc(node_id_generator=counter_generator(), doc_id="bad")
        doc = make_doc(node_id_generator=counter_generator(), doc_id="n-custom")
        assert doc.id == "n-custom"

    def test_restore_validates_all_ids_without_extract_time(self):
        gen = counter_generator()
        doc = seeded("a", node_id_generator=gen)
        data = doc.dump()
        data[3]["children"][0][0] = "bad"
        with pytest.raises(ValueError, match="Invalid node id"):
            Doc.restore(data, nodes=[Item], node_id_generator=counter_generator())

    def test_restore_accepts_valid_custom_ids(self):
        doc = seeded("a", node_id_generator=counter_generator())
        restored = Doc.restore(doc.dump(), nodes=[Item], node_id_generator=counter_generator())
        assert values(restored.root) == ["a"]

    def test_generator_with_extract_time_uses_compact_child_ids(self):
        from ulid import ULID

        gen = NodeIdGenerator(
            generate=lambda: str(ULID()).lower(),
            validate=lambda s: len(s) == 26,
            extract_time=lambda s: ULID.from_str(s.upper()).milliseconds,
        )
        doc = make_doc(node_id_generator=gen)
        child = doc.create_node(Item)
        assert "." in child.id

    def test_extract_time_failure_is_wrapped(self):
        def boom(_):
            raise RuntimeError("nope")

        gen = NodeIdGenerator(generate=lambda: "id", validate=lambda s: True, extract_time=boom)
        with pytest.raises(ValueError, match="Failed to extract time"):
            make_doc(node_id_generator=gen)

    def test_default_generator_is_exposed(self):
        doc = make_doc()
        assert doc.node_id_generator.extract_time is not None
        assert doc.node_id_generator.validate(doc.id)


# ---------------------------------------------------------------------------
# merge_operations
# ---------------------------------------------------------------------------


def test_merge_operations_concatenates_and_merges_state():
    a = ([(1, "x", 0)], {"n1": {"value": "a", "other": 1}})
    b = ([(1, "y", 0)], {"n1": {"value": "b"}, "n2": {"value": "c"}})
    merged = merge_operations(a, b)
    assert merged[0] == [(1, "x", 0), (1, "y", 0)]
    assert merged[1] == {"n1": {"value": "b", "other": 1}, "n2": {"value": "c"}}
    # inputs untouched
    assert a[1] == {"n1": {"value": "a", "other": 1}}


# ---------------------------------------------------------------------------
# Follow-up coverage
# ---------------------------------------------------------------------------


class TestNestedSkipUndo:
    def test_nested_skip_undo_isolates_outer_edits(self):
        doc = make_doc(undo_manager=UNDO)
        events = []
        doc.on_change(events.append)
        with doc.transaction():
            doc.root.value = "a"
            with doc.transaction(skip_undo=True):
                doc.root.value = "remote"
            doc.root.value = "b"

        assert [e.flags.skip_undo for e in events] == [False, True, False]
        assert doc.root.value == "b"
        doc.undo_manager.undo()
        assert doc.root.value == "remote"
        doc.undo_manager.undo()
        assert doc.root.value == ""
        assert not doc.undo_manager.can_undo

    def test_skip_undo_does_not_leak_into_following_edits(self):
        doc = make_doc(undo_manager=UNDO)
        with doc.transaction():
            with doc.transaction(skip_undo=True):
                doc.root.value = "remote"
            doc.root.children.append(doc.create_node(Item, value="mine"))
        assert doc.undo_manager.can_undo
        doc.undo_manager.undo()
        assert values(doc.root) == []
        assert doc.root.value == "remote"

    def test_journal_with_skip_undo(self):
        source = make_doc()
        replica = Doc.restore(source.dump(), nodes=[Item], undo_manager=UNDO)
        events = []
        source.on_change(events.append)
        with source.transaction():
            source.root.value = "one"
        with source.transaction():
            source.root.children.append(source.create_node(Item, value="two"))

        replica_events = []
        replica.on_change(replica_events.append)
        remaining = replica.apply_operations(
            [e.operations for e in events], skip_undo=True
        )
        assert remaining == []
        assert replica.root.value == "one"
        assert values(replica.root) == ["two"]
        assert [e.flags.skip_undo for e in replica_events] == [True, True]
        assert not replica.undo_manager.can_undo

    def test_journal_limit_with_skip_undo(self):
        source = make_doc()
        replica = Doc.restore(source.dump(), nodes=[Item], undo_manager=UNDO)
        events = []
        source.on_change(events.append)
        for v in ("one", "two", "three"):
            with source.transaction():
                source.root.value = v
        remaining = replica.apply_operations(
            [e.operations for e in events], limit=2, skip_undo=True
        )
        assert replica.root.value == "two"
        assert len(remaining) == 1
        assert not replica.undo_manager.can_undo


class TestMismatchedGenerators:
    def test_remote_insert_with_foreign_ids_is_dropped_silently(self):
        # Peers must share an ID scheme: with a validating generator, incoming
        # node IDs that fail validation abort the (swallowed) apply transaction.
        source = make_doc()  # default ULID doc id, compact child ids
        lenient = NodeIdGenerator(
            generate=counter_generator().generate,
            validate=lambda s: s.startswith("n") or len(s) == 26,
        )
        replica = Doc.restore(source.dump(), nodes=[Item], node_id_generator=lenient)

        events = []
        source.on_change(events.append)
        with source.transaction():
            source.root.children.append(source.create_node(Item, value="x"))

        replica.apply_operations(events[0].operations)  # no exception
        assert values(replica.root) == []

    def test_restore_with_default_generator_rejects_custom_doc_id(self):
        doc = make_doc(node_id_generator=counter_generator())
        with pytest.raises(ValueError, match="Invalid document id"):
            Doc.restore(doc.dump(), nodes=[Item])


class TestValidationOnInit:
    def _classes(self):
        return make_board_classes()

    def test_normalizer_inserting_valid_node_at_init(self):
        Board, Note = self._classes()

        def register(doc_ref):
            def normalize(diff):
                if not doc_ref.root.notes:
                    doc_ref.root.notes.append(doc_ref.create_node(Note, text="ok"))

            doc_ref.on_normalize(normalize)

        doc = Doc(root_type="Board", extensions=[Extension(nodes=[Board, Note], register=register)])
        assert [n.text for n in doc.root.notes] == ["ok"]

    def test_normalizer_inserting_invalid_node_at_init_raises(self):
        Board, Note = self._classes()

        def register(doc_ref):
            def normalize(diff):
                if not doc_ref.root.notes:
                    n = doc_ref.create_node(Note, text="bad")
                    n._state["opacity"] = 5.0  # bypass the per-field adapter
                    doc_ref.root.notes.append(n)

            doc_ref.on_normalize(normalize)

        with pytest.raises(ValidationError):
            Doc(root_type="Board", extensions=[Extension(nodes=[Board, Note], register=register)])

    def test_normalizer_edits_are_validated_in_transactions(self):
        Board, Note = self._classes()

        def register(doc_ref):
            def normalize(diff):
                # Push every inserted note out of range, bypassing the adapter
                for node_id in diff.inserted:
                    n = doc_ref.get_node_by_id(node_id)
                    if n is not None:
                        n._state["opacity"] = 5.0

            doc_ref.on_normalize(normalize)

        doc = Doc(root_type="Board", extensions=[Extension(nodes=[Board, Note], register=register)])
        with pytest.raises(ValidationError):
            with doc.transaction():
                doc.root.notes.append(doc.create_node(Note, text="x"))
        # Rolled back
        assert list(doc.root.notes) == []


# ---------------------------------------------------------------------------
# Node classes defined inside a function
# ---------------------------------------------------------------------------

class TestFunctionLocalNodeClasses:
    """This module uses ``from __future__ import annotations``, so every
    annotation is a string. A node class defined inside a function used to
    fail to resolve ``Array[Note]`` and silently turn the slot into a state
    field holding a plain Python list. Slots must either resolve or fail
    loudly.
    """

    def test_local_basemodel_array_field_is_a_slot(self):
        Board, Note = make_board_classes()
        assert "notes" in Board._slot_defs
        assert "notes" not in Board._field_tiers

    def test_local_basemodel_append_is_recorded_by_doc(self):
        Board, Note = make_board_classes()
        doc = Doc(root_type="Board", nodes=[Board, Note])
        with doc.transaction():
            n = doc.create_node(Note, text="ok")
            doc.root.notes.append(n)
        assert not isinstance(doc.root.notes, list)
        assert doc.get_node_by_id(n.id) is n
        assert doc.parent(n) is doc.root
        assert [x.text for x in doc.root.notes] == ["ok"]

    def test_local_plain_class_with_unresolvable_array_raises(self):
        # A plain (non-Pydantic) class has no frame-based resolution, so a
        # function-local element type cannot be found. Fail loudly, naming
        # the class and the field.
        @node
        class LocalNote:
            text: str = ""

        with pytest.raises(TypeError, match=r"LocalBoard\.notes"):
            @node
            class LocalBoard:
                notes: Array[LocalNote] = []

    def test_local_atomnode_subclass_with_unresolvable_array_raises(self):
        class LocalNote(AtomNode, node_type="local_note_direct"):
            text: str = ""

        with pytest.raises(TypeError, match=r"LocalBoard\.notes.*Array\[LocalNote\]"):
            class LocalBoard(AtomNode, node_type="local_board_direct"):
                notes: Array[LocalNote] = []

    def test_local_class_with_module_level_element_type_resolves(self):
        # ``ModuleNote`` lives in module globals, so a function-local
        # container resolves through the module namespace.
        @node
        class LocalBoard:
            notes: Array[ModuleNote] = []

        assert "notes" in LocalBoard._slot_defs
        doc = Doc(root_type="LocalBoard", nodes=[LocalBoard, ModuleNote])
        with doc.transaction():
            doc.root.notes.append(doc.create_node(ModuleNote, text="x"))
        assert [n.text for n in doc.root.notes] == ["x"]

    def test_self_referential_slot_resolves(self):
        # The class name is not bound anywhere while __init_subclass__ runs;
        # the resolver supplies it.
        class LocalTree(AtomNode, node_type="local_tree_selfref"):
            label: str = ""
            children: Array[LocalTree] = []

        assert "children" in LocalTree._slot_defs
        doc = Doc(root_type="local_tree_selfref", nodes=[LocalTree])
        with doc.transaction():
            doc.root.children.append(doc.create_node(LocalTree, label="kid"))
        assert [c.label for c in doc.root.children] == ["kid"]

    def test_subclass_of_local_node_class_reresolves_inherited_slot(self):
        # Subclassing re-runs resolution with the subclass's name; the base
        # class must still be findable for its own ``Array[LocalTree]``.
        class LocalTree(AtomNode, node_type="local_tree_base"):
            label: str = ""
            children: Array[LocalTree] = []

        class SpecialTree(LocalTree, node_type="local_tree_special"):
            extra: int = 0

        assert "children" in SpecialTree._slot_defs
        assert SpecialTree._slot_defs["children"].allowed_type is LocalTree

    def test_unresolvable_non_array_string_is_left_alone(self):
        # Only Array-looking annotations are required to resolve here; other
        # strings keep the old behavior (left for Pydantic to interpret).
        with pytest.raises(TypeError, match=r"Array\[") as info:
            class LocalBoard(AtomNode, node_type="local_board_mixed"):
                notes: Array[NoSuchName] = []  # noqa: F821

        assert "NoSuchName" in str(info.value)
