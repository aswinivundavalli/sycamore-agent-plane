#!/usr/bin/env bash
# Create a small Python project with a unittest suite to run agents against.
# Usage: scripts/make-demo-repo.sh [dir]   (default: ./demo-repo)
set -euo pipefail
dir="${1:-demo-repo}"
if [ -e "$dir" ]; then echo "$dir already exists" >&2; exit 1; fi
mkdir -p "$dir/textkit" "$dir/tests"
cd "$dir"
git init -q -b main .
cat > README.md <<'MD'
# textkit

Tiny text utilities. Run the tests with `python3 -m unittest discover -s tests -t .`
MD
cat > textkit/__init__.py <<'PY'
from .core import slugify, word_count, truncate

__all__ = ["slugify", "word_count", "truncate"]
PY
cat > textkit/core.py <<'PY'
"""Core text helpers."""
import re


def slugify(text):
    """Lower-case, replace runs of non-alphanumerics with '-'."""
    text = text.lower()
    text = re.sub(r"[^a-z0-9]+", "-", text)
    return text.strip("-")


def word_count(text):
    """Count whitespace-separated words."""
    return len(text.split(" "))  # BUG: multiple spaces / newlines count wrong


def truncate(text, width):
    """Shorten text to at most `width` characters."""
    if len(text) <= width:
        return text
    return text[:width]
PY
cat > tests/__init__.py <<'PY'
PY
cat > tests/test_core.py <<'PY'
import unittest

from textkit import slugify, truncate, word_count


class CoreTest(unittest.TestCase):
    def test_slugify(self):
        self.assertEqual(slugify("Hello, World!"), "hello-world")

    def test_word_count_simple(self):
        self.assertEqual(word_count("one two three"), 3)

    def test_truncate_short(self):
        self.assertEqual(truncate("abc", 5), "abc")


if __name__ == "__main__":
    unittest.main()
PY
git add -A
git -c user.name="Demo" -c user.email="demo@example.com" commit -qm "Initial textkit"
echo "Demo repo ready at $(pwd)"
