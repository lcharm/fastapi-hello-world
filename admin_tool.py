#!/usr/bin/env python3
"""用户管理工具 — 通过 SSH 连上服务器管理账号"""
import sys
from auth import init_db, create_user, delete_user, set_user_active, list_users


def print_usage():
    print("用法:")
    print("  python admin_tool.py add <username> <password>")
    print("  python admin_tool.py delete <username>")
    print("  python admin_tool.py disable <username>")
    print("  python admin_tool.py enable <username>")
    print("  python admin_tool.py list")


def main():
    init_db()

    if len(sys.argv) < 2:
        print_usage()
        sys.exit(1)

    command = sys.argv[1]

    if command == "add":
        if len(sys.argv) != 4:
            print("用法: python admin_tool.py add <username> <password>")
            sys.exit(1)
        ok, msg = create_user(sys.argv[2], sys.argv[3])
        print(msg)

    elif command == "delete":
        if len(sys.argv) != 3:
            print("用法: python admin_tool.py delete <username>")
            sys.exit(1)
        ok, msg = delete_user(sys.argv[2])
        print(msg)

    elif command == "disable":
        if len(sys.argv) != 3:
            print("用法: python admin_tool.py disable <username>")
            sys.exit(1)
        ok, msg = set_user_active(sys.argv[2], False)
        print(msg)

    elif command == "enable":
        if len(sys.argv) != 3:
            print("用法: python admin_tool.py enable <username>")
            sys.exit(1)
        ok, msg = set_user_active(sys.argv[2], True)
        print(msg)

    elif command == "list":
        users = list_users()
        if not users:
            print("(暂无用户)")
        for u in users:
            status = "启用" if u["is_active"] else "禁用"
            print(f"  [{u['id']}] {u['username']} — {status}")

    else:
        print(f"未知命令: {command}")
        print_usage()
        sys.exit(1)


if __name__ == "__main__":
    main()
