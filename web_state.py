"""Small, transactional state store shared by the web UI and scheduler."""

import json
import secrets
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

from werkzeug.security import generate_password_hash


def timestamp():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class StateStore:
    def __init__(self, directory, seed_directory):
        self.directory = Path(directory)
        self.directory.mkdir(parents=True, exist_ok=True)
        self.path = self.directory / "state.sqlite3"
        self.initial_password = None
        with self.connection() as conn:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("CREATE TABLE IF NOT EXISTS state (id INTEGER PRIMARY KEY CHECK(id=1), value TEXT NOT NULL)")
            conn.execute("BEGIN IMMEDIATE")
            if conn.execute("SELECT 1 FROM state WHERE id=1").fetchone() is None:
                def lines(filename):
                    content = (Path(seed_directory) / filename).read_text(encoding="utf-8-sig")
                    return list(dict.fromkeys(line.strip() for line in content.splitlines() if line.strip()))

                self.initial_password = secrets.token_urlsafe(18)
                state = {
                    "authors": sorted(lines("important_people.txt"), key=str.casefold),
                    "keywords": sorted(lines("keywords.txt"), key=str.casefold),
                    "suggestions": [],
                    "next_suggestion_id": 1,
                    "preview": None,
                    "last_message": None,
                    "baseline_ids": [],
                    "schedule_slots": {},
                    "job": None,
                    "password_hash": generate_password_hash(self.initial_password),
                    "session_secret": secrets.token_hex(32),
                }
                conn.execute("INSERT INTO state (id, value) VALUES (1, ?)", (json.dumps(state),))
            conn.commit()
        self.path.chmod(0o600)

    @contextmanager
    def connection(self):
        conn = sqlite3.connect(self.path, timeout=15)
        try:
            yield conn
        finally:
            conn.close()

    def read(self):
        with self.connection() as conn:
            return json.loads(conn.execute("SELECT value FROM state WHERE id=1").fetchone()[0])

    def update(self, change):
        with self.connection() as conn:
            conn.execute("BEGIN IMMEDIATE")
            state = json.loads(conn.execute("SELECT value FROM state WHERE id=1").fetchone()[0])
            result = change(state)
            conn.execute("UPDATE state SET value=? WHERE id=1", (json.dumps(state, ensure_ascii=False),))
            conn.commit()
            return result

    def suggest(self, kind, value):
        if kind not in ("authors", "keywords"):
            raise ValueError("Choose an author or a keyword.")
        if not isinstance(value, str) or any(ord(char) < 32 for char in value):
            raise ValueError("Enter one suggestion on a single line.")
        value = " ".join(value.split())
        if not value or len(value) > 160:
            raise ValueError("Suggestions must contain 1–160 characters.")
        if kind == "authors" and (len(value.split()) < 2 or value.startswith("(")):
            raise ValueError("Enter the author's first and last name.")

        def add(state):
            if value.casefold() in {entry.casefold() for entry in state[kind]}:
                raise ValueError("That entry is already on the watchlist.")
            if any(item["kind"] == kind and item["value"].casefold() == value.casefold()
                   for item in state["suggestions"]):
                raise ValueError("That suggestion is already awaiting review.")
            if len(state["suggestions"]) >= 500:
                raise ValueError("The review queue is full. Please try again later.")
            state["suggestions"].append({
                "id": state["next_suggestion_id"], "kind": kind,
                "value": value, "created_at": timestamp(),
            })
            state["next_suggestion_id"] += 1

        self.update(add)

    def review(self, suggestion_id, accept):
        def change(state):
            item = next((item for item in state["suggestions"] if item["id"] == suggestion_id), None)
            if item is None:
                raise ValueError("This suggestion has already been reviewed.")
            if accept and item["value"].casefold() not in {value.casefold() for value in state[item["kind"]]}:
                state[item["kind"]].append(item["value"])
                state[item["kind"]].sort(key=str.casefold)
            state["suggestions"].remove(item)

        self.update(change)

    def reset_password(self):
        password = secrets.token_urlsafe(18)
        password_hash = generate_password_hash(password)

        def change(state):
            state["password_hash"] = password_hash
            state["session_secret"] = secrets.token_hex(32)

        self.update(change)
        return password
