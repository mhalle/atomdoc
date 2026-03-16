"""Tests for Pydantic model validation at commit time."""

import pytest
from pydantic import BaseModel, Field, field_validator, model_validator

from atomdoc import Doc, Array, node, UndoManager


class Color(BaseModel, frozen=True):
    r: int = Field(ge=0, le=255, default=0)
    g: int = Field(ge=0, le=255, default=0)
    b: int = Field(ge=0, le=255, default=0)


@node
class Annotation(BaseModel):
    label: str = ""
    color: Color = Color()
    opacity: float = Field(ge=0.0, le=1.0, default=1.0)
    visible: bool = True

    @model_validator(mode="after")
    def check_visible_opacity(self):
        if self.visible and self.opacity == 0:
            raise ValueError("visible nodes must have opacity > 0")
        return self


@node
class StrictNote(BaseModel):
    text: str = ""

    @field_validator("text")
    @classmethod
    def text_not_empty(cls, v: str) -> str:
        if v == "FORBIDDEN":
            raise ValueError("FORBIDDEN is not allowed")
        return v


@node
class Slide(BaseModel):
    title: str = ""
    annotations: Array[Annotation] = []
    notes: Array[StrictNote] = []


def make_doc():
    return Doc(root_type="Slide", nodes=[Slide, Annotation, StrictNote])


class TestFieldConstraints:
    def test_frozen_model_field_constraint(self):
        """Color(r=300) should fail at construction time."""
        with pytest.raises(Exception):
            Color(r=300)

    def test_opacity_range_per_field(self):
        """TypeAdapter catches per-field constraint on write."""
        doc = make_doc()
        with doc.transaction():
            ann = doc.create_node(Annotation)
            doc.root.annotations.append(ann)
        with pytest.raises(Exception):
            with doc.transaction():
                ann.opacity = 2.0

    def test_valid_opacity(self):
        doc = make_doc()
        with doc.transaction():
            ann = doc.create_node(Annotation, opacity=0.5)
            doc.root.annotations.append(ann)
        assert ann.opacity == 0.5


class TestModelValidator:
    def test_cross_field_invalid(self):
        """visible=True + opacity=0 should fail at commit."""
        doc = make_doc()
        with doc.transaction():
            ann = doc.create_node(Annotation)
            doc.root.annotations.append(ann)

        with pytest.raises(Exception, match="visible nodes must have opacity > 0"):
            with doc.transaction():
                ann.visible = True
                ann.opacity = 0.0

    def test_cross_field_valid(self):
        """visible=False + opacity=0 is allowed."""
        doc = make_doc()
        with doc.transaction():
            ann = doc.create_node(Annotation)
            doc.root.annotations.append(ann)
        with doc.transaction():
            ann.visible = False
            ann.opacity = 0.0
        assert ann.visible is False
        assert ann.opacity == 0.0

    def test_rollback_on_validation_failure(self):
        """State should be rolled back when model validation fails."""
        doc = make_doc()
        with doc.transaction():
            ann = doc.create_node(Annotation, opacity=0.5)
            doc.root.annotations.append(ann)

        with pytest.raises(Exception):
            with doc.transaction():
                ann.visible = True
                ann.opacity = 0.0

        assert ann.visible is True
        assert ann.opacity == 0.5  # rolled back to original

    def test_no_change_event_on_validation_failure(self):
        """Change event should not fire if validation fails."""
        doc = make_doc()
        with doc.transaction():
            ann = doc.create_node(Annotation)
            doc.root.annotations.append(ann)

        events = []
        doc.on_change(lambda ev: events.append(ev))

        with pytest.raises(Exception):
            with doc.transaction():
                ann.visible = True
                ann.opacity = 0.0

        assert len(events) == 0

    def test_intermediate_invalid_state_ok(self):
        """Invalid intermediate states within a tx are fine if final state is valid."""
        doc = make_doc()
        with doc.transaction():
            ann = doc.create_node(Annotation)
            doc.root.annotations.append(ann)

        with doc.transaction():
            ann.visible = True
            ann.opacity = 0.0  # invalid intermediate!
            ann.visible = False  # fixed before commit
        assert ann.visible is False
        assert ann.opacity == 0.0


class TestFieldValidator:
    def test_field_validator_at_commit(self):
        """@field_validator runs at commit time."""
        doc = make_doc()
        with doc.transaction():
            note = doc.create_node(StrictNote)
            doc.root.notes.append(note)

        with pytest.raises(Exception, match="FORBIDDEN"):
            with doc.transaction():
                note.text = "FORBIDDEN"

    def test_valid_field_value(self):
        doc = make_doc()
        with doc.transaction():
            note = doc.create_node(StrictNote, text="hello")
            doc.root.notes.append(note)
        assert note.text == "hello"


class TestValidationWithUndo:
    def test_undo_after_valid_commit(self):
        doc = make_doc()
        undo = UndoManager(doc)
        with doc.transaction():
            ann = doc.create_node(Annotation, opacity=0.5)
            doc.root.annotations.append(ann)
        with doc.transaction():
            ann.opacity = 0.8
        assert ann.opacity == 0.8
        undo.undo()
        assert ann.opacity == 0.5

    def test_failed_validation_doesnt_pollute_undo(self):
        """A failed transaction should not add to the undo stack."""
        doc = make_doc()
        undo = UndoManager(doc)
        with doc.transaction():
            ann = doc.create_node(Annotation)
            doc.root.annotations.append(ann)

        assert undo.can_undo

        with pytest.raises(Exception):
            with doc.transaction():
                ann.visible = True
                ann.opacity = 0.0

        # Undo stack should still have only the insert, not the failed tx
        undo.undo()
        assert len(doc.root.annotations) == 0


class TestPlainClassNoValidation:
    def test_plain_class_no_validator_model(self):
        """@node on a plain class should not have a validator model."""
        from atomdoc import DocNode

        class PlainNode(DocNode, node_type="plain_test"):
            x: int = 0

        assert PlainNode._validator_model is None

    def test_plain_class_decorator_no_validator(self):
        @node
        class PlainNode:
            x: int = 0

        assert PlainNode._validator_model is None
