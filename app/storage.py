"""复核记录持久化（SQLite）。

仅当全部静态校验通过、固定点求值完成后才写入记录；
校验失败的请求不会获得编号，也不会产生任何数据。
"""

from __future__ import annotations

import json
import sqlite3
import threading
from pathlib import Path
from typing import Any


class ReviewStore:
    def __init__(self, db_path: str | Path) -> None:
        path = Path(db_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(str(path), check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        with self._lock:
            self._conn.execute(
                """
                CREATE TABLE IF NOT EXISTS reviews (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    created_at TEXT NOT NULL DEFAULT (datetime('now')),
                    payload TEXT NOT NULL
                )
                """
            )
            self._conn.commit()

    def create(self, payload: dict[str, Any]) -> int:
        data = json.dumps(payload, ensure_ascii=False, sort_keys=True)
        with self._lock:
            cur = self._conn.execute(
                "INSERT INTO reviews (payload) VALUES (?)", (data,)
            )
            self._conn.commit()
            return int(cur.lastrowid)

    def get(self, review_id: int) -> dict[str, Any] | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT payload FROM reviews WHERE id = ?", (review_id,)
            ).fetchone()
        if row is None:
            return None
        return json.loads(row["payload"])

    def count(self) -> int:
        with self._lock:
            return int(
                self._conn.execute("SELECT COUNT(*) AS c FROM reviews").fetchone()["c"]
            )

    def close(self) -> None:
        with self._lock:
            self._conn.close()
