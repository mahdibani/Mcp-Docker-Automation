"""Compose rendering, secret masking, retries and port checks."""

import socket

import pytest
import yaml

from dockai.compose.render import StackSpec, render_stack
from dockai.redact import MASK, redact
from dockai.retry import backoff_delays, retry
from dockai.server.ports import port_in_use, suggest_free_port


def spec(**kw):
    return StackSpec(**{"stack": "shop_db", "database": "shop_db", "user": "igor", **kw})


def test_stack_without_adminer():
    doc = yaml.safe_load(render_stack(spec()))
    assert set(doc["services"]) == {"postgres"}
    pg = doc["services"]["postgres"]
    assert pg["image"] == "postgres:16"
    assert pg["ports"] == ["127.0.0.1:5432:5432"]
    assert pg["environment"] == {"POSTGRES_DB": "shop_db", "POSTGRES_USER": "igor"}
    assert "pg_isready -U igor -d shop_db" in pg["healthcheck"]["test"][1]


def test_every_resource_is_labelled_managed_by_dockai():
    doc = yaml.safe_load(render_stack(spec(with_adminer=True, adminer_port=8081)))
    labelled = [*doc["services"].values(), *doc["volumes"].values(), *doc["networks"].values()]
    assert labelled and all(item["labels"]["managed-by"] == "dockai" for item in labelled)


def test_adminer_joins_the_network_and_waits_for_healthy_postgres():
    doc = yaml.safe_load(render_stack(spec(with_adminer=True, adminer_port=8081)))
    adminer = doc["services"]["adminer"]
    assert adminer["ports"] == ["127.0.0.1:8081:8080"]
    assert adminer["networks"] == doc["services"]["postgres"]["networks"]
    assert adminer["depends_on"] == {"postgres": {"condition": "service_healthy"}}


def test_password_never_in_compose_file():
    text = render_stack(spec())
    assert "POSTGRES_PASSWORD" not in text.split("\n", 2)[2]  # only mentioned in the header comment
    assert yaml.safe_load(text)["services"]["postgres"]["env_file"] == ".env"


@pytest.mark.parametrize("version,mount", [("16", "/var/lib/postgresql/data"), ("18", "/var/lib/postgresql")])
def test_data_mount_follows_postgres_version(version, mount):
    doc = yaml.safe_load(render_stack(spec(postgres_version=version)))
    assert doc["services"]["postgres"]["volumes"] == [f"pgdata:{mount}"]


@pytest.mark.parametrize("kw", [
    {"stack": "Bad Name"}, {"database": "drop;table"}, {"user": "a b"},
    {"postgres_version": "latest; rm"}, {"postgres_port": 0},
    {"with_adminer": True, "adminer_port": 5432},
])
def test_invalid_input_is_rejected_before_rendering(kw):
    with pytest.raises(ValueError):
        render_stack(spec(**kw))


def test_redact_masks_secret_keys_and_assignments():
    data = {"password": "hunter2", "nested": [{"api_key": "k"}], "env": ["POSTGRES_PASSWORD=s3cret", "A=1"], "user": "igor"}
    assert redact(data) == {"password": MASK, "nested": [{"api_key": MASK}], "env": [f"POSTGRES_PASSWORD={MASK}", "A=1"], "user": "igor"}


def test_backoff_is_exponential_and_capped():
    assert backoff_delays(4, 1.0) == [1.0, 2.0, 4.0]
    assert backoff_delays(8, 10.0, cap=30.0)[-1] == 30.0


def test_retry_succeeds_after_transient_failures():
    attempts, sleeps = [], []

    def flaky():
        attempts.append(1)
        if len(attempts) < 3:
            raise ConnectionError("daemon starting")
        return "ok"

    assert retry(flaky, attempts=4, base_delay=1.0, retry_on=(ConnectionError,), sleep=sleeps.append) == "ok"
    assert sleeps == [1.0, 2.0]


def test_retry_does_not_swallow_other_errors():
    with pytest.raises(ValueError):
        retry(lambda: (_ for _ in ()).throw(ValueError("bad input")), retry_on=(ConnectionError,), sleep=lambda _: None)


def _free_run(length=4, start=20000):
    # High ephemeral ports can sit in Windows/Hyper-V reserved ranges, so search a normal range.
    for base in range(start, start + 2000, 7):
        if not any(port_in_use(p) for p in range(base, base + length)):
            return base
    pytest.skip("no free port run found")


def test_port_check_detects_a_listener_and_suggests_another_port():
    port = _free_run()
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", port))
        listener.listen()
        assert port_in_use(port)
        suggestion = suggest_free_port(port, taken={port + 1})
        assert suggestion and suggestion not in (port, port + 1) and not port_in_use(suggestion)
