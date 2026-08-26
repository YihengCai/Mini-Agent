"""Terminal display utilities for proper text alignment.

This module provides utilities for calculating visible width of text in terminals,
handling ANSI escape codes, emoji, and East Asian characters correctly.
"""

import re
import unicodedata

# Compile regex once at module level for performance
ANSI_ESCAPE_RE = re.compile(r"\x1b\[[0-9;]*m")

# Unicode ranges for emoji
EMOJI_START = 0x1F300
EMOJI_END = 0x1FAFF


def calculate_display_width(text: str) -> int:
    """Calculate the visible width of text in terminal columns.

    This function correctly handles:
    - ANSI escape codes (removed from width calculation)
    - Emoji characters (counted as 2 columns)
    - East Asian Wide/Fullwidth characters (counted as 2 columns)
    - Combining characters (counted as 0 columns)
    - Regular ASCII characters (counted as 1 column)

    Args:
        text: Input text that may contain ANSI codes, emoji, or unicode characters

    Returns:
        Number of terminal columns the text will occupy when displayed

    Examples:
        >>> calculate_display_width("Hello")
        5
        >>> calculate_display_width("你好")
        4
        >>> calculate_display_width("🤖")
        2
        >>> calculate_display_width("\033[31mRed\033[0m")
        3
    """
    # Remove ANSI escape codes (they don't occupy display space)
    clean_text = ANSI_ESCAPE_RE.sub("", text)

    width = 0
    for char in clean_text:
        # Skip combining characters (zero width)
        if unicodedata.combining(char):
            continue

        code_point = ord(char)

        # Emoji range (most common emoji, counted as 2 columns)
        if EMOJI_START <= code_point <= EMOJI_END:
            width += 2
            continue

        # East Asian Width property
        # W = Wide, F = Fullwidth (both occupy 2 columns)
        eaw = unicodedata.east_asian_width(char)
        if eaw in ("W", "F"):
            width += 2
        else:
            width += 1

    return width
