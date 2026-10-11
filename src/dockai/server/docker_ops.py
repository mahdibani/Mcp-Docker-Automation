"""Thin layer over the docker SDK: connection handling and container summaries."""

from __future__ import annotations

from functools import lru_cache

import docker
from docker.errors import DockerException
from docker.models.containers import Container

from dockai import MANAGED_LABEL_KEY, MANAGED_LABEL_VALUE
from dockai.retry import retry

DOCKER_DOWN_MESSAGE = (
    "Cannot reach the Docker daemon. Is Docker Desktop running? "
    "Start Docker Desktop, wait until it reports 'Engine running', then try again."
)


class DockerUnavailable(RuntimeError):
    pass


@lru_cache(maxsize=1)
def _client() -> docker.DockerClient:
    def connect() -> docker.DockerClient:
        client = docker.from_env(timeout=30)
        client.ping()
        return client

    try:
        # The daemon may still be starting right after Docker Desktop launches.
        return retry(connect, attempts=3, base_delay=1.0, retry_on=(DockerException,))
    except DockerException as exc:
        raise DockerUnavailable(DOCKER_DOWN_MESSAGE) from exc


def client() -> docker.DockerClient:
    return _client()


def is_managed(container: Container) -> bool:
    return container.labels.get(MANAGED_LABEL_KEY) == MANAGED_LABEL_VALUE


def summarize(container: Container) -> dict:
    attrs = container.attrs
    state = attrs.get("State", {})
    ports = {}
    for container_port, bindings in (attrs.get("NetworkSettings", {}).get("Ports") or {}).items():
        if bindings:
            ports[container_port] = sorted({b["HostPort"] for b in bindings})
    return {
        "name": container.name,
        "id": container.short_id,
        "image": attrs.get("Config", {}).get("Image"),
        "status": state.get("Status"),
        "health": (state.get("Health") or {}).get("Status"),
        "ports": ports,
        "managed_by_dockai": is_managed(container),
        "compose_project": container.labels.get("com.docker.compose.project"),
    }
