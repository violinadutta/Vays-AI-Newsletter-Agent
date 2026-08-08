#!/usr/bin/env python
"""Create an application user from the command line.

The first account has to be created here — there is no self-service signup, by
design: this is an internal tool with a known, small set of users.

Usage::

    python -m scripts.create_user                          # interactive
    python -m scripts.create_user --username priya --role admin
    python -m scripts.create_user --username bot --password ... --role editor

Passwords are never taken as plain command-line arguments unless you pass
``--password`` explicitly, because arguments land in shell history and in the
process list. Interactive entry via ``getpass`` is the default.
"""

from __future__ import annotations

import argparse
import getpass
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from config import configure_logging  # noqa: E402
from core.enums import UserRole  # noqa: E402
from core.exceptions import NewsletterAppError  # noqa: E402
from modules.repository.database import init_database  # noqa: E402
from services.auth_service import AuthService  # noqa: E402


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Create a Vays Newsletter Platform user.")
    parser.add_argument("--username")
    parser.add_argument("--display-name", help="Defaults to the username")
    parser.add_argument("--email")
    parser.add_argument(
        "--password",
        help="Prompted for securely if omitted (recommended — arguments leak into shell history)",
    )
    parser.add_argument(
        "--role",
        choices=[role.value for role in UserRole],
        default=UserRole.ADMIN.value,
        help="Defaults to admin, since this is normally used for the first account",
    )
    return parser.parse_args()


def prompt(label: str, current: str | None) -> str:
    while current is None or not current.strip():
        current = input(f"{label}: ").strip()
    return current


def main() -> int:
    args = parse_args()
    configure_logging("WARNING")  # keep the CLI output readable

    username = prompt("Username", args.username)
    display_name = args.display_name or username
    email = prompt("Email", args.email)

    password = args.password
    if password is None:
        password = getpass.getpass("Password: ")
        if password != getpass.getpass("Confirm password: "):
            print("Passwords do not match.")
            return 1

    try:
        init_database()
        created = AuthService().create_user(
            username=username,
            display_name=display_name,
            email=email,
            password=password,
            role=UserRole(args.role),
        )
    except NewsletterAppError as exc:
        # A domain error means the user did something fixable; show the friendly
        # message rather than a traceback.
        print(f"\n{exc.user_message}")
        return 1
    except Exception as exc:  # noqa: BLE001
        print(f"\nUnexpected error: {exc}")
        print("If the database does not exist yet, run: alembic upgrade head")
        return 1

    print(f"\nCreated user '{created}' with role '{args.role}'.")
    print("Start the app with:  streamlit run app.py")
    return 0


if __name__ == "__main__":
    sys.exit(main())
