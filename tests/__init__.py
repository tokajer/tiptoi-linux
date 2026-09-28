import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
# WHY: assignment, not setdefault - the developer's own desktop may be German (LANGUAGE=de), but
# the suite's string assertions are written against the English source text, so the language must
# be pinned to English regardless of what's inherited from the shell. Set before anything below
# imports tiptoi_linux, since tiptoi_linux.i18n binds gettext at import time.
os.environ["LANGUAGE"] = "C"
