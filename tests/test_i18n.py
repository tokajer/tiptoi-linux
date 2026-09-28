from __future__ import annotations

import ast
import gettext
import shutil
import string
import subprocess
import tempfile
import unittest
from pathlib import Path

from tiptoi_linux.i18n import LOCALE_DIR

PACKAGE_DIR = Path(__file__).resolve().parent.parent / "tiptoi_linux"
PO_PATH = LOCALE_DIR / "de" / "LC_MESSAGES" / "tiptoi.po"
MO_PATH = LOCALE_DIR / "de" / "LC_MESSAGES" / "tiptoi.mo"

# Strings that are legitimately identical in German.
SAME_IN_GERMAN = frozenset({"OK", "tiptoi", "Version"})


def _load_german() -> gettext.GNUTranslations:
    return gettext.translation("tiptoi", localedir=LOCALE_DIR, languages=["de"])


def _catalog(mo_path: Path) -> dict:
    with open(mo_path, "rb") as handle:
        return gettext.GNUTranslations(handle)._catalog


def _translation_calls() -> list[tuple[Path, ast.Call]]:
    calls = []
    for path in sorted(PACKAGE_DIR.glob("*.py")):
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id in ("_", "ngettext"):
                calls.append((path, node))
    return calls


def _message_args(call: ast.Call) -> list[ast.expr]:
    return call.args[:2] if call.func.id == "ngettext" else call.args[:1]  # type: ignore[attr-defined]


def _fields(text: str) -> set[str]:
    return {field for _, field, _, _ in string.Formatter().parse(text) if field}


class TranslationSourceTests(unittest.TestCase):
    def test_every_message_is_a_plain_string_literal(self) -> None:
        # A non-literal (f-string, variable) can't be extracted by xgettext and never gets translated.
        offenders = [
            f"{path.name}:{call.lineno}"
            for path, call in _translation_calls()
            for arg in _message_args(call)
            if not (isinstance(arg, ast.Constant) and isinstance(arg.value, str))
        ]
        self.assertEqual(offenders, [])


class GermanCatalogTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.catalog = _load_german()._catalog

    def _messages(self) -> list[tuple[str, str, str]]:
        """(location, english, german) for every singular and plural message in the sources."""
        found = []
        for path, call in _translation_calls():
            args = _message_args(call)
            if not all(isinstance(arg, ast.Constant) for arg in args):
                continue
            location = f"{path.name}:{call.lineno}"
            if call.func.id == "_":  # type: ignore[attr-defined]
                english = args[0].value  # type: ignore[attr-defined]
                found.append((location, english, self.catalog.get(english, "")))
            else:
                singular, plural = (arg.value for arg in args)  # type: ignore[attr-defined]
                found.append((location, singular, self.catalog.get((singular, 0), "")))
                found.append((location, plural, self.catalog.get((singular, 1), "")))
        return found

    def test_every_message_has_a_german_translation(self) -> None:
        untranslated = [
            f"{location} {english!r}"
            for location, english, german in self._messages()
            if not german or (german == english and english not in SAME_IN_GERMAN)
        ]
        self.assertEqual(untranslated, [])

    def test_placeholders_match(self) -> None:
        # A renamed or dropped {placeholder} raises KeyError in .format() only when German is active.
        mismatched = [
            f"{location} {english!r} -> {german!r}"
            for location, english, german in self._messages()
            if german and _fields(english) != _fields(german)
        ]
        self.assertEqual(mismatched, [])

    def test_gui_mnemonics_are_unique_in_german(self) -> None:
        letters: dict[str, str] = {}
        clashes = []
        for path, call in _translation_calls():
            if path.name != "gui.py" or call.func.id != "_":  # type: ignore[attr-defined]
                continue
            arg = call.args[0]
            if not (isinstance(arg, ast.Constant) and "&" in arg.value.replace("&&", "")):
                continue
            german = self.catalog.get(arg.value, "").replace("&&", "")
            index = german.find("&")
            if index == -1 or index + 1 >= len(german):
                clashes.append(f"{arg.value!r}: German text {german!r} has no mnemonic")
                continue
            letter = german[index + 1].casefold()
            if letter in letters and letters[letter] != german:
                clashes.append(f"{letter!r}: {letters[letter]!r} and {german!r}")
            letters[letter] = german
        self.assertEqual(clashes, [])

    def test_known_gui_string_translates(self) -> None:
        self.assertEqual(_load_german().gettext("&Unmount"), "&Aushängen")


@unittest.skipUnless(shutil.which("msgfmt"), "msgfmt (gettext) is not installed")
class CompiledCatalogFreshnessTests(unittest.TestCase):
    def test_committed_mo_matches_po(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            fresh = Path(tmp_dir) / "fresh.mo"
            subprocess.run(["msgfmt", "--check", "-o", str(fresh), str(PO_PATH)], check=True)
            self.assertEqual(
                _catalog(MO_PATH),
                _catalog(fresh),
                "tiptoi.mo is stale - recompile it with: msgfmt -o tiptoi.mo tiptoi.po",
            )


if __name__ == "__main__":
    unittest.main()
