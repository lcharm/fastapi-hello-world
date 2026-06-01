import os
import sqlite3
import logging
from datetime import datetime, timedelta, timezone

import jwt
import bcrypt

logger = logging.getLogger(__name__)

DB_PATH = os.getenv("DB_PATH", "./users.db")
JWT_SECRET = os.getenv("JWT_SECRET")
JWT_ALGORITHM = "HS256"
JWT_EXPIRY_DAYS = 30

if JWT_SECRET is None:
    raise RuntimeError("环境变量 JWT_SECRET 未设置，请配置一个长随机字符串后重启服务")


def hash_password(password: str) -> str:
    return bcrypt.hashpw(password.encode("utf-8"), bcrypt.gensalt()).decode("utf-8")


def verify_password(plain: str, hashed: str) -> bool:
    return bcrypt.checkpw(plain.encode("utf-8"), hashed.encode("utf-8"))


def _get_conn():
    conn = sqlite3.connect(DB_PATH, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    conn = _get_conn()
    conn.execute("""
        CREATE TABLE IF NOT EXISTS users (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            username        TEXT NOT NULL UNIQUE,
            hashed_password TEXT NOT NULL,
            is_active       INTEGER NOT NULL DEFAULT 1
        )
    """)
    conn.commit()
    conn.close()
    logger.info("用户数据库已初始化")


def create_jwt(username: str) -> str:
    now = datetime.now(timezone.utc)
    payload = {
        "sub": username,
        "iat": now,
        "exp": now + timedelta(days=JWT_EXPIRY_DAYS),
    }
    return jwt.encode(payload, JWT_SECRET, algorithm=JWT_ALGORITHM)


def decode_jwt(token: str) -> str | None:
    try:
        payload = jwt.decode(token, JWT_SECRET, algorithms=[JWT_ALGORITHM])
        return payload.get("sub")
    except jwt.ExpiredSignatureError:
        logger.info("JWT 已过期")
        return None
    except jwt.InvalidTokenError:
        return None


def is_user_active(username: str) -> bool:
    conn = _get_conn()
    row = conn.execute(
        "SELECT is_active FROM users WHERE username = ?", (username,)
    ).fetchone()
    conn.close()
    if row is None:
        return False
    return bool(row["is_active"])


def authenticate_user(username: str, password: str) -> str | None:
    conn = _get_conn()
    row = conn.execute(
        "SELECT hashed_password, is_active FROM users WHERE username = ?",
        (username,),
    ).fetchone()
    conn.close()

    if row is None:
        return None
    if not row["is_active"]:
        logger.info(f"用户 {username} 已被禁用，拒绝登录")
        return None
    if not verify_password(password, row["hashed_password"]):
        return None

    return create_jwt(username)


# ========== 用户管理（供 admin_tool 调用） ==========

def create_user(username: str, password: str) -> tuple[bool, str]:
    conn = _get_conn()
    try:
        conn.execute(
            "INSERT INTO users (username, hashed_password) VALUES (?, ?)",
            (username, hash_password(password)),
        )
        conn.commit()
        return True, f"用户 {username} 创建成功"
    except sqlite3.IntegrityError:
        return False, f"用户 {username} 已存在"
    finally:
        conn.close()


def delete_user(username: str) -> tuple[bool, str]:
    conn = _get_conn()
    cursor = conn.execute("DELETE FROM users WHERE username = ?", (username,))
    conn.commit()
    deleted = cursor.rowcount
    conn.close()
    if deleted:
        return True, f"用户 {username} 已删除"
    return False, f"用户 {username} 不存在"


def set_user_active(username: str, active: bool) -> tuple[bool, str]:
    conn = _get_conn()
    cursor = conn.execute(
        "UPDATE users SET is_active = ? WHERE username = ?",
        (int(active), username),
    )
    conn.commit()
    updated = cursor.rowcount
    conn.close()
    if updated:
        status = "启用" if active else "禁用"
        return True, f"用户 {username} 已{status}"
    return False, f"用户 {username} 不存在"


def list_users() -> list[dict]:
    conn = _get_conn()
    rows = conn.execute(
        "SELECT id, username, is_active FROM users ORDER BY id"
    ).fetchall()
    conn.close()
    return [{"id": r["id"], "username": r["username"], "is_active": bool(r["is_active"])} for r in rows]


def get_user(username: str) -> dict | None:
    conn = _get_conn()
    row = conn.execute(
        "SELECT id, username, is_active, hashed_password FROM users WHERE username = ?",
        (username,),
    ).fetchone()
    conn.close()
    if row is None:
        return None
    return {
        "id": row["id"],
        "username": row["username"],
        "is_active": bool(row["is_active"]),
        "hashed_password": row["hashed_password"],
    }
