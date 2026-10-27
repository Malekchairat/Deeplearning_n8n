"""The insured-property database of the Climate agent (non-life, home insurance).

SQLite stands in for the PostgreSQL base of the concept: the agent only ever sees a read-only SELECT interface, so the backend can be
swapped without touching the agent. The demo portfolio is seeded deterministically at first use (CLIMATE_DB_PATH to relocate the file).
"""
import os
import re
import sqlite3
from pathlib import Path
from typing import Any

DB_PATH = Path(os.getenv("CLIMATE_DB_PATH") or Path(__file__).with_name("portfolio.db"))
MAX_ROWS = 200
SCHEMA = """CREATE TABLE insured_properties (
    id INTEGER PRIMARY KEY, city TEXT NOT NULL, country TEXT NOT NULL,
    latitude REAL NOT NULL, longitude REAL NOT NULL,
    property_type TEXT NOT NULL,            -- 'Habitation individuelle' | 'Habitation collective' | 'Résidence secondaire'
    sum_insured_keur REAL NOT NULL,         -- total insured value of the policy block, k EUR
    annual_premium_keur REAL NOT NULL,
    flood_cover INTEGER NOT NULL            -- 1 if the policies cover flood / natural catastrophes, 0 otherwise
)"""

# (city, latitude, longitude, weight): the size of the book in each city
CITIES = [
    ("Paris", 48.8566, 2.3522, 1.8), ("Lyon", 45.7640, 4.8357, 1.2), ("Marseille", 43.2965, 5.3698, 1.0),
    ("Toulouse", 43.6047, 1.4442, 0.9), ("Bordeaux", 44.8378, -0.5792, 0.9), ("Nantes", 47.2184, -1.5536, 0.8),
    ("Strasbourg", 48.5734, 7.7521, 0.6), ("Lille", 50.6292, 3.0573, 0.8), ("Nice", 43.7102, 7.2620, 0.7),
    ("Montpellier", 43.6108, 3.8767, 0.6), ("Rouen", 49.4432, 1.0993, 0.5), ("Orléans", 47.9029, 1.9093, 0.4),
    ("Grenoble", 45.1885, 5.7245, 0.5), ("Tours", 47.3941, 0.6848, 0.4), ("Brest", 48.3904, -4.4861, 0.4),
]
PREMIUM_RATE = 0.0012                          # premium / sum insured
SECONDARY_WITHOUT_COVER = {"Nice", "Marseille", "Brest", "Montpellier"}   # secondary homes sold without natural-catastrophe cover


def _seed(conn: sqlite3.Connection) -> None:
    conn.execute(SCHEMA)
    rows = []
    for city, lat, lon, w in CITIES:
        blocks = [("Habitation individuelle", 120_000 * w, 1), ("Habitation collective", 200_000 * w, 1)]
        if city in SECONDARY_WITHOUT_COVER:
            blocks.append(("Résidence secondaire", 45_000 * w, 0))
        rows += [(city, "FR", lat, lon, kind, round(si), round(si * PREMIUM_RATE, 1), cover) for kind, si, cover in blocks]
    conn.executemany("INSERT INTO insured_properties (city, country, latitude, longitude, property_type, sum_insured_keur, "
                     "annual_premium_keur, flood_cover) VALUES (?,?,?,?,?,?,?,?)", rows)


def ensure_db() -> None:
    if DB_PATH.exists():
        return
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(DB_PATH) as conn:
        _seed(conn)


def _connect(properties: list[dict[str, Any]] | None = None) -> sqlite3.Connection:
    """The seeded demo book (read-only file), or - when the client sends its own portfolio - a throwaway in-memory table with the same schema."""
    if properties is None:
        ensure_db()
        conn = sqlite3.connect(f"file:{DB_PATH.as_posix()}?mode=ro", uri=True)
    else:
        conn = sqlite3.connect(":memory:")
        conn.execute(SCHEMA)
        conn.executemany("INSERT INTO insured_properties (city, country, latitude, longitude, property_type, sum_insured_keur, annual_premium_keur, "
                         "flood_cover) VALUES (:city, :country, :latitude, :longitude, :property_type, :sum_insured_keur, :annual_premium_keur, :flood_cover)", properties)
    conn.row_factory = sqlite3.Row
    return conn


_FORBIDDEN = re.compile(r"--|/\*|\b(attach|pragma|insert|update|delete|drop|alter|create|replace|vacuum|detach)\b", re.I)


def query_portfolio(sql: str, properties: list[dict[str, Any]] | None = None) -> tuple[list[dict[str, Any]], bool]:
    """Runs ONE read-only SELECT (the connection itself is read-only too). Returns (rows, truncated). Raises ValueError otherwise."""
    statement = sql.strip().rstrip(";").strip()
    if not re.match(r"select\b", statement, re.I) or ";" in statement or _FORBIDDEN.search(statement):
        raise ValueError("Seule une requête SELECT unique est autorisée sur la table insured_properties.")
    try:
        with _connect(properties) as conn:
            fetched = conn.execute(statement).fetchmany(MAX_ROWS + 1)
    except sqlite3.Error as exc:
        raise ValueError(f"Requête SQL invalide : {exc}") from exc
    return [dict(r) for r in fetched[:MAX_ROWS]], len(fetched) > MAX_ROWS


def all_properties(properties: list[dict[str, Any]] | None = None) -> list[dict[str, Any]]:
    """The whole book, read by the code itself (the stress test never depends on the SQL the model happened to write)."""
    with _connect(properties) as conn:
        return [dict(r) for r in conn.execute("SELECT * FROM insured_properties ORDER BY id")]
