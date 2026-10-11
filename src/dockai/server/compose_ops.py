"""Stacks: render compose files under .dockai/stacks/<name>/ and drive `docker compose`."""

from __future__ import annotations

import json
import os
import secrets
import subprocess
from pathlib import Path

from dockai.compose.render import StackSpec, render_stack
from dockai.redact import redact
from dockai.retry import retry

COMPOSE_FILE = "docker-compose.yml"
SECRETS_FILE = ".env"
META_FILE = "stack.json"
UP_TIMEOUT_S = 180


class ComposeError(RuntimeError):
    pass


def stacks_root() -> Path:
    return Path(os.getenv("DOCKAI_HOME") or Path.cwd() / ".dockai") / "stacks"


def stack_dir(stack: str) -> Path:
    return stacks_root() / stack


def load_meta(stack: str) -> dict | None:
    path = stack_dir(stack) / META_FILE
    return json.loads(path.read_text(encoding="utf-8")) if path.exists() else None


def _ensure_password(directory: Path) -> None:
    """Reuse the stack's password, else take POSTGRES_PASSWORD from .env, else generate one."""
    secrets_path = directory / SECRETS_FILE
    if secrets_path.exists():
        return
    password = os.getenv("POSTGRES_PASSWORD") or secrets.token_urlsafe(18)
    secrets_path.write_text(f"POSTGRES_PASSWORD={password}\n", encoding="utf-8")


def write_stack(spec: StackSpec, extra_meta: dict | None = None) -> Path:
    directory = stack_dir(spec.stack)
    directory.mkdir(parents=True, exist_ok=True)
    (directory / COMPOSE_FILE).write_text(render_stack(spec), encoding="utf-8")
    _ensure_password(directory)
    (directory / META_FILE).write_text(json.dumps({**spec.to_dict(), **(extra_meta or {})}, indent=2), encoding="utf-8")
    return directory


class _TransientCompose(ComposeError):
    pass


def _compose(stack: str, *args: str, timeout: int = UP_TIMEOUT_S) -> str:
    directory = stack_dir(stack)
    cmd = ["docker", "compose", "-p", stack, "-f", str(directory / COMPOSE_FILE), *args]
    try:
        proc = subprocess.run(cmd, cwd=directory, capture_output=True, text=True, encoding="utf-8",
                              errors="replace", timeout=timeout)
    except FileNotFoundError as exc:
        raise ComposeError("The `docker` CLI was not found on PATH. Is Docker Desktop installed?") from exc
    except subprocess.TimeoutExpired as exc:
        raise ComposeError(f"`docker compose {' '.join(args)}` timed out after {timeout}s.") from exc
    output = redact((proc.stdout + proc.stderr).strip())
    if proc.returncode != 0:
        tail = "\n".join(output.splitlines()[-15:])
        transient = any(s in output.lower() for s in ("timeout", "tls handshake", "connection reset", "eof", "i/o timeout"))
        raise (_TransientCompose if transient else ComposeError)(f"docker compose {args[0]} failed:\n{tail}")
    return output


def pull(stack: str) -> None:
    # Image pulls are the most common transient failure (registry hiccups), so they get backoff.
    retry(lambda: _compose(stack, "pull", "--quiet"), attempts=4, base_delay=2.0, retry_on=(_TransientCompose,))


def up(stack: str) -> None:
    # --wait blocks until every service with a healthcheck reports healthy.
    _compose(stack, "up", "-d", "--remove-orphans", "--wait", "--wait-timeout", str(UP_TIMEOUT_S - 20))


def down(stack: str, delete_volumes: bool) -> str:
    args = ["down", "--remove-orphans"] + (["--volumes"] if delete_volumes else [])
    return _compose(stack, *args)
