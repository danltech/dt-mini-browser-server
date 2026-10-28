# DT Mini Browser Server – UI strings for error pages and messages
# Copyright (C) 2026 DanlTech
# Licensed under the GNU Affero General Public License v3.0
#
# Fallback language is set via MINI_FALLBACK_LANG (default: en).
# The client can send ?lang=de or ?lang=en; otherwise the fallback is used.
# Add new languages by extending TRANSLATIONS and opening a PR.

import os

FALLBACK_LANG = (os.environ.get("MINI_FALLBACK_LANG") or "en").strip().lower()[:2] or "en"

TRANSLATIONS = {
    "en": {
        "error_prefix": "Error: ",
        "error_title": "Error",
        "page_read_failed": "Page could not be read",
        "address_not_allowed": "Address not allowed (internal address)",
    },
    "de": {
        "error_prefix": "Fehler: ",
        "error_title": "Fehler",
        "page_read_failed": "Seite konnte nicht ausgelesen werden",
        "address_not_allowed": "Adresse nicht erlaubt (interne Adresse)",
    },
}


def normalize_lang(lang):
    """Return a supported language code, or FALLBACK_LANG."""
    if not lang:
        return FALLBACK_LANG
    code = str(lang).strip().lower().replace("_", "-")
    if "-" in code:
        code = code.split("-", 1)[0]
    code = code[:2]
    if code in TRANSLATIONS:
        return code
    return FALLBACK_LANG


def t(key, lang=None):
    """Translate key for the given language (or fallback)."""
    code = normalize_lang(lang)
    table = TRANSLATIONS.get(code) or TRANSLATIONS.get(FALLBACK_LANG) or TRANSLATIONS["en"]
    if key in table:
        return table[key]
    # Ultimate fallback to English, then key itself
    en = TRANSLATIONS.get("en", {})
    return en.get(key, key)
