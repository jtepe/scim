import secrets
import sqlite3
import threading
import uuid
from urllib.parse import urlsplit

from flask import Flask, abort, flash, redirect, render_template, request, session

from scim_client.db import connect
from scim_client.sync import SyncError, pending, synchronize

# One local process: serialize edits, configuration changes, and synchronization.
LOCK = threading.RLock()


def create_app(db_path=None):
    app = Flask(__name__)
    app.secret_key = secrets.token_hex(32)
    app.config.update(
        TRUSTED_HOSTS=["localhost", "127.0.0.1", "[::1]"],
        MAX_CONTENT_LENGTH=65536,
        SESSION_COOKIE_SAMESITE="Strict",
        SESSION_COOKIE_HTTPONLY=True,
    )

    @app.before_request
    def csrf():
        session.setdefault("csrf", secrets.token_hex(32))
        if request.method == "POST" and not secrets.compare_digest(
            session["csrf"], request.form.get("csrf", "")
        ):
            abort(403)

    @app.after_request
    def security_headers(response):
        response.headers["Cache-Control"] = "no-store"
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Content-Security-Policy"] = (
            "default-src 'self'; style-src 'self' 'unsafe-inline'; "
            "frame-ancestors 'none'; form-action 'self'; base-uri 'none'"
        )
        return response

    @app.get("/")
    def index():
        with LOCK:
            db = connect(db_path)
            try:
                settings = dict(db.execute("SELECT * FROM settings").fetchone())
                settings["has_token"] = bool(settings.pop("token"))
                return render_template(
                    "index.html",
                    settings=settings,
                    pending=pending(db),
                    users=db.execute(
                        "SELECT * FROM users ORDER BY username"
                    ).fetchall(),
                    groups=db.execute(
                        "SELECT * FROM groups ORDER BY display_name"
                    ).fetchall(),
                    companies=db.execute("SELECT * FROM companies").fetchall(),
                    memberships={
                        (r[0], r[1]) for r in db.execute("SELECT * FROM memberships")
                    },
                    runs=db.execute(
                        "SELECT * FROM runs ORDER BY id DESC LIMIT 20"
                    ).fetchall(),
                    journal=db.execute(
                        "SELECT * FROM journal ORDER BY id DESC LIMIT 200"
                    ).fetchall(),
                )
            finally:
                db.close()

    @app.post("/action/<action>")
    def action(action):
        with LOCK:
            db = connect(db_path)
            form = request.form
            try:
                if action == "settings":
                    url = form["base_url"].strip().rstrip("/")
                    parsed = urlsplit(url)
                    if (
                        parsed.scheme not in {"http", "https"}
                        or not parsed.hostname
                        or parsed.username
                        or parsed.password
                        or parsed.query
                        or parsed.fragment
                    ):
                        raise ValueError(
                            "Use an HTTP(S) base URL without credentials, "
                            "query, or fragment"
                        )
                    extensions = [
                        form[k].strip() for k in ("user_extension", "group_extension")
                    ]
                    if any(not value.startswith("urn:") for value in extensions):
                        raise ValueError("Company extensions must be URNs")
                    old = db.execute("SELECT * FROM settings").fetchone()
                    if (url, *extensions) != (
                        old["base_url"],
                        old["user_extension"],
                        old["group_extension"],
                    ):
                        # Resource mappings belong to this endpoint and schema.
                        for table in ("users", "groups"):
                            db.execute(
                                f"UPDATE {table} SET remote_id=NULL, synced_revision=0"
                            )
                    token = form["token"].strip() or old["token"]
                    if any(ord(c) < 33 or ord(c) > 126 for c in token):
                        raise ValueError(
                            "Token must contain printable ASCII without whitespace"
                        )
                    db.execute(
                        "UPDATE settings SET base_url=?,token=?,"
                        "user_extension=?,group_extension=?",
                        (url, token, *extensions),
                    )
                elif action == "company":
                    company_id = form["company_id"].strip()
                    name = form["name"].strip()
                    if not company_id or not name:
                        raise ValueError("Company ID and name are required")
                    db.execute("INSERT INTO companies VALUES(?,?)", (company_id, name))
                elif action == "user":
                    fields = [
                        form[k].strip()
                        for k in ("username", "given_name", "family_name", "email")
                    ]
                    if not all(fields):
                        raise ValueError("All user fields are required")
                    if form.get("id"):
                        db.execute(
                            "UPDATE users SET username=?,given_name=?,family_name=?,"
                            "email=?, revision=revision+1 WHERE id=?",
                            (*fields, form["id"]),
                        )
                    else:
                        db.execute(
                            "INSERT INTO users(id,company_id,username,given_name,"
                            "family_name,email) VALUES(?,?,?,?,?,?)",
                            (str(uuid.uuid4()), form["company_id"], *fields),
                        )
                elif action == "active":
                    db.execute(
                        "UPDATE users SET active=1-active, "
                        "revision=revision+1 WHERE id=?",
                        (form["id"],),
                    )
                    db.execute(
                        "UPDATE groups SET revision=revision+1 WHERE id IN "
                        "(SELECT group_id FROM memberships WHERE user_id=?)",
                        (form["id"],),
                    )
                elif action == "membership":
                    user = db.execute(
                        "SELECT * FROM users WHERE id=?", (form["user_id"],)
                    ).fetchone()
                    group = db.execute(
                        "SELECT * FROM groups WHERE id=?", (form["group_id"],)
                    ).fetchone()
                    if (
                        not user
                        or not group
                        or user["company_id"] != group["company_id"]
                    ):
                        raise ValueError(
                            "Membership requires a user and group in the same company"
                        )
                    if form["operation"] == "assign":
                        db.execute(
                            "INSERT OR IGNORE INTO memberships VALUES(?,?)",
                            (user["id"], group["id"]),
                        )
                    else:
                        db.execute(
                            "DELETE FROM memberships WHERE user_id=? AND group_id=?",
                            (user["id"], group["id"]),
                        )
                    db.execute(
                        "UPDATE groups SET revision=revision+1 WHERE id=?",
                        (group["id"],),
                    )
                elif action == "sync":
                    run_id = synchronize(db, form["mode"])
                    flash(
                        f"Sync run {run_id} finished. See its status and journal below."
                    )
                else:
                    abort(404)
                db.commit()
            except (ValueError, sqlite3.IntegrityError, SyncError) as exc:
                db.rollback()
                # Never render raw SQL errors (which can reflect sensitive form data).
                flash(
                    "Conflicting or invalid data"
                    if isinstance(exc, sqlite3.IntegrityError)
                    else str(exc)
                )
            finally:
                db.close()
        return redirect("/")

    return app


if __name__ == "__main__":
    create_app().run(host="127.0.0.1", port=5000, debug=False)
