import json

import httpx
import pytest

from scim_client.db import connect
from scim_client.sync import CORE, LIST, synchronize
from scim_client.web import create_app


@pytest.fixture
def db(tmp_path):
    connection = connect(tmp_path / "test.sqlite3")
    connection.execute(
        "UPDATE settings SET base_url='http://provider/scim/v2',token='secret-token'"
    )
    connection.commit()
    yield connection
    connection.close()


class Provider:
    """Small protocol double: records requests and applies user/member PATCHes."""

    def __init__(self, db):
        settings = db.execute("SELECT * FROM settings").fetchone()
        self.users = {}
        self.groups = {
            g["id"]: {
                "schemas": [CORE + "Group", settings["group_extension"]],
                "id": g["id"],
                "displayName": g["display_name"],
                settings["group_extension"]: {"companyId": g["company_id"]},
                "members": [{"value": "remote-only-user"}],
            }
            for g in db.execute("SELECT * FROM groups")
        }
        self.requests = []
        self.fail_patch = False
        self.lose_create_response = False

    def __call__(self, request):
        self.requests.append(request)
        assert request.headers["authorization"] == "Bearer secret-token"
        parts = request.url.path.split("/")
        resource = parts[3]
        objects = self.users if resource == "Users" else self.groups
        if request.method == "GET":
            if len(parts) == 5:
                return httpx.Response(200, json=objects[parts[4]])
            rows = list(objects.values())
            if "filter" in request.url.params:
                key, value = request.url.params["filter"].split(" eq ", 1)
                rows = [r for r in rows if r.get(key) == json.loads(value)]
            start = int(request.url.params.get("startIndex", 1))
            # Deliberately return smaller pages than the client requested.
            return httpx.Response(
                200,
                json={
                    "schemas": [LIST],
                    "totalResults": len(rows),
                    "startIndex": start,
                    "Resources": rows[start - 1 : start + 1],
                },
            )
        body = json.loads(request.content)
        if request.method == "POST":
            assert resource == "Users"
            body["id"] = "remote-" + body["externalId"]
            objects[body["id"]] = body
            if self.lose_create_response:
                self.lose_create_response = False
                raise httpx.ReadTimeout("lost response")
            return httpx.Response(201, json=body)
        assert request.method == "PATCH"
        if self.fail_patch:
            return httpx.Response(503, json={"detail": "secret-token"})
        obj = objects[parts[4]]
        for op in body["Operations"]:
            if resource == "Users":
                obj[op["path"]] = op["value"]
            elif op["op"] == "add":
                existing = {m["value"] for m in obj["members"]}
                obj["members"].extend(
                    m for m in op["value"] if m["value"] not in existing
                )
            else:
                uid = json.loads(op["path"][len("members[value eq ") : -1])
                obj["members"] = [m for m in obj["members"] if m["value"] != uid]
        return httpx.Response(204)


def run(db, provider, mode="incremental"):
    return synchronize(db, mode, httpx.MockTransport(provider))


def test_full_sync_creates_users_preserves_remote_only_members_and_is_idempotent(db):
    provider = Provider(db)
    provider.users["remote-only-user"] = {
        "id": "remote-only-user",
        "userName": "untouched@example.test",
    }
    run(db, provider, "full")
    assert len(provider.users) == 8
    assert provider.users["remote-only-user"] == {
        "id": "remote-only-user",
        "userName": "untouched@example.test",
    }
    assert all(
        {"value": "remote-only-user"} in g["members"] for g in provider.groups.values()
    )
    assert (
        db.execute(
            "SELECT COUNT(*) FROM users WHERE revision != synced_revision"
        ).fetchone()[0]
        == 0
    )
    provider.requests.clear()
    run(db, provider, "full")
    assert all(r.method == "GET" for r in provider.requests)
    assert any(r.url.params.get("startIndex") == "3" for r in provider.requests)
    provider.requests.clear()
    run(db, provider)
    assert provider.requests == []


