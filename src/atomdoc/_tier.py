"""Field classification: mergeable / atomic / opaque / ref."""

from __future__ import annotations

from typing import Any, Literal, get_args, get_origin

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
    - frozen BaseModel → atomic
    - everything else → mergeable
    """
    if parse_ref_annotation(annotation) is not None:
        return "ref"
    # Unwrap Optional / Union with None
    origin = get_origin(annotation)
    if origin is not None:
        args = get_args(annotation)
        # For Optional[X] = Union[X, None], check X
        non_none = [a for a in args if a is not type(None)]
        if len(non_none) == 1:
            return classify_field(non_none[0])

    if annotation is bytes:
        return "opaque"
    if _is_frozen_model(annotation):
        return "atomic"
    return "mergeable"
