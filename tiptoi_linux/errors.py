"""Shared base exception for tiptoi-linux."""

from __future__ import annotations


class TiptoiError(Exception):
    """Base class for every tiptoi-linux failure the CLI reports."""
