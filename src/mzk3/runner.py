"""The single subprocess boundary. All side effects flow through Runner.

Tests use `dry_run=True` (records calls, executes nothing) or run cheap real
commands; production code uses a live Runner.
"""

from __future__ import annotations

import shutil
import subprocess
from collections.abc import Callable
from dataclasses import dataclass


@dataclass
class Result:
    returncode: int
    stdout: str
    stderr: str

    @property
    def ok(self) -> bool:
        return self.returncode == 0


class CommandError(RuntimeError):
    def __init__(self, argv: list[str], result: Result):
        self.argv = argv
        self.result = result
        super().__init__(
            f"command failed ({result.returncode}): {' '.join(argv)}\n{result.stderr}"
        )


class Runner:
    def __init__(
        self,
        dry_run: bool = False,
        responder: Callable[[list[str]], Result | None] | None = None,
        binaries: set[str] | None = None,
    ):
        self.dry_run = dry_run
        self.calls: list[list[str]] = []
        # Test seams: `responder` supplies canned Results per argv (dry_run only),
        # `binaries` overrides which() so tests don't depend on the host PATH.
        self._responder = responder
        self._binaries = binaries

    def which(self, binary: str) -> str | None:
        if self._binaries is not None:
            return f"/usr/bin/{binary}" if binary in self._binaries else None
        return shutil.which(binary)

    def run(
        self,
        argv: list[str],
        *,
        check: bool = True,
        capture: bool = False,
        text_input: str | None = None,
    ) -> Result:
        """Run argv. `check` raises CommandError on nonzero; `capture` keeps stdout."""
        self.calls.append(list(argv))

        if self.dry_run:
            if self._responder is not None:
                canned = self._responder(argv)
                if canned is not None:
                    return canned
            return Result(0, "", "")

        proc = subprocess.run(
            argv,
            input=text_input,
            capture_output=capture,
            text=True,
        )
        result = Result(
            proc.returncode,
            (proc.stdout or "") if capture else "",
            (proc.stderr or "") if capture else "",
        )
        if check and not result.ok:
            raise CommandError(argv, result)
        return result
