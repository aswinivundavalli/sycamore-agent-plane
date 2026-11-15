"""Core text helpers."""
import re


def slugify(text):
    """Lower-case, replace runs of non-alphanumerics with '-'."""
    text = text.lower()
    text = re.sub(r"[^a-z0-9]+", "-", text)
    return text.strip("-")


def word_count(text):
    """Count whitespace-separated words."""
    return len(text.split())


def truncate(text, width):
    """Shorten text to at most `width` characters."""
    if len(text) <= width:
        return text
    return text[:width]
