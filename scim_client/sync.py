"""Explicit reconciliation, with checkpoints only after successful operations."""

import json
import sqlite3
import time
from urllib.parse import quote

import httpx

CORE = "urn:ietf:params:scim:schemas:core:2.0:"
PATCH = "urn:ietf:params:scim:api:messages:2.0:PatchOp"
LIST = "urn:ietf:params:scim:api:messages:2.0:ListResponse"


class SyncError(Exception):
    pass


def pending(db):
    return {
        table: [
            dict(row)
            for row in db.execute(
                f"SELECT * FROM {table} WHERE revision != synced_revision"
            )
        ]
        for table in ("users", "groups")
    }


def user_payload(user, settings):
    extension = settings["user_extension"]
    return {
        "schemas": [CORE + "User", extension],
        "externalId": user["id"],
        "userName": user["username"],
        "name": {"givenName": user["given_name"], "familyName": user["family_name"]},
        "emails": [{"value": user["email"], "type": "work", "primary": True}],
        "active": bool(user["active"]),
        extension: {"companyId": user["company_id"]},
    }


class Client:
    def __init__(self, db, run_id, transport=None):
        self.db = db
        self.run_id = run_id
        self.settings = dict(db.execute("SELECT * FROM settings").fetchone())
        self.http = httpx.Client(
            timeout=20,
            follow_redirects=False,
            transport=transport,
            trust_env=False,
            headers={
                "Authorization": "Bearer " + self.settings["token"],
                "Accept": "application/scim+json",
                "Content-Type": "application/scim+json",
            },
        )

    def redact(self, value):
        text = json.dumps(value, ensure_ascii=False)
        token = self.settings["token"]
        # Redact both raw and JSON-escaped forms, including reflected credentials.
        if token:
            text = text.replace(json.dumps(token)[1:-1], "[REDACTED]")
            text = text.replace(token, "[REDACTED]")
        return text

    def request(self, method, path, body=None, params=None):
        url = self.settings["base_url"].rstrip("/") + "/" + path.lstrip("/")
        for attempt in range(1, 4):
            start = time.monotonic()
            response = None
            error = None
            try:
                response = self.http.request(method, url, json=body, params=params)
                try:
                    result = response.json() if response.content else None
                except ValueError:
                    result = response.text
            except httpx.HTTPError as exc:
                error = type(exc).__name__
                result = {"error": error}
            request = {
                "headers": {
                    "Authorization": "Bearer [REDACTED]",
                    "Accept": "application/scim+json",
                    "Content-Type": "application/scim+json",
                },
                "body": body,
            }
            reply = {
                "body": result,
                "headers": {
                    key: value
                    for key, value in (
                        response.headers.items() if response is not None else []
                    )
                    if key
                    in {
                        "content-type",
                        "etag",
                        "retry-after",
                        "x-rate-limit-retry-after-seconds",
                    }
                },
            }
            self.db.execute(
                "INSERT INTO journal(run_id,method,url,request,response,"
                "status,duration_ms,attempt) "
                "VALUES(?,?,?,?,?,?,?,?)",
                (
                    self.run_id,
                    method,
                    self.redact(str(httpx.URL(url, params=params))),
                    self.redact(request),
                    self.redact(reply),
                    response.status_code if response is not None else None,
                    int((time.monotonic() - start) * 1000),
                    attempt,
                ),
            )
            self.db.commit()
            status = response.status_code if response is not None else 0
            if (
                method == "GET"
                and (error or status in {429, 502, 503, 504})
                and attempt < 3
            ):
                delay = (
                    response.headers.get("Retry-After", str(attempt))
                    if response is not None
                    else str(attempt)
                )
                # Do not retry earlier than a long or unsupported Retry-After value.
                if not delay.isdigit() or int(delay) > 30:
                    raise SyncError("Retry deferred; run sync later (see journal)")
                time.sleep(int(delay))
                continue
            if error or not 200 <= status < 300:
                raise SyncError(f"{method} {path}: {error or status}; see journal")
            return result
        raise SyncError("Retry limit reached")

    def list(self, resource, filter_value=None):
        resources = []
        seen = set()
        start = 1
        while True:
            params = {"startIndex": start, "count": 100}
            if filter_value:
                params["filter"] = filter_value
            page = self.request("GET", resource, params=params)
            if not isinstance(page, dict) or LIST not in page.get("schemas", []):
                raise SyncError("Expected a SCIM ListResponse")
            rows = page.get("Resources", [])
            total = page.get("totalResults")
            if not isinstance(rows, list) or type(total) is not int or total < 0:
                raise SyncError("Invalid pagination")
            if page.get("startIndex", start) != start:
                raise SyncError("Provider returned the wrong startIndex")
            for row in rows:
                if (
                    not isinstance(row, dict)
                    or not isinstance(row.get("id"), str)
                    or not row["id"]
                    or row["id"] in seen
                ):
                    raise SyncError("Missing or repeated resource ID in pagination")
                seen.add(row["id"])
            resources.extend(rows)
            if len(resources) >= total:
                return resources
            if not rows or start > 1000000:
                raise SyncError("Pagination made no progress")
            start += len(rows)

    def patch(self, resource, remote_id, operations):
        if operations:
            self.request(
                "PATCH",
                resource + "/" + quote(remote_id, safe=""),
                {"schemas": [PATCH], "Operations": operations},
            )

    def resolve(self, local, resource, catalog=None):
        extension = self.settings[
            "user_extension" if resource == "Users" else "group_extension"
        ]
        if local["remote_id"]:
            if catalog is None:
                remote = self.request(
                    "GET", resource + "/" + quote(local["remote_id"], safe="")
                )
                if (
                    not isinstance(remote, dict)
                    or remote.get("id") != local["remote_id"]
                ):
                    raise SyncError("Resource response does not match the requested ID")
                matches = [remote]
            else:
                matches = [r for r in catalog if r["id"] == local["remote_id"]]
            if not matches:
                raise SyncError(f"Previously mapped {resource} resource is missing")
        else:
            key = "externalId" if resource == "Users" else "displayName"
            value = local["id"] if resource == "Users" else local["display_name"]
            candidates = (
                catalog
                if catalog is not None
                else self.list(resource, f"{key} eq {json.dumps(value)}")
            )
            matches = [
                r
                for r in candidates
                if r.get(key) == value
                and r.get(extension, {}).get("companyId") == local["company_id"]
            ]
            # A conflicting externalId must never result in a duplicate user.
            if resource == "Users" and any(
                r.get(key) == value and r not in matches for r in candidates
            ):
                raise SyncError(
                    "External ID belongs to a different or unspecified company"
                )
        if len(matches) > 1:
            raise SyncError(f"Ambiguous {resource} correlation")
        if matches:
            company = matches[0].get(extension)
            if (
                not isinstance(company, dict)
                or company.get("companyId") != local["company_id"]
            ):
                raise SyncError("Company mismatch; refusing to modify resource")
        return matches[0] if matches else None


