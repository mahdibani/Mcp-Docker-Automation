"""Validate stack parameters and render the compose template."""

from __future__ import annotations

import re
from dataclasses import asdict, dataclass
from pathlib import Path

from jinja2 import Environment, FileSystemLoader, StrictUndefined

_TEMPLATES = Path(__file__).parent / "templates"
_env = Environment(loader=FileSystemLoader(_TEMPLATES), undefined=StrictUndefined, keep_trailing_newline=True)

STACK_NAME = re.compile(r"^[a-z0-9][a-z0-9_-]{0,62}$")
PG_IDENTIFIER = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,62}$")
PG_VERSION = re.compile(r"^\d{1,2}(\.\d{1,2})?(-alpine|-bookworm|-trixie)?$")


@dataclass
class StackSpec:
    stack: str
    database: str
    user: str
    postgres_version: str = "16"
    postgres_port: int = 5432
    with_adminer: bool = False
    adminer_port: int = 8080

    def validate(self) -> None:
        if not STACK_NAME.match(self.stack):
            raise ValueError(f"Invalid stack name {self.stack!r}: use lowercase letters, digits, '-' or '_'.")
        for label, value in (("database", self.database), ("user", self.user)):
            if not PG_IDENTIFIER.match(value):
                raise ValueError(f"Invalid {label} {value!r}: use letters, digits and '_' (max 63 chars).")
        if not PG_VERSION.match(self.postgres_version):
            raise ValueError(f"Invalid Postgres version {self.postgres_version!r} (examples: 16, 16.4, 17-alpine).")
        for port in (self.postgres_port, self.adminer_port):
            if not 1 <= port <= 65535:
                raise ValueError(f"Invalid port {port}.")
        if self.with_adminer and self.adminer_port == self.postgres_port:
            raise ValueError("Adminer and Postgres cannot use the same host port.")

    @property
    def pgdata_mount(self) -> str:
        # Postgres 18+ images keep data under /var/lib/postgresql instead of .../data.
        return "/var/lib/postgresql" if int(self.postgres_version.split(".")[0].split("-")[0]) >= 18 else "/var/lib/postgresql/data"

    def to_dict(self) -> dict:
        return asdict(self)


def render_stack(spec: StackSpec) -> str:
    spec.validate()
    return _env.get_template("postgres-stack.yml.j2").render(**spec.to_dict(), pgdata_mount=spec.pgdata_mount)
