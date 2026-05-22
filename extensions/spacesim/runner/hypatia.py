"""
Hypatia simulation runner.

Wraps ``./waf --run "main_satnet --run_dir=..."`` in a thread-friendly
class with line-streamed stdout and a hard timeout. Streamlit's main
thread polls :meth:`HypatiaRunner.poll` to update the UI.

Why a class and not just ``subprocess.run``:

- Streamlit needs to keep its event loop responsive; ``subprocess.run``
  blocks until completion. We use :func:`subprocess.Popen` plus a reader
  thread that drains stdout into an internal buffer.
- We want incremental log access (every line as it arrives) so the
  Logs tab can show progress in near-real-time.
- The build / run can occasionally hang (e.g., when fstate files are
  truncated); a deadline is a safety net.
"""

from __future__ import annotations

import os
import subprocess
import sys
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, List, Optional


# Paths --------------------------------------------------------------------

_HYPATIA_ROOT = Path(__file__).resolve().parent.parent.parent.parent
_NS3_SIM_DIR = _HYPATIA_ROOT / "ns3-sat-sim" / "simulator"
_VENV_BIN = (_HYPATIA_ROOT.parent / "venv" / "bin").as_posix()


@dataclass
class RunResult:
    returncode: int
    log_lines: List[str]
    duration_seconds: float
    run_dir: Path
    timed_out: bool = False
    error_message: Optional[str] = None

    @property
    def success(self) -> bool:
        return self.returncode == 0 and not self.timed_out


class HypatiaRunner:
    """Threaded wrapper around ``./waf --run main_satnet``.

    Lifecycle:

      r = HypatiaRunner(run_dir)
      r.start()
      while not r.is_done():
          for line in r.consume_new_lines():
              print(line)
          time.sleep(0.1)
      result = r.finalise()
    """

    def __init__(
        self,
        run_dir: Path,
        *,
        ns3_sim_dir: Path = _NS3_SIM_DIR,
        timeout_seconds: int = 3600,
    ):
        self.run_dir = Path(run_dir).resolve()
        self.ns3_sim_dir = Path(ns3_sim_dir).resolve()
        self.timeout_seconds = timeout_seconds

        self._proc: Optional[subprocess.Popen] = None
        self._reader: Optional[threading.Thread] = None
        self._log_lines: List[str] = []
        self._line_lock = threading.Lock()
        self._consumed_up_to = 0  # index into _log_lines
        self._start_ts: Optional[float] = None
        self._end_ts: Optional[float] = None
        self._error: Optional[str] = None

    # --- Lifecycle -----------------------------------------------------

    def start(self) -> None:
        """Launch the subprocess and the reader thread."""
        if self._proc is not None:
            raise RuntimeError("HypatiaRunner has already been started")

        if not self.run_dir.exists():
            raise FileNotFoundError(f"run_dir {self.run_dir} does not exist")
        if not (self.ns3_sim_dir / "waf").exists():
            raise FileNotFoundError(
                f"waf not found at {self.ns3_sim_dir / 'waf'} — has ns-3 been built?"
            )

        env = os.environ.copy()
        # Ensure the venv's python is on PATH so any waf-helper scripts pick it up.
        env["PATH"] = f"{_VENV_BIN}:{env.get('PATH', '')}"

        # Hypatia's expected shell pattern: ./waf --run "main_satnet --run_dir='...'"
        # We avoid quoting headaches by passing the whole thing as one arg.
        cmd = [
            "./waf",
            "--run",
            f"main_satnet --run_dir='{self.run_dir.as_posix()}'",
        ]

        self._start_ts = time.time()
        self._proc = subprocess.Popen(
            cmd,
            cwd=self.ns3_sim_dir,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1,
            env=env,
        )
        self._append(f"$ cd {self.ns3_sim_dir}")
        self._append(f"$ {' '.join(cmd)}")
        self._reader = threading.Thread(target=self._read_lines, daemon=True)
        self._reader.start()

    def is_done(self) -> bool:
        if self._proc is None:
            return False
        if self._proc.poll() is None:
            # Check timeout.
            if self._start_ts and (time.time() - self._start_ts) > self.timeout_seconds:
                self._error = f"timed out after {self.timeout_seconds}s"
                try:
                    self._proc.kill()
                except Exception:
                    pass
                return True
            return False
        return True

    def consume_new_lines(self) -> List[str]:
        """Pop log lines added since the last consume."""
        with self._line_lock:
            new = self._log_lines[self._consumed_up_to:]
            self._consumed_up_to = len(self._log_lines)
        return new

    @property
    def all_log_lines(self) -> List[str]:
        with self._line_lock:
            return list(self._log_lines)

    def finalise(self) -> RunResult:
        """Wait for completion (with timeout) and return the result."""
        if self._proc is None:
            raise RuntimeError("HypatiaRunner has not been started")
        timed_out = False
        try:
            self._proc.wait(timeout=max(self.timeout_seconds, 1))
        except subprocess.TimeoutExpired:
            timed_out = True
            self._proc.kill()
            self._proc.wait()
            self._error = self._error or f"timed out after {self.timeout_seconds}s"
        if self._reader is not None:
            self._reader.join(timeout=5)
        self._end_ts = time.time()
        return RunResult(
            returncode=self._proc.returncode if self._proc.returncode is not None else -1,
            log_lines=self.all_log_lines,
            duration_seconds=(self._end_ts - (self._start_ts or self._end_ts)),
            run_dir=self.run_dir,
            timed_out=timed_out,
            error_message=self._error,
        )

    # --- Internals -----------------------------------------------------

    def _read_lines(self) -> None:
        assert self._proc is not None
        assert self._proc.stdout is not None
        try:
            for line in self._proc.stdout:
                self._append(line.rstrip("\n"))
        except Exception as exc:
            self._error = f"reader thread crashed: {exc!r}"
            self._append(f"[ERROR] {self._error}")

    def _append(self, line: str) -> None:
        with self._line_lock:
            self._log_lines.append(line)
