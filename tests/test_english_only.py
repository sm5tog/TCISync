# -*- coding: utf-8 -*-
"""Spärrtest: användarsynlig text i den paketerade appen ska vara engelska.
Räknar fram de paketerade modulerna ur lokala importer (start: tcisync.py och
tcisync_updater.py) och kontrollerar strängliteraler (docstrings undantagna):
(a) inga å, ä, ö, (b) inga svenska ordlisteord. Baslinje: inga undantag.
Körs med: python -m unittest discover -s tests"""

import ast
import os
import re
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

ENTRY_POINTS = ("tcisync", "tcisync_updater")
SWEDISH_CHARS = re.compile("[åäöÅÄÖ]")
SWEDISH_WORDS = ("och", "inte", "eller", "fel", "spara", "sparar", "ingen",
                 "inget", "troligen", "radion", "igång", "saknas",
                 "misslyckades", "kunde")
WORD_RE = re.compile(r"(?<!\w)(?:%s)(?!\w)" % "|".join(SWEDISH_WORDS), re.IGNORECASE)


def _parse(root, module):
    with open(os.path.join(root, module + ".py"), encoding="utf-8-sig") as f:
        return ast.parse(f.read())


def _local_imports(tree, local):
    found = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                found.add(alias.name.split(".")[0])
        elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
            found.add(node.module.split(".")[0])
    return found & local


def packaged_modules(root=ROOT):
    local = {f[:-3] for f in os.listdir(root) if f.endswith(".py")}
    seen = set()
    stack = list(ENTRY_POINTS)
    while stack:
        module = stack.pop()
        if module in seen or module not in local:
            continue
        seen.add(module)
        stack.extend(_local_imports(_parse(root, module), local))
    return sorted(seen)


def _docstring_ids(tree):
    ids = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
            body = node.body
            if (body and isinstance(body[0], ast.Expr)
                    and isinstance(body[0].value, ast.Constant)
                    and isinstance(body[0].value.value, str)):
                ids.add(id(body[0].value))
    return ids


def string_literals(root, module):
    """(rad, text) för alla strängliteraler utom docstrings."""
    tree = _parse(root, module)
    docs = _docstring_ids(tree)
    return [(n.lineno, n.value) for n in ast.walk(tree)
            if isinstance(n, ast.Constant) and isinstance(n.value, str)
            and id(n) not in docs]


def find_violations(root=ROOT):
    chars, words = [], []
    for module in packaged_modules(root):
        for line, text in string_literals(root, module):
            if SWEDISH_CHARS.search(text):
                chars.append("%s.py:%d: %r" % (module, line, text[:100]))
            if WORD_RE.search(text):
                words.append("%s.py:%d: %r" % (module, line, text[:100]))
    return sorted(chars), sorted(words)


class EnglishOnlyTest(unittest.TestCase):
    def test_modules_found(self):
        modules = packaged_modules()
        self.assertIn("tcisync", modules)
        self.assertIn("tcisync_updater", modules)
        self.assertIn("tci_link", modules)

    def test_no_swedish_letters_in_strings(self):
        chars, _ = find_violations()
        self.assertEqual(chars, [], "Swedish letters in user-visible strings:\n" + "\n".join(chars))

    def test_no_swedish_words_in_strings(self):
        _, words = find_violations()
        self.assertEqual(words, [], "Swedish words in user-visible strings:\n" + "\n".join(words))


if __name__ == "__main__":
    unittest.main()
