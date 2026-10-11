"""MCP server exposing typed Docker tools. Run with `uv run dockai-server` (stdio transport)."""

from __future__ import annotations

import logging
import os
import shutil
import sys

from docker.errors import APIError, ImageNotFound, NotFound

from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.types import ToolAnnotations

from dockai import MANAGED_LABEL_KEY, MANAGED_LABEL_VALUE, MANAGED_LABELS
from dockai.compose.render import StackSpec
from dockai.redact import redact
from dockai.retry import retry
from dockai.server import compose_ops, docker_ops
from dockai.server.compose_ops import ComposeError
from dockai.server.docker_ops import DockerUnavailable
from dockai.server.ports import port_in_use, suggest_free_port

# stdout carries the MCP protocol, so logs must go to stderr.
logging.basicConfig(stream=sys.stderr, level=logging.WARNING, format="[dockai-server] %(levelname)s %(message)s")

READ_ONLY = ToolAnnotations(read_only_hint=True, destructive_hint=False)
WRITE = ToolAnnotations(read_only_hint=False, destructive_hint=False)
DESTRUCTIVE = ToolAnnotations(read_only_hint=False, destructive_hint=True)
MANAGED_FILTER = {"label": f"{MANAGED_LABEL_KEY}={MANAGED_LABEL_VALUE}"}

mcp = MCPServer(
    name="dockai",
    instructions=(
        "Docker tools for dockai. Everything created is labelled managed-by=dockai; "
        "stop/remove tools only touch labelled resources."
    ),
    log_level="WARNING",
)


def _docker():
    try:
        return docker_ops.client()
    except DockerUnavailable as exc:
        raise ToolError(str(exc)) from exc


def _find_container(name: str):
    """Exact name or id first; otherwise a unique container whose name starts with `name-` (e.g. a stack name)."""
    client = _docker()
    try:
        return client.containers.get(name)
    except NotFound:
        pass
    candidates = [c for c in client.containers.list(all=True) if c.name.startswith(f"{name}-")]
    if len(candidates) == 1:
        return candidates[0]
    if candidates:
        raise ToolError(f"'{name}' matches several containers: {[c.name for c in candidates]}. Pick one.")
    raise ToolError(f"No container named '{name}'. Use list_containers to see what exists.")


def _find_managed_container(name: str):
    container = _find_container(name)
    if not docker_ops.is_managed(container):
        raise ToolError(
            f"Refusing: container '{container.name}' was not created by dockai (no {MANAGED_LABEL_KEY}={MANAGED_LABEL_VALUE} label)."
        )
    return container


def _ports_used_by_containers(exclude_project: str | None = None) -> set[int]:
    used = set()
    for c in _docker().containers.list():
        if exclude_project and c.labels.get("com.docker.compose.project") == exclude_project:
            continue
        for bindings in (c.attrs.get("NetworkSettings", {}).get("Ports") or {}).values():
            used.update(int(b["HostPort"]) for b in bindings or [] if b.get("HostPort"))
    return used


def _require_free_port(port: int, purpose: str, param: str, exclude_project: str | None = None) -> None:
    if exclude_project:
        own = _docker().containers.list(filters={"label": f"com.docker.compose.project={exclude_project}"})
        own_ports = {
            int(b["HostPort"])
            for c in own
            for bindings in (c.attrs.get("NetworkSettings", {}).get("Ports") or {}).values()
            for b in bindings or []
            if b.get("HostPort")
        }
        if port in own_ports:
            return  # the stack already holds this port; updating it in place is fine
    taken_by_containers = _ports_used_by_containers(exclude_project)
    if port in taken_by_containers or port_in_use(port):
        suggestion = suggest_free_port(port, taken=taken_by_containers)
        hint = f" Port {suggestion} is free: call again with {param}={suggestion}." if suggestion else ""
        raise ToolError(f"Port {port} ({purpose}) is already in use on this machine.{hint}")


# ---------------------------------------------------------------- read-only


@mcp.tool(annotations=READ_ONLY)
def docker_status() -> dict:
    """Check that the Docker daemon is reachable and report its version."""
    info = _docker().version()
    return {"reachable": True, "version": info.get("Version"), "os": info.get("Os"), "arch": info.get("Arch")}


@mcp.tool(annotations=READ_ONLY)
def list_containers(include_stopped: bool = True, only_managed: bool = False) -> dict:
    """List containers with status, health and published ports.

    Args:
        include_stopped: Also list containers that are not running.
        only_managed: Only list containers created by dockai (label managed-by=dockai).
    """
    containers = _docker().containers.list(all=include_stopped, filters=MANAGED_FILTER if only_managed else None)
    return {"containers": [docker_ops.summarize(c) for c in containers]}