def test_failure_keeps_changes_pending_and_journal_redacts_token(db):
    provider = Provider(db)
    run(db, provider)
    db.execute(
        "UPDATE users SET given_name='Updated',revision=revision+1 "
        "WHERE id='user-kaladin'"
    )
    db.commit()
    provider.fail_patch = True
    run(db, provider)
    user = db.execute("SELECT * FROM users WHERE id='user-kaladin'").fetchone()
    assert user["synced_revision"] != user["revision"]
    assert (
        db.execute("SELECT status FROM runs ORDER BY id DESC").fetchone()[0] == "failed"
    )
    journal = str([tuple(r) for r in db.execute("SELECT * FROM journal")])
    assert "secret-token" not in journal
    assert "[REDACTED]" in journal
    provider.fail_patch = False
    run(db, provider)
    assert provider.users["remote-user-kaladin"]["name"]["givenName"] == "Updated"


def test_uncertain_create_is_correlated_on_next_run_without_duplicate(db):
    provider = Provider(db)
    provider.lose_create_response = True
    run(db, provider)
    run(db, provider)
    assert len(provider.users) == 7
    assert len([r for r in provider.requests if r.method == "POST"]) == 7


def test_deactivation_removes_managed_memberships_and_reactivation_restores_them(db):
    provider = Provider(db)
    run(db, provider)
    db.execute("UPDATE users SET active=0,revision=revision+1 WHERE id='user-kaladin'")
    db.commit()
    run(db, provider)
    assert provider.users["remote-user-kaladin"]["active"] is False
    assert all(
        {"value": "remote-user-kaladin"} not in g["members"]
        for g in provider.groups.values()
    )
    db.execute("UPDATE users SET active=1,revision=revision+1 WHERE id='user-kaladin'")
    db.commit()
    run(db, provider)
    assert {"value": "remote-user-kaladin"} in provider.groups["group-engineering"][
        "members"
    ]


def test_missing_group_is_not_created_and_remains_pending(db):
    provider = Provider(db)
    del provider.groups["group-engineering"]
    run(db, provider)
    assert (
        db.execute(
            "SELECT synced_revision FROM groups WHERE id='group-engineering'"
        ).fetchone()[0]
        == 0
    )
    assert not any(
        r.method == "POST" and r.url.path.endswith("Groups") for r in provider.requests
    )


def test_cross_company_mapping_is_rejected(db):
    provider = Provider(db)
    run(db, provider)
    settings = db.execute("SELECT * FROM settings").fetchone()
    provider.users["remote-user-kaladin"][settings["user_extension"]]["companyId"] = (
        "another-company"
    )
    provider.requests.clear()
    run(db, provider, "full")
    assert (
        db.execute(
            "SELECT synced_revision FROM users WHERE id='user-kaladin'"
        ).fetchone()[0]
        == 0
    )
    assert not any(
        r.method == "PATCH" and r.url.path.endswith("remote-user-kaladin")
        for r in provider.requests
    )


def test_ui_edits_are_local_and_require_csrf(tmp_path):
    path = tmp_path / "web.sqlite3"
    app = create_app(path)
    app.testing = True
    client = app.test_client()
    assert client.get("/").status_code == 200
    assert client.post("/action/active", data={"id": "user-kaladin"}).status_code == 403
    with client.session_transaction() as session:
        csrf = session["csrf"]
    assert (
        client.post(
            "/action/active", data={"id": "user-kaladin", "csrf": csrf}
        ).status_code
        == 302
    )
    db = connect(path)
    assert (
        db.execute("SELECT active FROM users WHERE id='user-kaladin'").fetchone()[0]
        == 0
    )
    assert db.execute("SELECT COUNT(*) FROM runs").fetchone()[0] == 0
    assert db.execute("SELECT COUNT(*) FROM journal").fetchone()[0] == 0
    db.close()
