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
    "es": {
        "error_prefix": "Error: ",
        "error_title": "Error",
        "page_read_failed": "No se pudo leer la página",
        "address_not_allowed": "Dirección no permitida (dirección interna)",
    },
    "fr": {
        "error_prefix": "Erreur : ",
        "error_title": "Erreur",
        "page_read_failed": "Impossible de lire la page",
        "address_not_allowed": "Adresse non autorisée (adresse interne)",
    },
    "it": {
        "error_prefix": "Errore: ",
        "error_title": "Errore",
        "page_read_failed": "Impossibile leggere la pagina",
        "address_not_allowed": "Indirizzo non consentito (indirizzo interno)",
    },
    "tr": {
        "error_prefix": "Hata: ",
        "error_title": "Hata",
        "page_read_failed": "Sayfa okunamadı",
        "address_not_allowed": "Adrese izin verilmiyor (dahili adres)",
    },
    "ru": {
        "error_prefix": "Ошибка: ",
        "error_title": "Ошибка",
        "page_read_failed": "Не удалось прочитать страницу",
        "address_not_allowed": "Адрес не разрешён (внутренний адрес)",
    },
    "ja": {
        "error_prefix": "エラー：",
        "error_title": "エラー",
        "page_read_failed": "ページを読み取れませんでした",
        "address_not_allowed": "このアドレスは許可されていません（内部アドレス）",
    },
    "zh": {
        "error_prefix": "错误：",
        "error_title": "错误",
        "page_read_failed": "无法读取页面",
        "address_not_allowed": "不允许的地址（内部地址）",
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
