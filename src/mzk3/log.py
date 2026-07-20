"""Colored status logging, matching the bash [INFO]/[WARN]/[ERROR]/[STEP] output."""

from __future__ import annotations

import sys

_RED = "\033[0;31m"
_GREEN = "\033[0;32m"
_YELLOW = "\033[1;33m"
_BLUE = "\033[0;34m"
_NC = "\033[0m"


def _emit(color: str, tag: str, msg: str) -> None:
    if sys.stderr.isatty():
        print(f"{color}[{tag}]{_NC} {msg}", file=sys.stderr)
    else:
        print(f"[{tag}] {msg}", file=sys.stderr)


def info(msg: str = "") -> None:
    _emit(_GREEN, "INFO", msg)


def warn(msg: str = "") -> None:
    _emit(_YELLOW, "WARN", msg)


def error(msg: str = "") -> None:
    _emit(_RED, "ERROR", msg)


def step(msg: str = "") -> None:
    _emit(_BLUE, "STEP", msg)
