import httpx
import pytest

from scim_client.db import connect
from scim_client.sync import LIST, Client, SyncError


def test_rate_limited_read_is_retried_and_each_attempt_is_journaled(tmp_path):
    db = connect(tmp_path / "client.sqlite3")
    db.execute("UPDATE settings SET base_url='http://provider',token='test-token'")
    run_id = db.execute("INSERT INTO runs(mode) VALUES('full')").lastrowid
    attempts = []

    def respond(request):
        attempts.append(request)
        if len(attempts) == 1:
            return httpx.Response(429, headers={"Retry-After": "0"})
        return httpx.Response(
            200, json={"schemas": [LIST], "totalResults": 0, "Resources": []}
        )

    client = Client(db, run_id, httpx.MockTransport(respond))
    try:
        assert client.list("Users") == []
        assert [r[0] for r in db.execute("SELECT status FROM journal ORDER BY id")] == [
            429,
            200,
        ]
    finally:
        client.http.close()
        db.close()


def test_repeated_page_is_rejected_instead_of_looping_forever(tmp_path):
    db = connect(tmp_path / "client.sqlite3")
    db.execute("UPDATE settings SET base_url='http://provider',token='test-token'")
    run_id = db.execute("INSERT INTO runs(mode) VALUES('full')").lastrowid

    def respond(request):
        return httpx.Response(
            200,
            json={
                "schemas": [LIST],
                "totalResults": 2,
                "Resources": [{"id": "same-id"}],
            },
        )

    client = Client(db, run_id, httpx.MockTransport(respond))
    try:
        with pytest.raises(SyncError, match="repeated resource ID"):
            client.list("Users")
    finally:
        client.http.close()
        db.close()
