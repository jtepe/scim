import argparse

from scim_client.db import ROOT, connect
from scim_client.web import create_app


def main():
    parser = argparse.ArgumentParser(description="Local SCIM provisioning client")
    parser.add_argument(
        "command", choices=["serve", "init", "reset"], nargs="?", default="serve"
    )
    parser.add_argument(
        "--yes", action="store_true", help="Confirm destructive local reset"
    )
    args = parser.parse_args()
    if args.command == "reset" and not args.yes:
        parser.error(
            "Reset deletes local data and credentials. Stop the server and pass --yes."
        )
    db = connect()
    try:
        if args.command == "reset":
            db.executescript((ROOT / "sql/seed.sql").read_text())
            print("Local database reset. Service provider unchanged.")
        elif args.command == "serve":
            db.execute("UPDATE runs SET status='interrupted' WHERE status='running'")
            db.commit()
    finally:
        db.close()
    if args.command == "serve":
        create_app().run(host="127.0.0.1", port=5000, debug=False)


if __name__ == "__main__":
    main()
