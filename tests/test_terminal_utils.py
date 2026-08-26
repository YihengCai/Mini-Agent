"""Tests for terminal_utils module."""

import mini_agent.utils as utils
from mini_agent.utils import calculate_display_width


def test_utils_do_not_export_unused_terminal_helpers():
    assert not hasattr(utils, "truncate_with_ellipsis")
    assert not hasattr(utils, "pad_to_width")


class TestCalculateDisplayWidth:
    """Tests for calculate_display_width function."""

    def test_ascii_text(self):
        """Test ASCII text width calculation."""
        assert calculate_display_width("Hello") == 5
        assert calculate_display_width("World") == 5
        assert calculate_display_width("Test 123") == 8

    def test_empty_string(self):
        """Test empty string."""
        assert calculate_display_width("") == 0

    def test_emoji(self):
        """Test emoji width (should count as 2)."""
        assert calculate_display_width("🤖") == 2
        assert calculate_display_width("💭") == 2
        assert calculate_display_width("🤖 Agent") == 8  # 2 + 1 + 5

    def test_chinese_characters(self):
        """Test Chinese characters (each counts as 2)."""
        assert calculate_display_width("你好") == 4
        assert calculate_display_width("你好世界") == 8
        assert calculate_display_width("中文") == 4

    def test_japanese_characters(self):
        """Test Japanese characters."""
        assert calculate_display_width("日本語") == 6  # 3 chars * 2

    def test_mixed_content(self):
        """Test mixed ASCII and wide characters."""
        assert calculate_display_width("Hello 你好") == 10  # 5 + 1 + 4
        assert calculate_display_width("Test 🤖") == 7  # 4 + 1 + 2

    def test_ansi_codes_ignored(self):
        """Test that ANSI escape codes are not counted."""
        colored = "\033[31mRed\033[0m"
        assert calculate_display_width(colored) == 3

        colored_emoji = "\033[31m🤖\033[0m"
        assert calculate_display_width(colored_emoji) == 2

    def test_combining_characters(self):
        """Test combining characters (should not add width)."""
        # é = e + combining acute accent
        e_with_accent = "e\u0301"
        assert calculate_display_width(e_with_accent) == 1

    def test_complex_ansi_sequences(self):
        """Test complex ANSI sequences."""
        text = "\033[1m\033[36mBold Cyan\033[0m"
        assert calculate_display_width(text) == 9  # "Bold Cyan"
