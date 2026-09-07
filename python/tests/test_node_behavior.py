"""@node keeps the source class's methods and properties."""

from typing import ClassVar

import pytest
from pydantic import BaseModel, model_validator

from atomdoc import Array, Doc, node


@node
class Feature(BaseModel):
    label: str = ""
    iris: list[str] = []
    SCHEME: ClassVar[str] = "http://example.org/"  # Pydantic needs ClassVar

    @property
    def concept_iris(self) -> list[str]:
        return [self.SCHEME + i for i in self.iris]

    @property
    def title(self) -> str:
        return self.label.title()

    @title.setter
    def title(self, value: str) -> None:
        self.label = value.lower()

    def describe(self) -> str:
        return f"{self.label} ({len(self.iris)})"

    @classmethod
    def blank(cls, doc: "Doc") -> "Feature":
        return doc.create_node(cls, label="blank")

    @staticmethod
    def normalize(s: str) -> str:
        return s.strip()

    @model_validator(mode="after")
    def not_empty_iri(self):
        if any(not i for i in self.iris):
            raise ValueError("empty iri")
        return self


@node
class Special(Feature):
    def describe(self) -> str:  # overriding the base's own method is fine
        return "special " + super().describe()


@node
class Plain:
    n: int = 0

    def doubled(self) -> int:
        return self.n * 2


@node
class Root:
    features: Array[Feature] = []
    plains: Array[Plain] = []


def test_methods_properties_and_class_members_survive():
    doc = Doc(Root)
    f = doc.create_node(Feature, label="a", iris=["x"])
    doc.root.features.append(f)
    assert f.concept_iris == ["http://example.org/x"]
    assert f.describe() == "a (1)"
    assert f.title == "A"
    f.title = "New"
    assert f.label == "new"  # the setter wrote through the field
    assert Feature.blank(doc).label == "blank"
    assert Feature.normalize("  s ") == "s"
    assert Feature.SCHEME == "http://example.org/"
    s = doc.create_node(Special, label="s")
    doc.root.features.append(s)
    assert s.describe() == "special s (0)"
    p = doc.create_node(Plain, n=2)
    doc.root.plains.append(p)
    assert p.doubled() == 4
    with pytest.raises(Exception):
        f.iris = [""]  # the validator still runs


def test_shadowing_the_node_api_is_an_error():
    with pytest.raises(TypeError, match="would shadow AtomNode.delete"):
        @node
        class Bad:
            x: int = 0

            def delete(self) -> None:  # pragma: no cover
                pass
