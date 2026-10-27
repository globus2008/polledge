"""Minimal message catalog: source code uses English texts, translations live in locales/<lang>.yaml.

Usage: ``_("Election '{id}' is not configured", id=eid)``. A missing translation falls back
to the English text, so the program never breaks because of the catalog.
"""

from __future__ import annotations

import logging
from importlib import resources

import yaml

log = logging.getLogger(__name__)

SUPPORTED = ("en", "cs")

_catalog: dict[str, str] = {}
_language = "en"


def set_language(language: str) -> None:
    global _catalog, _language
    if language not in SUPPORTED:
        raise ValueError(f"Unsupported language '{language}' (supported: {', '.join(SUPPORTED)})")
    _language = language
    if language == "en":
        _catalog = {}
        return
    text = resources.files("polledge").joinpath("locales", f"{language}.yaml").read_text(encoding="utf-8")
    _catalog = yaml.safe_load(text) or {}


def language() -> str:
    return _language


def _(text: str, /, **values) -> str:
    template = _catalog.get(text, text)
    if not values:
        return template
    try:
        return template.format(**values)
    except (KeyError, IndexError, ValueError):
        log.debug("Bad translation for %r, using English", text)
        return text.format(**values)


def decimal(text: str) -> str:
    """Localised decimal separator for an already formatted number."""
    return text.replace(".", ",") if _language == "cs" else text
