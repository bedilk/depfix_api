"""Sandboxed subprocess execution for running an untrusted repo's own
install/test commands.

Everything :mod:`depfix.verify.manager` and :mod:`depfix.verify.runner`
shell out to (``npm ci``, ``npm test``, ...) is code the target repo
controls, not us -- an allowlisted environment (not a denylist), a fixed
argv (never a shell string), a process-group kill on timeout, and an
output byte cap are all here to keep a hostile ``package.json`` script
from doing much beyond wasting CPU time inside its own checkout.
"""

from __future__ import annotations

import contextlib
import logging
import os
import signal
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path

logger = logging.getLogger(__name__)

#: Environment variables passed through to the sandboxed process. Everything
#: else -- credentials, tokens, cloud SDK config, etc. that happen to be set
#: in *our* process environment -- is stripped. An allowlist is used (rather
#: than denylisting known-sensitive names) because we can't enumerate every
#: secret-shaped env var a deployment might set, but we *can* enumerate the
#: handful of things `npm`/`node`/`yarn`/`pnpm` actually need to run.
ENV_ALLOWLIST = frozenset(
    {
        "PATH",
        "LANG",
        "LC_ALL",
        "LC_CTYPE",
        "TMPDIR",
        "TEMP",
        "TMP",
        "CI",
        "NODE_ENV",
        # NODE_OPTIONS and NODE_PATH are excluded: they allow --require/--import
        # flags and module-resolution redirects that can escape the sandbox.
        "NPM_CONFIG_CACHE",
        "NPM_CONFIG_REGISTRY",
        "NPM_CONFIG_PREFIX",
        "NPM_CONFIG_USERCONFIG",
        "npm_config_cache",
        "npm_config_registry",
        "YARN_CACHE_FOLDER",
        "PNPM_HOME",
        # Windows equivalents of PATH/HOME/TEMP -- harmless no-ops on POSIX.
        "SystemRoot",
        "USERPROFILE",
        "APPDATA",
        "LOCALAPPDATA",
    }
)

#: Default cap on how much combined stdout/stderr we'll buffer in memory
#: before truncating -- a runaway `console.log` loop in a repo's own test
#: script shouldn't be able to exhaust our process's memory.
DEFAULT_MAX_OUTPUT_BYTES = 2_000_000

_TRUNCATION_NOTICE = "\n...[output truncated by depfix sandbox]...\n"


@dataclass
class SandboxResult:
    """Outcome of one sandboxed subprocess run."""

    argv: tuple[str, ...]
    exit_code: int
    stdout: str
    stderr: str
    duration_ms: float
    timed_out: bool = False
    error: str = ""

    @property
    def ok(self) -> bool:
        return not self.timed_out and not self.error and self.exit_code == 0


def _sandboxed_env(
    extra_env: dict[str, str] | None, extra_allowlist: frozenset[str] | None
) -> dict[str, str]:
    allowlist = ENV_ALLOWLIST | (extra_allowlist or frozenset())
    env = {k: v for k, v in os.environ.items() if k in allowlist}
    if extra_env:
        env.update(extra_env)
    # Point HOME at a throwaway directory so test scripts cannot read
    # ~/.ssh, ~/.aws/credentials, or ~/.netrc from the real user home.
    env["HOME"] = os.environ.get("TMPDIR") or os.environ.get("TMP") or "/tmp"
    return env


def _truncate(data: bytes, max_bytes: int) -> str:
    if len(data) <= max_bytes:
        return data.decode("utf-8", errors="replace")
    head = data[:max_bytes]
    return head.decode("utf-8", errors="replace") + _TRUNCATION_NOTICE


def _kill_process_group(proc: subprocess.Popen) -> None:
    """Kill the whole process group (not just ``proc`` itself) so a test
    runner's child processes (workers, watchers) don't survive a timeout."""
    try:
        pgid = os.getpgid(proc.pid)
    except ProcessLookupError:
        return
    with contextlib.suppress(ProcessLookupError):
        os.killpg(pgid, signal.SIGKILL)


def run_sandboxed(
    argv: list[str] | tuple[str, ...],
    *,
    cwd: str | Path,
    timeout: float,
    extra_env: dict[str, str] | None = None,
    max_output_bytes: int = DEFAULT_MAX_OUTPUT_BYTES,
    extra_allowlist: frozenset[str] | None = None,
    container_image: str = "",
    container_runtime: str = "docker",
) -> SandboxResult:
    """Run ``argv`` (a fixed arg list, never a shell string) inside ``cwd``
    with an allowlisted environment, killing the whole process group if it
    exceeds ``timeout`` seconds.

    ``extra_allowlist`` widens the pass-through env for one call -- how a
    non-npm ecosystem admits its own tooling vars (``VIRTUAL_ENV``,
    ``PIP_*``, ...; see ``EcosystemSpec.env_allowlist_extra``) without the
    base allowlist growing a union of every ecosystem's needs.

    If ``container_image`` is set, the command runs inside that container
    (using ``container_runtime``: docker, podman, ...) with ``cwd`` mounted
    at /workspace.
    """
    argv = tuple(argv)
    env = _sandboxed_env(extra_env, extra_allowlist)

    # Wrap in container if requested
    if container_image:
        container_argv = [container_runtime, "run", "--rm"]
        # Mount the working directory
        abs_cwd = str(Path(cwd).resolve())
        container_argv.extend(["-v", f"{abs_cwd}:/workspace", "-w", "/workspace"])
        # Pass environment variables
        for key, value in env.items():
            container_argv.extend(["-e", f"{key}={value}"])
        # Add the image and original command
        container_argv.append(container_image)
        container_argv.extend(argv)
        argv = tuple(container_argv)

    start = time.monotonic() * 1000.0
    try:
        proc = subprocess.Popen(
            argv,
            cwd=str(cwd) if not container_image else None,
            env=env if not container_image else None,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            start_new_session=True,  # own process group, for killpg on timeout
        )
    except OSError as exc:
        return SandboxResult(
            argv=argv,
            exit_code=-1,
            stdout="",
            stderr="",
            duration_ms=time.monotonic() * 1000.0 - start,
            error=f"could not start {argv[0]}: {exc}",
        )

    try:
        raw_stdout, raw_stderr = proc.communicate(timeout=timeout)
        timed_out = False
    except subprocess.TimeoutExpired:
        _kill_process_group(proc)
        raw_stdout, raw_stderr = proc.communicate()
        timed_out = True

    duration_ms = time.monotonic() * 1000.0 - start
    return SandboxResult(
        argv=argv,
        exit_code=proc.returncode if proc.returncode is not None else -1,
        stdout=_truncate(raw_stdout, max_output_bytes),
        stderr=_truncate(raw_stderr, max_output_bytes),
        duration_ms=duration_ms,
        timed_out=timed_out,
    )
