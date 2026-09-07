"""Field classification: mergeable / atomic / opaque / ref."""

from __future__ import annotations

import types
from typing import Annotated, Any, Literal, Union, get_args, get_origin

from pydantic import BaseModel

from ._ref import parse_ref_annotation

Tier = Literal["mergeable", "atomic", "opaque", "ref"]


def _is_frozen_model(ann: Any) -> bool:
    """Check if annotation is a frozen Pydantic BaseModel subclass."""
    try:
        return (
            isinstance(ann, type)
            and issubclass(ann, BaseModel)
            and ann.model_config.get("frozen", False) is True
        )
    except Exception:
        return False


def classify_field(annotation: Any) -> Tier:
    """Classify a field annotation into its tier.

    - Ref[T] / list[Ref[T]] → ref
    - bytes → opaque
    - frozen BaseModel, or a union of frozen BaseModels → atomic
    - everything else (scalars, JsonValue, mutable models) → mergeable
    """
    if parse_ref_annotation(annotation) is not None:
        return "ref"
    # Unwrap Annotated[X, ...] (Field constraints, discriminators)
    origin = get_origin(annotation)
    if origin is Annotated:
        return classify_field(get_args(annotation)[0])

    # Unwrap Optional / Union with None
    if origin is Union or origin is types.UnionType:
        args = get_args(annotation)
        # For Optional[X] = Union[X, None], check X
        non_none = [a for a in args if a is not type(None)]
        if len(non_none) == 1:
            return classify_field(non_none[0])
        # A union of frozen models (a tagged union of values) is one
        # value replaced as a unit.
        if non_none and all(_is_frozen_model(a) for a in non_none):
            return "atomic"

    if annotation is bytes:
        return "opaque"
    if _is_frozen_model(annotation):
        return "atomic"
    return "mergeable"


def frozen_models_in(annotation: Any) -> list[type[BaseModel]]:
    """Frozen model classes mentioned by an annotation (unions, Optional,
    Annotated, list/dict item types), in declaration order."""
    found: list[type[BaseModel]] = []

    def walk(ann: Any) -> None:
        if _is_frozen_model(ann):
            if ann not in found:
                found.append(ann)
            return
        origin = get_origin(ann)
        if origin is Annotated:
            walk(get_args(ann)[0])
        elif origin is not None:
            for arg in get_args(ann):
                walk(arg)

    walk(annotation)
    return found
