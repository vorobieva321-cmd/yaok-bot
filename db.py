import aiosqlite
import os

DB_PATH = os.path.join(os.path.dirname(__file__), "bot_data.db")

FREE_MESSAGE_LIMIT = 6


async def init_db():
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("""
            CREATE TABLE IF NOT EXISTS users (
                user_id INTEGER PRIMARY KEY,
                username TEXT,
                first_name TEXT,
                message_count INTEGER DEFAULT 0,
                is_premium INTEGER DEFAULT 0,
                current_topic TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
        # Safe migrations — add columns if they don't exist
        for migration in [
            "ALTER TABLE users ADD COLUMN last_reminded_at TIMESTAMP",
            "ALTER TABLE users ADD COLUMN reminders_enabled INTEGER DEFAULT 1",
            "ALTER TABLE users ADD COLUMN payment_charge_id TEXT",
        ]:
            try:
                await db.execute(migration)
                await db.commit()
            except Exception:
                pass  # Column already exists
        await db.execute("""
            CREATE TABLE IF NOT EXISTS messages (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER,
                role TEXT,
                content TEXT,
                topic TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (user_id) REFERENCES users(user_id)
            )
        """)
        await db.commit()


async def get_or_create_user(user_id: int, username: str = None, first_name: str = None):
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            "SELECT user_id, message_count, is_premium, current_topic FROM users WHERE user_id = ?",
            (user_id,)
        ) as cursor:
            row = await cursor.fetchone()
        if row is None:
            await db.execute(
                "INSERT INTO users (user_id, username, first_name) VALUES (?, ?, ?)",
                (user_id, username, first_name)
            )
            await db.commit()
            return {"user_id": user_id, "message_count": 0, "is_premium": False, "current_topic": None}
        return {
            "user_id": row[0],
            "message_count": row[1],
            "is_premium": bool(row[2]),
            "current_topic": row[3],
        }


async def increment_message_count(user_id: int) -> int:
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "UPDATE users SET message_count = message_count + 1 WHERE user_id = ?",
            (user_id,)
        )
        await db.commit()
        async with db.execute(
            "SELECT message_count FROM users WHERE user_id = ?", (user_id,)
        ) as cursor:
            row = await cursor.fetchone()
            return row[0] if row else 0


async def set_topic(user_id: int, topic: str):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "UPDATE users SET current_topic = ? WHERE user_id = ?",
            (topic, user_id)
        )
        await db.commit()


async def set_premium(user_id: int, is_premium: bool):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "UPDATE users SET is_premium = ? WHERE user_id = ?",
            (1 if is_premium else 0, user_id)
        )
        await db.commit()


async def save_message(user_id: int, role: str, content: str, topic: str = None):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "INSERT INTO messages (user_id, role, content, topic) VALUES (?, ?, ?, ?)",
            (user_id, role, content, topic)
        )
        await db.commit()


async def get_conversation_history(user_id: int, topic: str, limit: int = 10):
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            """SELECT role, content FROM messages
               WHERE user_id = ? AND topic = ?
               ORDER BY created_at DESC LIMIT ?""",
            (user_id, topic, limit)
        ) as cursor:
            rows = await cursor.fetchall()
    rows.reverse()
    return [{"role": row[0], "content": row[1]} for row in rows]


async def clear_topic_history(user_id: int, topic: str):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "DELETE FROM messages WHERE user_id = ? AND topic = ?",
            (user_id, topic)
        )
        await db.commit()


async def get_stats() -> dict:
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute("SELECT COUNT(*) FROM users") as cursor:
            total_users = (await cursor.fetchone())[0]

        async with db.execute("SELECT COUNT(*) FROM users WHERE is_premium = 1") as cursor:
            premium_users = (await cursor.fetchone())[0]

        async with db.execute(
            "SELECT COUNT(*) FROM users WHERE created_at >= datetime('now', '-1 day')"
        ) as cursor:
            new_today = (await cursor.fetchone())[0]

        async with db.execute(
            "SELECT COUNT(DISTINCT user_id) FROM messages WHERE created_at >= datetime('now', '-1 day')"
        ) as cursor:
            active_today = (await cursor.fetchone())[0]

        async with db.execute(
            "SELECT COUNT(*) FROM messages WHERE created_at >= datetime('now', '-1 day')"
        ) as cursor:
            messages_today = (await cursor.fetchone())[0]

        async with db.execute(
            "SELECT COUNT(*) FROM messages"
        ) as cursor:
            total_messages = (await cursor.fetchone())[0]

        async with db.execute(
            """SELECT topic, COUNT(*) as cnt FROM messages
               WHERE role = 'user' AND topic IS NOT NULL
               GROUP BY topic ORDER BY cnt DESC"""
        ) as cursor:
            topic_rows = await cursor.fetchall()

    return {
        "total_users": total_users,
        "premium_users": premium_users,
        "free_users": total_users - premium_users,
        "new_today": new_today,
        "active_today": active_today,
        "messages_today": messages_today,
        "total_messages": total_messages,
        "topics": {row[0]: row[1] for row in topic_rows},
    }


async def get_users_to_remind(inactive_days: int = 3, min_remind_gap_days: int = 7) -> list[dict]:
    """Return users who opted in, had activity, went quiet, and haven't been reminded recently."""
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            f"""SELECT u.user_id, u.first_name, u.current_topic
                FROM users u
                WHERE (u.reminders_enabled IS NULL OR u.reminders_enabled = 1)
                AND (
                    SELECT MAX(m.created_at) FROM messages m WHERE m.user_id = u.user_id
                ) < datetime('now', '-{inactive_days} days')
                AND (
                    SELECT COUNT(*) FROM messages m WHERE m.user_id = u.user_id
                ) > 0
                AND (
                    u.last_reminded_at IS NULL
                    OR u.last_reminded_at < datetime('now', '-{min_remind_gap_days} days')
                )"""
        ) as cursor:
            rows = await cursor.fetchall()
    return [{"user_id": row[0], "first_name": row[1], "current_topic": row[2]} for row in rows]


async def toggle_reminders(user_id: int) -> bool:
    """Toggle reminders for user. Returns the new state (True = enabled)."""
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            "SELECT reminders_enabled FROM users WHERE user_id = ?", (user_id,)
        ) as cursor:
            row = await cursor.fetchone()
        current = row[0] if row and row[0] is not None else 1
        new_state = 0 if current else 1
        await db.execute(
            "UPDATE users SET reminders_enabled = ? WHERE user_id = ?",
            (new_state, user_id)
        )
        await db.commit()
    return bool(new_state)


async def save_payment_charge(user_id: int, charge_id: str):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "UPDATE users SET payment_charge_id = ? WHERE user_id = ?",
            (charge_id, user_id)
        )
        await db.commit()


async def get_payment_charge(user_id: int) -> str | None:
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            "SELECT payment_charge_id FROM users WHERE user_id = ?", (user_id,)
        ) as cursor:
            row = await cursor.fetchone()
    return row[0] if row else None


async def mark_reminded(user_id: int):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "UPDATE users SET last_reminded_at = CURRENT_TIMESTAMP WHERE user_id = ?",
            (user_id,)
        )
        await db.commit()


async def get_all_user_ids() -> list[int]:
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute("SELECT user_id FROM users") as cursor:
            rows = await cursor.fetchall()
    return [row[0] for row in rows]
