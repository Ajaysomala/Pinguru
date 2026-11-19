#!/usr/bin/env python3

import argparse
import getpass
import os
import sys
from datetime import datetime, timezone
from urllib.parse import urlparse


ADMIN_EMAIL = "aj@pinguru.me"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Reset the PinGuru admin password in MongoDB."
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Apply the reset. Without this flag, run a dry run.",
    )
    parser.add_argument(
        "--database",
        help="MongoDB database name (defaults to DB_NAME, MONGODB_DB_NAME, or the URI database)",
    )
    return parser.parse_args()


def uri_database_name(uri: str) -> str | None:
    parsed = urlparse(uri)
    if parsed.path and parsed.path != "/":
        return parsed.path.lstrip("/").split("/", 1)[0]
    return None


def main() -> int:
    args = parse_args()

    uri = os.environ.get("MONGODB_URI")
    if not uri:
        raise SystemExit("MONGODB_URI is required")

    try:
        from passlib.context import CryptContext
        from pymongo import MongoClient
    except ImportError as exc:
        raise SystemExit(
            f"Missing dependency: {exc.name}. Install backend requirements first."
        ) from exc

    password = getpass.getpass("New admin password: ")
    if len(password) < 8:
        raise SystemExit("Password must be at least 8 characters")

    pwd_ctx = CryptContext(schemes=["bcrypt"], deprecated="auto")
    password_hash = pwd_ctx.hash(password)

    database_name = (
        args.database
        or os.environ.get("DB_NAME")
        or os.environ.get("MONGODB_DB_NAME")
        or uri_database_name(uri)
        or "pinguru"
    )

    client = MongoClient(uri, serverSelectionTimeoutMS=10000)
    try:
        client.admin.command("ping")
        collection = client[database_name]["admin_config"]

        credentials = collection.find_one(
            {"_id": "admin_credentials"},
            {"password_hash": 1},
        )
        session_doc = collection.find_one(
            {"_id": "admin_session"},
            {"admin_session_version": 1},
        )
        current_version = int((session_doc or {}).get("admin_session_version", 0) or 0)
        next_version = current_version + 1

        if not args.apply:
            credential_state = "configured" if credentials else "missing"
            print(
                f"DRY RUN: would reset the separate admin credential for {ADMIN_EMAIL} "
                f"in {database_name}.admin_config"
            )
            print(
                f"Credential document: {credential_state}; "
                f"admin_session_version {current_version} -> {next_version}"
            )
            return 0

        now = datetime.now(timezone.utc)
        collection.update_one(
            {"_id": "admin_credentials"},
            {
                "$set": {
                    "password_hash": password_hash,
                    "updated_at": now,
                }
            },
            upsert=True,
        )
        collection.update_one(
            {"_id": "admin_session"},
            {"$set": {"admin_session_version": next_version}},
            upsert=True,
        )
        print(
            f"Updated the separate admin credential for {ADMIN_EMAIL}; "
            f"admin_session_version is now {next_version}"
        )
        return 0
    finally:
        client.close()


if __name__ == "__main__":
    raise SystemExit(main())
