"""Bound raw tool text when projecting it into model-visible messages."""

from __future__ import annotations


# This is a UTF-8 byte budget for one model-facing tool message, not a vendor
# token estimate or a limit on raw events, logs, or the complete tool batch.
MAX_TOOL_MESSAGE_BYTES = 64 * 1024

_MARKER_TEMPLATE = (
    "\n\n[Tool output truncated: "
    "original_bytes={original_bytes}; "
    "retained_bytes={retained_bytes}; "
    "omitted_bytes={omitted_bytes}; "
    "limit_bytes={limit_bytes}]\n\n"
)


def _marker(
    *,
    original_bytes: int,
    retained_bytes: int,
    omitted_bytes: int,
) -> str:
    return _MARKER_TEMPLATE.format(
        original_bytes=original_bytes,
        retained_bytes=retained_bytes,
        omitted_bytes=omitted_bytes,
        limit_bytes=MAX_TOOL_MESSAGE_BYTES,
    )


def _largest_value_with_same_digits(value: int) -> int:
    return (10 ** len(str(value))) - 1


def truncate_tool_message(content: str) -> str:
    """Keep UTF-8-safe head and tail text within one model message budget."""

    encoded = content.encode("utf-8")
    original_bytes = len(encoded)
    if original_bytes <= MAX_TOOL_MESSAGE_BYTES:
        return content

    # Reserve the longest marker possible for these digit widths. The actual
    # metadata can only be the same size or shorter, so the final text cannot
    # exceed the hard byte limit even when a cut crosses a multi-byte character.
    marker_reserve = _marker(
        original_bytes=original_bytes,
        retained_bytes=_largest_value_with_same_digits(MAX_TOOL_MESSAGE_BYTES),
        omitted_bytes=_largest_value_with_same_digits(original_bytes),
    )
    retained_budget = MAX_TOOL_MESSAGE_BYTES - len(marker_reserve.encode("utf-8"))
    if retained_budget <= 0:  # pragma: no cover - guarded by the fixed policy above
        raise RuntimeError("Tool message budget is too small for truncation metadata")

    head_budget = (retained_budget + 1) // 2
    tail_budget = retained_budget // 2
    head = encoded[:head_budget].decode("utf-8", errors="ignore")
    tail = encoded[-tail_budget:].decode("utf-8", errors="ignore")
    retained_bytes = len(head.encode("utf-8")) + len(tail.encode("utf-8"))
    marker = _marker(
        original_bytes=original_bytes,
        retained_bytes=retained_bytes,
        omitted_bytes=original_bytes - retained_bytes,
    )
    projected = f"{head}{marker}{tail}"
    assert len(projected.encode("utf-8")) <= MAX_TOOL_MESSAGE_BYTES
    return projected


__all__ = ["MAX_TOOL_MESSAGE_BYTES", "truncate_tool_message"]
