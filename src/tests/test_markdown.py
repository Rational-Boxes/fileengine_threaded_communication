"""Markdown → plaintext projection (§4a)."""
from discussion.markdown_text import to_plaintext


def test_strips_common_markdown():
    md = "# Heading\n\nSome **bold** and *italic* and `code` here.\n\n- one\n- two\n\n> quote"
    out = to_plaintext(md)
    assert "Heading" in out
    assert "**" not in out and "*" not in out and "`" not in out
    assert "#" not in out and ">" not in out
    assert "bold" in out and "italic" in out and "code" in out
    assert "one" in out and "two" in out


def test_links_keep_text_drop_url():
    out = to_plaintext("see [the doc](https://example.com/x) now")
    assert "the doc" in out
    assert "example.com" not in out


def test_fenced_code_and_images_removed():
    out = to_plaintext("before\n```python\nx = 1\n```\nafter ![alt](img.png)")
    assert "before" in out and "after" in out
    assert "x = 1" not in out
    assert "alt" not in out


def test_empty():
    assert to_plaintext("") == ""
    assert to_plaintext(None) == ""
