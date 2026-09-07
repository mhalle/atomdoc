"""Tests for composition: dump(node) and Doc.adopt()."""

import pytest

from atomdoc import Array, Doc, Ref, RefIntegrityError, node


@node
class Transform:
    name: str = ""
    parent: Ref["Transform"] | None = None


@node
class Volume:
    name: str = ""
    transform: Ref[Transform] | None = None


@node
class Scene:
    transforms: Array[Transform] = []
    volumes: Array[Volume] = []


@node
class Library:
    scenes: Array[Scene] = []
    volumes: Array[Volume] = []


def make_scene(prefix):
    doc = Doc(Scene)
    with doc.transaction():
        t = doc.create_node(Transform, name=f"{prefix}-t")
        doc.root.transforms.append(t)
        v = doc.create_node(Volume, name=f"{prefix}-v", transform=t)
        doc.root.volumes.append(v)
    return doc, t, v


def test_dump_node_is_a_fragment():
    doc, t, v = make_scene("a")
    fragment = doc.dump(v)
    assert fragment[0] == v.id
    assert fragment[1] == "Volume"
    assert fragment[2] == {"name": "a-v", "transform": t.id}


def test_adopt_keeps_ids_and_internal_references():
    src, t, v = make_scene("a")
    lib = Doc(Library)
    with lib.transaction():
        [scene] = lib.adopt(src.dump(), lib.root, "scenes")
    assert scene.id == src.id
    at = lib.get_node_by_id(t.id)
    av = lib.get_node_by_id(v.id)
    assert av.transform is at
    assert lib.referrers(at) == [av]
    assert at.name == "a-t"


def test_adopt_emits_ordinary_ops():
    src, t, v = make_scene("a")
    lib = Doc(Library)
    events = []
    lib.on_change(events.append)
    with lib.transaction():
        lib.adopt(src.dump(), lib.root, "scenes")
    ops = events[-1].operations
    inserted = {nid for op in ops[0] if op[0] == 0 for nid, _ in op[1]}
    assert {src.id, t.id, v.id} <= inserted
    assert ops[1][v.id] == {"name": "a-v", "transform": t.id}
    # A peer applying the same ops reproduces the composition.
    peer = Doc(Library)
    peer.apply_operations(ops)
    assert peer.get_node_by_id(v.id).transform is peer.get_node_by_id(t.id)


def test_adopt_remints_only_colliding_ids():
    src, t, v = make_scene("a")
    lib = Doc(Library)
    with lib.transaction():
        lib.adopt(src.dump(), lib.root, "scenes")
    # Adopt the same fragment again: every ID collides, all are re-minted,
    # and the internal reference follows the remap.
    with lib.transaction():
        [again] = lib.adopt(src.dump(), lib.root, "scenes")
    assert again.id != src.id
    vols = again.volumes
    assert vols[0].id != v.id
    assert vols[0].transform is again.transforms[0]
    assert again.transforms[0].id != t.id
    assert lib.get_node_by_id(v.id).transform is lib.get_node_by_id(t.id)


def test_adopt_partial_collision():
    src, t, v = make_scene("a")
    lib = Doc(Library)
    with lib.transaction():
        lib.adopt(src.dump(t), lib.root.scenes.append(lib.create_node(Scene)) or lib.root.scenes[0], "transforms")
    # Now the volume alone: its transform ref points at t, which exists here.
    with lib.transaction():
        [av] = lib.adopt(src.dump(v), lib.root.scenes[0], "volumes")
    assert av.id == v.id
    assert av.transform is lib.get_node_by_id(t.id)


def test_adopt_dangling_external_reference_rejected():
    src, t, v = make_scene("a")
    lib = Doc(Library)
    with pytest.raises(RefIntegrityError):
        with lib.transaction():
            lib.adopt(src.dump(v), lib.root, "volumes")  # t is not here
    assert lib.get_node_by_id(v.id) is None


def test_adopt_list_of_fragments_and_positions():
    src, t, v = make_scene("a")
    lib = Doc(Library)
    with lib.transaction():
        scene = lib.create_node(Scene)
        lib.root.scenes.append(scene)
        first = lib.create_node(Volume, name="first")
        scene.volumes.append(first)
        lib.adopt([src.dump(t)], scene, "transforms")
        lib.adopt([src.dump(v)], scene, "volumes", "before", target=first)
    assert [x.name for x in scene.volumes] == ["a-v", "first"]


def test_adopt_round_trips_through_dump_and_undo():
    from atomdoc import UndoManagerConfig

    src, t, v = make_scene("a")
    lib = Doc(Library, undo_manager=UndoManagerConfig(max_steps=5))
    with lib.transaction():
        lib.adopt(src.dump(), lib.root, "scenes")
    restored = Doc.restore(lib.dump(), Library)
    assert restored.get_node_by_id(v.id).transform.id == t.id
    lib.undo_manager.undo()
    assert lib.get_node_by_id(v.id) is None
    lib.undo_manager.redo()
    assert lib.get_node_by_id(v.id).transform.id == t.id
