import ast
import string
from pathlib import Path

import pytest

from polledge import i18n
from polledge.i18n import _
from polledge.odds.scrapers import UNSUPPORTED

SRC = Path(__file__).resolve().parents[1] / "src" / "polledge"


def _source_messages() -> set[str]:
    messages = set(UNSUPPORTED.values())
    for path in SRC.rglob("*.py"):
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if (isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == "_"
                    and node.args and isinstance(node.args[0], ast.Constant) and isinstance(node.args[0].value, str)):
                messages.add(node.args[0].value)
    return messages


def _fields(text: str) -> set[str]:
    return {name for _lit, name, _spec, _conv in string.Formatter().parse(text) if name}


def test_czech_catalog_is_complete_and_consistent():
    i18n.set_language("cs")
    catalog = i18n._catalog
    messages = _source_messages()
    missing = sorted(m for m in messages if m not in catalog)
    assert not missing, f"missing Czech translations: {missing}"
    for message in messages:
        assert _fields(catalog[message]) == _fields(message), f"placeholders differ for {message!r}"
    unused = sorted(set(catalog) - messages)
    assert not unused, f"translations not used in the code: {unused}"


def test_translation_and_fallback():
    i18n.set_language("cs")
    assert _("Election '{id}' is not configured. Available: {available}", id="x", available="y") == \
        "Volby 'x' nejsou nakonfigurované. Dostupné: y"
    assert _("A text without translation {n}", n=1) == "A text without translation 1"
    assert i18n.decimal("1.5") == "1,5"
    i18n.set_language("en")
    assert _("Others") == "Others"
    assert i18n.decimal("1.5") == "1.5"


def test_unknown_language():
    with pytest.raises(ValueError):
        i18n.set_language("de")