@mcp.tool(annotations=READ_ONLY)
def container_status(name: str) -> dict:
    """Detailed status of one container: running state, health check result, ports, start time.

    Args:
        name: Container name or id. A stack name also works when the stack has a single container.
    """
    container = _find_container(name)
    state = container.attrs.get("State", {})
    health = state.get("Health") or {}
    last_check = (health.get("Log") or [{}])[-1]
    return {
        **docker_ops.summarize(container),
        "running": state.get("Running", False),
        "started_at": state.get("StartedAt"),
        "restart_count": container.attrs.get("RestartCount", 0),
        "last_health_check": (last_check.get("Output") or "").strip()[:300] or None,
    }


@mcp.tool(annotations=READ_ONLY)
def get_logs(name: str, tail: int = 50) -> dict:
    """Return the last lines of a container's logs.

    Args:
        name: Container name or id. A stack name also works when the stack has a single container.
        tail: Number of lines from the end of the log (1-1000).
    """
    container = _find_container(name)
    tail = max(1, min(tail, 1000))
    text = container.logs(tail=tail, timestamps=False).decode("utf-8", errors="replace")
    return {"container": container.name, "lines": tail, "logs": redact(text)}


@mcp.tool(annotations=READ_ONLY)
def check_port(port: int) -> dict:
    """Check whether a host port is free; if not, suggest the next free one.

    Args:
        port: TCP port on this machine.
    """
    taken = _ports_used_by_containers()
    in_use = port in taken or port_in_use(port)
    return {"port": port, "free": not in_use, "suggested_port": suggest_free_port(port, taken=taken) if in_use else None}


# ---------------------------------------------------------------- create


@mcp.tool(annotations=WRITE)
def run_container(
    image: str,
    name: str,
    ports: list[str] | None = None,
    env: list[str] | None = None,
    command: str | None = None,
) -> dict:
    """Run a single detached container (for anything that is not a Postgres stack).

    Args:
        image: Image reference, e.g. "redis:7" or "nginx:alpine".
        name: Container name.
        ports: Port mappings "HOST:CONTAINER", e.g. ["6379:6379"]. Bound to 127.0.0.1.
        env: Environment variables "KEY=VALUE".
        command: Optional command overriding the image default.
    """
    client = _docker()
    port_map = {}
    for mapping in ports or []:
        host, _, container_port = mapping.partition(":")
        if not (host.isdigit() and container_port.split("/")[0].isdigit()):
            raise ToolError(f"Invalid port mapping {mapping!r}; expected HOST:CONTAINER like 8080:80.")
        _require_free_port(int(host), f"for {name}", "ports")
        port_map[container_port if "/" in container_port else f"{container_port}/tcp"] = ("127.0.0.1", int(host))
    try:
        client.images.get(image)
    except ImageNotFound:
        retry(lambda: client.images.pull(image), attempts=4, base_delay=2.0, retry_on=(APIError,))
    try:
        container = client.containers.run(
            image, name=name, command=command, detach=True, ports=port_map, environment=env or [],
            labels=dict(MANAGED_LABELS), restart_policy={"Name": "unless-stopped"},
        )
    except APIError as exc:
        raise ToolError(f"Docker refused to start {name}: {exc.explanation or exc}") from exc
    container.reload()
    return docker_ops.summarize(container)


