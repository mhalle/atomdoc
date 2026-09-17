"""A frozen value is validated when it enters a document, however it was built.

Pydantic trusts an instance of the right class: `model_copy(update=...)` and
`model_construct(...)` both make one without validating. Before this, a Color
with r='x' built that way was stored and committed, on validating and plain
nodes alike. The red channel still cannot be set on its own — Color is frozen
— but an invalid whole Color could get in.
"""

from __future__ import annotations

from typing import Literal

import pytest
from pydantic import BaseModel, Field, ValidationError

from atomdoc import Array, Doc, node


class Color(BaseModel, frozen=True):
    r: int = Field(0, ge=0, le=255)
    g: int = 0
    b: int = 0


class Solid(BaseModel, frozen=True):
    kind: Literal["solid"] = "solid"
    color: Color = Color()


class Gradient(BaseModel, frozen=True):
    kind: Literal["gradient"] = "gradient"
    stops: tuple[Color, ...] = ()


@node
class Swatch(BaseModel):                 # a validating node
    color: Color = Color()
    history: list[Color] = []
    named: dict[str, Color] = {}
    fill: Solid | Gradient = Field(default=Solid(), discriminator="kind")


@node
class PlainSwatch:                       # a plain node: no model validation, field types still checked
    color: Color = Color()


@node
class Palette(BaseModel):
    swatches: Array[Swatch] = []
    plain: Array[PlainSwatch] = []


@pytest.fixture
def doc():
    d = Doc(Palette())
    with d.transaction():
        d.root.swatches.append(d.create_node(Swatch))
        d.root.plain.append(d.create_node(PlainSwatch))
    return d


def bad_copy(value, **update):
    return value.model_copy(update=update)                     # no validation


INVALID = {
    "model_copy, wrong type": lambda: bad_copy(Color(), r="x"),
    "model_copy, out of range": lambda: bad_copy(Color(), r=999),
    "model_construct": lambda: Color.model_construct(r="x", g=0, b=0),
}


@pytest.mark.parametrize("make", INVALID.values(), ids=INVALID.keys())
@pytest.mark.parametrize("which", ["swatches", "plain"])
def test_an_unvalidated_instance_is_refused_on_assignment(doc, make, which):
    target = getattr(doc.root, which)[0]
    with pytest.raises(ValidationError):
        with doc.transaction():
            target.color = make()
    assert target.color == Color()                             # nothing got in


@pytest.mark.parametrize("make", INVALID.values(), ids=INVALID.keys())
def test_inside_a_list_a_dict_and_a_union(doc, make):
    swatch = doc.root.swatches[0]
    for value in ([Color(), make()], {"a": make()}):
        field = "history" if isinstance(value, list) else "named"
        with pytest.raises(ValidationError):
            with doc.transaction():
                setattr(swatch, field, value)
    with pytest.raises(ValidationError):
        with doc.transaction():
            swatch.fill = Gradient(stops=()).model_copy(update={"stops": (make(),)})
    assert (swatch.history, swatch.named, swatch.fill) == ([], {}, Solid())


@pytest.mark.parametrize("make", INVALID.values(), ids=INVALID.keys())
def test_on_create_node_and_in_a_snapshot(doc, make):
    with pytest.raises(ValidationError):
        with doc.transaction():
            doc.root.swatches.append(doc.create_node(Swatch, color=make()))
    with pytest.raises(ValidationError):
        Swatch(color=make())
    assert len(doc.root.swatches) == 1


def test_a_valid_copy_is_still_accepted(doc):
    swatch = doc.root.swatches[0]
    with doc.transaction():
        swatch.color = swatch.color.model_copy(update={"r": 200})
        swatch.history = [Color(r=1), Color(r=2).model_copy(update={"g": 3})]
        swatch.fill = Gradient(stops=(Color(r=5),))
    assert swatch.color == Color(r=200)
    assert swatch.history == [Color(r=1), Color(r=2, g=3)]
    assert swatch.fill == Gradient(stops=(Color(r=5),))
    restored = Doc.restore(doc.dump(), root_type=Palette)
    assert restored.root.swatches[0].fill == Gradient(stops=(Color(r=5),))


def test_the_red_channel_still_cannot_be_set_alone(doc):
    swatch = doc.root.swatches[0]
    with pytest.raises(ValidationError):
        swatch.color.r = 255                                   # frozen


class Weak(BaseModel, frozen=True):
    uri: str = ""


class Strong(BaseModel, frozen=True):
    uri: str = ""


@node
class Holder:
    either: Weak | Strong | None = None
    many: list[Weak | Strong] = []


@node
class Shelf:
    holders: Array[Holder] = []


def test_revalidation_keeps_the_class_between_same_shaped_types():
    """Validating plain data would pick the first union member that fits and
    turn a Strong into a Weak; each instance is validated by its own class."""
    d = Doc(Shelf())
    with d.transaction():
        d.root.holders.append(d.create_node(Holder))
    holder = d.root.holders[0]
    with d.transaction():
        holder.either = Strong(uri="a")
        holder.many = [Weak(uri="b"), Strong(uri="c").model_copy(update={"uri": "d"})]
    assert type(holder.either) is Strong
    assert [type(v) for v in holder.many] == [Weak, Strong]
    with pytest.raises(ValidationError):
        with d.transaction():
            holder.either = Strong.model_construct(uri=5)
    assert holder.either == Strong(uri="a")
