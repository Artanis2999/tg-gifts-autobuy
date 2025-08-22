import aiosqlite
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Iterable, Sequence

# Поддержка sqlite:///path.db
def _sqlite_path_from_url(url: str) -> str:
    if not url.startswith("sqlite:///"):
        raise ValueError("For quick start we use SQLite. Set DATABASE_URL like sqlite:///giftbot.db")
    return url.replace("sqlite:///", "", 1)

_SQLITE_PATH = None

async def init_db(database_url: str) -> None:
    global _SQLITE_PATH
    _SQLITE_PATH = _sqlite_path_from_url(database_url)
    Path(_SQLITE_PATH).parent.mkdir(parents=True, exist_ok=True)
    async with aiosqlite.connect(_SQLITE_PATH) as db:
        await db.executescript(
            """
            PRAGMA journal_mode=WAL;

            CREATE TABLE IF NOT EXISTS users(
              user_id     INTEGER PRIMARY KEY,
              username    TEXT,
              balance     INTEGER NOT NULL DEFAULT 0,  -- в Stars (целые)
              autobuy     INTEGER NOT NULL DEFAULT 0,  -- 0/1
              created_at  TEXT DEFAULT (datetime('now'))
            );

            /* Правила автоскупа для каждого пользователя */
            CREATE TABLE IF NOT EXISTS rules(
              user_id      INTEGER PRIMARY KEY,
              only_limited INTEGER NOT NULL DEFAULT 1,       -- 1 = покупать только лимитные
              min_price    INTEGER NOT NULL DEFAULT 0,       -- мин. цена ⭐
              max_price    INTEGER NOT NULL DEFAULT 1000000000, -- макс. цена ⭐
              updated_at   TEXT DEFAULT (datetime('now')),
              FOREIGN KEY(user_id) REFERENCES users(user_id) ON DELETE CASCADE
            );

            CREATE TABLE IF NOT EXISTS payments(
              id          INTEGER PRIMARY KEY AUTOINCREMENT,
              user_id     INTEGER NOT NULL,
              amount      INTEGER NOT NULL,
              payload     TEXT,
              ts          TEXT DEFAULT (datetime('now')),
              FOREIGN KEY(user_id) REFERENCES users(user_id)
            );

            /* Кэш каталога подарков (для диффа). Поля ограничены до нужного минимума */
            CREATE TABLE IF NOT EXISTS gifts_cache(
              gift_id     TEXT PRIMARY KEY,
              title       TEXT,
              price       INTEGER,
              added_at    TEXT DEFAULT (datetime('now'))
            );

            CREATE TABLE IF NOT EXISTS logs(
              id          INTEGER PRIMARY KEY AUTOINCREMENT,
              level       TEXT NOT NULL,
              message     TEXT NOT NULL,
              ts          TEXT DEFAULT (datetime('now'))
            );
            """
        )
        await db.commit()

@asynccontextmanager
async def _conn():
    if _SQLITE_PATH is None:
        raise RuntimeError("DB not initialized. Call init_db() first.")
    conn = await aiosqlite.connect(_SQLITE_PATH)
    try:
        conn.row_factory = aiosqlite.Row
        yield conn
    finally:
        await conn.close()

# ---------- Users ----------
async def ensure_user(user_id: int, username: str | None) -> None:
    async with _conn() as db:
        await db.execute(
            "INSERT OR IGNORE INTO users(user_id, username) VALUES(?, ?)",
            (user_id, username or ""),
        )
        if username:
            await db.execute("UPDATE users SET username=? WHERE user_id=?", (username, user_id))
        # создаём дефолтные правила, если их ещё нет
        await db.execute("INSERT OR IGNORE INTO rules(user_id) VALUES(?)", (user_id,))
        await db.commit()

async def get_balance(user_id: int) -> int:
    async with _conn() as db:
        cur = await db.execute("SELECT balance FROM users WHERE user_id=?", (user_id,))
        row = await cur.fetchone()
        return int(row["balance"]) if row else 0

async def add_balance(user_id: int, amount: int) -> None:
    async with _conn() as db:
        await db.execute(
            "UPDATE users SET balance = COALESCE(balance,0) + ? WHERE user_id=?",
            (amount, user_id)
        )
        await db.commit()

async def set_autobuy(user_id: int, enabled: bool) -> None:
    async with _conn() as db:
        await db.execute("UPDATE users SET autobuy=? WHERE user_id=?", (1 if enabled else 0, user_id))
        await db.commit()

async def is_autobuy(user_id: int) -> bool:
    async with _conn() as db:
        cur = await db.execute("SELECT autobuy FROM users WHERE user_id=?", (user_id,))
        row = await cur.fetchone()
        return bool(row and row["autobuy"])

async def autobuy_users_with_rules() -> Sequence[aiosqlite.Row]:
    """Пользователи с включённым автобаем + их правила (джоин)."""
    async with _conn() as db:
        cur = await db.execute(
            """
            SELECT u.user_id, u.balance,
                   r.only_limited, r.min_price, r.max_price
            FROM users u
            JOIN rules r ON r.user_id = u.user_id
            WHERE u.autobuy = 1
            """
        )
        return await cur.fetchall()

# ---------- Rules ----------
async def get_rules(user_id: int) -> dict:
    async with _conn() as db:
        cur = await db.execute(
            "SELECT only_limited, min_price, max_price FROM rules WHERE user_id=?",
            (user_id,)
        )
        row = await cur.fetchone()
        if not row:
            # создаём дефолт если нет
            await db.execute("INSERT OR IGNORE INTO rules(user_id) VALUES(?)", (user_id,))
            await db.commit()
            return {"only_limited": 1, "min_price": 0, "max_price": 1000000000}
        return {
            "only_limited": int(row["only_limited"]),
            "min_price": int(row["min_price"]),
            "max_price": int(row["max_price"]),
        }

async def set_only_limited(user_id: int, enabled: bool) -> None:
    async with _conn() as db:
        await db.execute(
            "UPDATE rules SET only_limited=?, updated_at=datetime('now') WHERE user_id=?",
            (1 if enabled else 0, user_id)
        )
        await db.commit()

async def set_price_range(user_id: int, min_price: int, max_price: int) -> None:
    if min_price < 0:
        min_price = 0
    if max_price < 0:
        max_price = 0
    if min_price > max_price:
        min_price, max_price = max_price, min_price
    async with _conn() as db:
        await db.execute(
            "UPDATE rules SET min_price=?, max_price=?, updated_at=datetime('now') WHERE user_id=?",
            (int(min_price), int(max_price), user_id)
        )
        await db.commit()

# ---------- Gifts cache / logs ----------
async def upsert_gifts_cache(items: Iterable[dict]) -> None:
    async with _conn() as db:
        for it in items:
            await db.execute(
                "INSERT OR REPLACE INTO gifts_cache(gift_id, title, price) VALUES(?,?,?)",
                (str(it["id"]), it.get("title", ""), int(it.get("price", 0))),
            )
        await db.commit()

async def known_gift_ids() -> set[str]:
    async with _conn() as db:
        cur = await db.execute("SELECT gift_id FROM gifts_cache")
        return {r["gift_id"] for r in await cur.fetchall()}

async def record_payment(user_id: int, amount: int, payload: str) -> None:
    async with _conn() as db:
        await db.execute(
            "INSERT INTO payments(user_id, amount, payload) VALUES(?,?,?)",
            (user_id, amount, payload)
        )
        await db.commit()

async def log(level: str, message: str) -> None:
    async with _conn() as db:
        await db.execute("INSERT INTO logs(level, message) VALUES(?,?)", (level.upper(), message))
        await db.commit()