def synchronize(db, mode, transport=None):
    if mode not in {"full", "incremental"}:
        raise ValueError("Unknown sync mode")
    settings = db.execute("SELECT * FROM settings").fetchone()
    if not settings["base_url"] or not settings["token"]:
        raise SyncError("Configure the connection and bearer token first")
    run_id = db.execute("INSERT INTO runs(mode) VALUES(?)", (mode,)).lastrowid
    db.commit()
    client = Client(db, run_id, transport)
    failures = []
    try:
        users_catalog = client.list("Users") if mode == "full" else None
        groups_catalog = client.list("Groups") if mode == "full" else None
        users = list(
            db.execute(
                "SELECT * FROM users"
                + (
                    " WHERE revision != synced_revision"
                    if mode == "incremental"
                    else ""
                )
            )
        )
        for user in users:
            try:
                desired = user_payload(user, settings)
                remote = client.resolve(user, "Users", users_catalog)
                if remote is None:
                    remote = client.request("POST", "Users", desired)
                    if (
                        not isinstance(remote, dict)
                        or not isinstance(remote.get("id"), str)
                        or not remote["id"]
                    ):
                        raise SyncError("Create response must contain a resource ID")
                else:
                    changes = [
                        {"op": "replace", "path": key, "value": value}
                        for key, value in desired.items()
                        if key not in {"schemas", settings["user_extension"]}
                        and remote.get(key) != value
                    ]
                    client.patch("Users", remote["id"], changes)
                db.execute(
                    "UPDATE users SET remote_id=?, synced_revision=? WHERE id=?",
                    (remote["id"], user["revision"], user["id"]),
                )
                db.commit()
            except (
                SyncError,
                ValueError,
                TypeError,
                KeyError,
                AttributeError,
                sqlite3.IntegrityError,
            ) as exc:
                failures.append(f"User {user['id']}: {exc}")
                db.execute(
                    "UPDATE users SET synced_revision=0 WHERE id=?", (user["id"],)
                )
                db.commit()
        # User changes also require group membership reconciliation.
        groups = list(
            db.execute(
                "SELECT * FROM groups"
                + (
                    " WHERE revision != synced_revision"
                    if mode == "incremental" and not users
                    else ""
                )
            )
        )
        for group in groups:
            try:
                remote = client.resolve(group, "Groups", groups_catalog)
                if remote is None:
                    raise SyncError("Required provider-owned group does not exist")
                db.execute(
                    "UPDATE groups SET remote_id=? WHERE id=?",
                    (remote["id"], group["id"]),
                )
                db.commit()
                members = list(
                    db.execute(
                        "SELECT u.* FROM users u JOIN memberships m ON m.user_id=u.id "
                        "WHERE m.group_id=?",
                        (group["id"],),
                    )
                )
                if any(
                    not u["remote_id"] or u["revision"] != u["synced_revision"]
                    for u in members
                ):
                    raise SyncError("Membership waits for successful user provisioning")
                desired = {u["remote_id"] for u in members if u["active"]}
                current = {m["value"] for m in remote.get("members", [])}
                managed = {
                    u[0]
                    for u in db.execute(
                        "SELECT remote_id FROM users WHERE remote_id IS NOT NULL"
                    )
                }
                operations = [
                    {"op": "remove", "path": f"members[value eq {json.dumps(uid)}]"}
                    for uid in sorted((current & managed) - desired)
                ]
                additions = desired - current
                if additions:
                    operations.append(
                        {
                            "op": "add",
                            "path": "members",
                            "value": [{"value": uid} for uid in sorted(additions)],
                        }
                    )
                client.patch("Groups", remote["id"], operations)
                db.execute(
                    "UPDATE groups SET synced_revision=revision WHERE id=?",
                    (group["id"],),
                )
                db.commit()
            except (
                SyncError,
                ValueError,
                TypeError,
                KeyError,
                AttributeError,
                sqlite3.IntegrityError,
            ) as exc:
                failures.append(f"Group {group['id']}: {exc}")
                db.execute(
                    "UPDATE groups SET synced_revision=0 WHERE id=?", (group["id"],)
                )
                db.commit()
    except Exception as exc:
        # Record an unexpected protocol failure rather than reporting success.
        failures.append(f"{type(exc).__name__}: {exc}")
    finally:
        client.http.close()
        db.execute(
            "UPDATE runs SET status=?, error=? WHERE id=?",
            ("failed" if failures else "succeeded", client.redact(failures), run_id),
        )
        db.commit()
    return run_id
