"""Translations for user-facing text (gettext, language from LANGUAGE / LC_ALL / LC_MESSAGES / LANG)."""
from __future__ import annotations

import gettext
from pathlib import Path

LOCALE_DIR = Path(__file__).parent / "locale"
_translation = gettext.translation("tiptoi", localedir=LOCALE_DIR, fallback=True)
_ = _translation.gettext
ngettext = _translation.ngettext