@mcp.tool(annotations=WRITE)
def compose_up(
    stack_name: str,
    database: str | None = None,
    user: str | None = None,
    postgres_version: str | None = None,
    postgres_port: int | None = None,
    with_adminer: bool | None = None,
    adminer_port: int | None = None,
) -> dict:
    """Create or update a Postgres stack (optionally with Adminer) from dockai's compose template.

    Waits until every service is healthy. The stack gets a named volume, a healthcheck and its own
    network; Adminer joins that network so it can reach Postgres by the host name "postgres". The password comes from
    POSTGRES_PASSWORD in .env or is generated; it is stored in the stack folder and never returned.
    For an existing stack, omitted arguments keep their current values.

    Args:
        stack_name: Stack name, e.g. "shop_db". Containers are named <stack_name>-<service>.
        database: Database to create (required for a new stack).
        user: Database user (required for a new stack).
        postgres_version: Postgres image tag, e.g. "16" (default 16).
        postgres_port: Host port for Postgres (default 5432).
        with_adminer: Add (true) or remove (false) the Adminer web UI for browsing the database.
        adminer_port: Host port for Adminer (default from DOCKAI_ADMINER_PORT, else 8080).
    """
    stack = stack_name.lower()
    existing = compose_ops.load_meta(stack) or {}
    spec = StackSpec(
        stack=stack,
        database=database or existing.get("database") or "",
        user=user or existing.get("user") or "",
        postgres_version=str(postgres_version or existing.get("postgres_version") or "16"),
        postgres_port=int(postgres_port or existing.get("postgres_port") or 5432),
        with_adminer=bool(with_adminer if with_adminer is not None else existing.get("with_adminer", False)),
        adminer_port=int(adminer_port or existing.get("adminer_port") or os.getenv("DOCKAI_ADMINER_PORT") or 8080),
    )
    if not spec.database or not spec.user:
        raise ToolError("A new stack needs both `database` and `user`.")
    try:
        spec.validate()
    except ValueError as exc:
        raise ToolError(str(exc)) from exc

    _docker()
    _require_free_port(spec.postgres_port, "Postgres", "postgres_port", exclude_project=stack)
    if spec.with_adminer:
        _require_free_port(spec.adminer_port, "Adminer", "adminer_port", exclude_project=stack)
    directory = compose_ops.write_stack(spec)
    try:
        compose_ops.pull(stack)
        compose_ops.up(stack)
    except ComposeError as exc:
        raise ToolError(f"{exc}\nStack files: {directory}") from exc

    containers = _docker().containers.list(all=True, filters={"label": f"com.docker.compose.project={stack}"})
    return {
        "stack": stack,
        "ready": all(docker_ops.summarize(c)["health"] in (None, "healthy") for c in containers),
        "containers": [docker_ops.summarize(c) for c in containers],
        "connection": {
            "host": "localhost",
            "port": spec.postgres_port,
            "database": spec.database,
            "user": spec.user,
            "password": f"stored in {directory / compose_ops.SECRETS_FILE} (not shown)",
        },
        "adminer": {
            "url": f"http://localhost:{spec.adminer_port}",
            "login": {"system": "PostgreSQL", "server": "postgres", "username": spec.user, "database": spec.database},
        } if spec.with_adminer else None,
    }


# ---------------------------------------------------------------- stop / remove (managed only)


@mcp.tool(annotations=WRITE)
def stop_container(name: str) -> dict:
    """Stop a running container created by dockai. Data is kept; it can be started again.

    Args:
        name: Container name or id.
    """
    container = _find_managed_container(name)
    container.stop(timeout=15)
    container.reload()
    return docker_ops.summarize(container)


@mcp.tool(annotations=DESTRUCTIVE)
def remove_container(name: str, force: bool = False) -> dict:
    """Remove a container created by dockai. Its named volumes are kept.

    Args:
        name: Container name or id.
        force: Also remove it if it is still running.
    """
    container = _find_managed_container(name)
    try:
        container.remove(force=force)
    except APIError as exc:
        raise ToolError(f"Could not remove {container.name}: {exc.explanation or exc}. Stop it first or use force.") from exc
    return {"removed": container.name}


@mcp.tool(annotations=DESTRUCTIVE)
def compose_down(stack_name: str, delete_volumes: bool = False) -> dict:
    """Stop and remove a dockai stack's containers and network.

    Args:
        stack_name: Stack name used with compose_up.
        delete_volumes: Also delete the stack's data volume (all database data is lost).
    """
    stack = stack_name.lower()
    if compose_ops.load_meta(stack) is None:
        raise ToolError(f"No dockai stack named '{stack}'.")
    try:
        compose_ops.down(stack, delete_volumes)
    except ComposeError as exc:
        raise ToolError(str(exc)) from exc
    if delete_volumes:
        # Nothing left to reconnect to, so the stored password goes too.
        shutil.rmtree(compose_ops.stack_dir(stack), ignore_errors=True)
    return {"stack": stack, "removed": True, "volumes_deleted": delete_volumes}


@mcp.tool(annotations=DESTRUCTIVE)
def prune_managed(include_volumes: bool = False) -> dict:
    """Remove every container and network created by dockai (label managed-by=dockai), optionally volumes too.

    Never touches resources without the label.

    Args:
        include_volumes: Also delete dockai's volumes (all their data is lost).
    """
    client = _docker()
    removed = {"containers": [], "networks": [], "volumes": []}
    for c in client.containers.list(all=True, filters=MANAGED_FILTER):
        c.remove(force=True)
        removed["containers"].append(c.name)
    for n in client.networks.list(filters=MANAGED_FILTER):
        n.remove()
        removed["networks"].append(n.name)
    if include_volumes:
        for v in client.volumes.list(filters=MANAGED_FILTER):
            v.remove(force=True)
            removed["volumes"].append(v.name)
        for meta in compose_ops.stacks_root().glob("*/stack.json"):
            shutil.rmtree(meta.parent, ignore_errors=True)
    return removed


def main() -> None:
    mcp.run("stdio")


if __name__ == "__main__":
    main()
