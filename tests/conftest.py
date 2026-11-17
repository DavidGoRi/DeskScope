from __future__ import annotations

import re

import pytest
from fastapi.testclient import TestClient

from deskscope import security, users
from deskscope.db import connect, init_db
from deskscope.web import create_app

PASSWORD = "correct-horse-battery"


@pytest.fixture(autouse=True)
def fast_hashing(monkeypatch):
    """Real PBKDF2, but with few rounds so the suite stays fast."""
    monkeypatch.setattr(security, "ITERATIONS", 1000)


@pytest.fixture
def db_path(tmp_path):
    path = tmp_path / "desk.db"
    init_db(path)
    return path


@pytest.fixture
def conn(db_path):
    c = connect(db_path)
    yield c
    c.close()


@pytest.fixture
def people(conn):
    """A small desk: one admin, two technicians and two employees."""
    make = lambda u, n, r: users.create_user(conn, u, n, f"{u}@example.com", r, PASSWORD)  # noqa: E731
    return {
        "admin": make("admin", "Ada Admin", "admin"),
        "agent": make("tech1", "Tom Tech", "agent"),
        "agent2": make("tech2", "Tina Tech", "agent"),
        "req": make("ana", "Ana Example", "requester"),
        "req2": make("bob", "Bob Example", "requester"),
    }


@pytest.fixture
def app(db_path, people):
    return create_app(db_path, "test-secret-key")


@pytest.fixture
def client(app):
    with TestClient(app) as c:
        yield c


CSRF_RE = re.compile(r'name="csrf_token" value="([^"]+)"')


def csrf_from(client: TestClient, url: str = "/tickets") -> str:
    page = client.get(url)
    match = CSRF_RE.search(page.text)
    assert match, f"no CSRF token on {url}"
    return match.group(1)


def login(client: TestClient, username: str, password: str = PASSWORD):
    token = csrf_from(client, "/login")
    return client.post(
        "/login",
        data={"username": username, "password": password, "csrf_token": token, "next": "/tickets"},
        follow_redirects=False,
    )
