from __future__ import annotations

import argparse
import getpass
from pathlib import Path

from .config import Settings
from .service import Service
from .security import AppError


def main():
    parser = argparse.ArgumentParser(description="知行工作台服务器管理")
    commands = parser.add_subparsers(dest="command", required=True)
    init = commands.add_parser("init-admin", help="Create an administrator; never prints the password")
    init.add_argument("--username", default="admin")
    init.add_argument("--display-name", default="管理员")
    init.add_argument("--password-file", type=Path)
    args = parser.parse_args()
    service = Service(Settings())
    try:
        if args.command == "init-admin":
            password = args.password_file.read_text().strip() if args.password_file else getpass.getpass("管理员密码（至少 10 位）: ")
            user = service.create_user({"username": args.username, "display_name": args.display_name, "password": password, "role": "admin"})
            print(f"Administrator created: {user['username']} (id={user['id']})")
    except AppError as exc:
        parser.exit(1, exc.message + "\n")
    finally:
        service.db.close()


if __name__ == "__main__":
    main()
