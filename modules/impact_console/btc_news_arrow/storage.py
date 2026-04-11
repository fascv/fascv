from __future__ import annotations

import json
import sqlite3
from datetime import timedelta
from difflib import SequenceMatcher
from pathlib import Path

from btc_news_arrow.models import NewsItem
from btc_news_arrow.utils import canonicalize_url, ensure_utc, hash_text, normalize_text, parse_datetime, utcnow


class Storage:
    def __init__(self, db_path: str | Path = "btc_news_arrow.db") -> None:
        self.db_path = Path(db_path)
        self.conn = sqlite3.connect(self.db_path)
        self.conn.row_factory = sqlite3.Row
        self.init_schema()

    def close(self) -> None:
        self.conn.close()

    def init_schema(self) -> None:
        self.conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS items (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp_utc TEXT NOT NULL,
                source TEXT NOT NULL,
                title TEXT NOT NULL,
                summary TEXT NOT NULL,
                url TEXT,
                guid TEXT,
                title_norm TEXT NOT NULL,
                title_hash TEXT NOT NULL,
                category TEXT NOT NULL DEFAULT 'other',
                polarity INTEGER NOT NULL DEFAULT 0,
                impact REAL NOT NULL DEFAULT 0,
                raw_json TEXT NOT NULL DEFAULT '{}',
                created_at TEXT NOT NULL
            );

            CREATE INDEX IF NOT EXISTS idx_items_ts ON items(timestamp_utc);
            CREATE INDEX IF NOT EXISTS idx_items_title_hash ON items(title_hash);
            CREATE UNIQUE INDEX IF NOT EXISTS idx_items_guid_unique ON items(guid) WHERE guid IS NOT NULL;
            CREATE UNIQUE INDEX IF NOT EXISTS idx_items_url_unique ON items(url) WHERE url IS NOT NULL;

            CREATE TABLE IF NOT EXISTS price_points (
                ts_minute INTEGER PRIMARY KEY,
                price REAL NOT NULL,
                source TEXT NOT NULL DEFAULT 'binance',
                created_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS item_labels (
                item_id INTEGER NOT NULL,
                horizon_minutes INTEGER NOT NULL,
                return_value REAL NOT NULL,
                labeled_at TEXT NOT NULL,
                PRIMARY KEY (item_id, horizon_minutes)
            );

            CREATE INDEX IF NOT EXISTS idx_item_labels_horizon ON item_labels(horizon_minutes);

            CREATE TABLE IF NOT EXISTS feature_stats (
                feature_key TEXT NOT NULL,
                horizon_minutes INTEGER NOT NULL,
                n INTEGER NOT NULL,
                mean_return REAL NOT NULL,
                updated_at TEXT NOT NULL,
                PRIMARY KEY (feature_key, horizon_minutes)
            );
            """
        )
        self.conn.commit()

    def exists_guid_or_url(self, guid: str | None, url: str | None) -> bool:
        if guid:
            row = self.conn.execute("SELECT 1 FROM items WHERE guid = ? LIMIT 1", (guid,)).fetchone()
            if row:
                return True
        canonical_url = canonicalize_url(url)
        if canonical_url:
            row = self.conn.execute("SELECT 1 FROM items WHERE url = ? LIMIT 1", (canonical_url,)).fetchone()
            if row:
                return True
        return False

    def has_similar_title(self, title: str, lookback_hours: int, threshold: float) -> bool:
        title_norm = normalize_text(title)
        if not title_norm:
            return False
        since = ensure_utc(utcnow() - timedelta(hours=lookback_hours)).isoformat()
        rows = self._candidate_titles_for_similarity(title_norm=title_norm, since_iso=since, limit=400)
        for row in rows:
            ratio = SequenceMatcher(a=title_norm, b=row["title_norm"]).ratio()
            if ratio >= threshold:
                return True
        return False

    def _candidate_titles_for_similarity(
        self,
        title_norm: str,
        since_iso: str,
        limit: int,
    ) -> list[sqlite3.Row]:
        # Two-stage dedupe prefilter:
        # 1) constrain by timestamp and rough title length
        # 2) require at least one meaningful token overlap when available
        token_candidates = [t for t in title_norm.split() if len(t) >= 4][:3]
        title_len = len(title_norm)
        min_len = max(8, int(title_len * 0.6))
        max_len = max(min_len, int(title_len * 1.4) + 8)

        params: list[object] = [since_iso, min_len, max_len]
        token_sql = ""
        if token_candidates:
            token_sql = " AND (" + " OR ".join("instr(title_norm, ?) > 0" for _ in token_candidates) + ")"
            params.extend(token_candidates)

        params.append(int(limit))
        query = (
            "SELECT title_norm FROM items "
            "WHERE timestamp_utc >= ? "
            "AND length(title_norm) BETWEEN ? AND ?"
            f"{token_sql} "
            "ORDER BY timestamp_utc DESC "
            "LIMIT ?"
        )
        rows = self.conn.execute(query, tuple(params)).fetchall()
        if rows or not token_candidates:
            return rows

        # Fallback without token filter to avoid false negatives.
        return self.conn.execute(
            """
            SELECT title_norm
            FROM items
            WHERE timestamp_utc >= ?
              AND length(title_norm) BETWEEN ? AND ?
            ORDER BY timestamp_utc DESC
            LIMIT ?
            """,
            (since_iso, min_len, max_len, int(limit)),
        ).fetchall()

    def insert_items(self, items: list[NewsItem]) -> int:
        inserted = 0
        now_iso = utcnow().isoformat()
        for item in items:
            title_norm = normalize_text(item.title)
            payload = (
                ensure_utc(item.timestamp_utc).isoformat(),
                item.source,
                item.title,
                item.summary,
                canonicalize_url(item.url),
                item.guid,
                title_norm,
                hash_text(title_norm),
                item.category,
                item.polarity,
                float(item.impact),
                json.dumps(item.raw, ensure_ascii=True),
                now_iso,
            )
            cur = self.conn.execute(
                """
                INSERT OR IGNORE INTO items (
                    timestamp_utc, source, title, summary, url, guid,
                    title_norm, title_hash, category, polarity, impact,
                    raw_json, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                payload,
            )
            if cur.rowcount > 0:
                inserted += 1

        self.conn.commit()
        return inserted

    def _row_to_item(self, row: sqlite3.Row) -> NewsItem:
        ts = parse_datetime(row["timestamp_utc"]) or utcnow()
        raw_json = row["raw_json"]
        raw = {}
        if raw_json:
            try:
                raw = json.loads(raw_json)
            except json.JSONDecodeError:
                raw = {}
        if not isinstance(raw, dict):
            raw = {}
        raw["_db_id"] = int(row["id"])
        raw["_db_created_at"] = row["created_at"]

        return NewsItem(
            timestamp_utc=ts,
            source=row["source"],
            title=row["title"],
            summary=row["summary"],
            url=row["url"],
            guid=row["guid"],
            category=row["category"],
            polarity=int(row["polarity"]),
            impact=float(row["impact"]),
            raw=raw,
        )

    def get_items_between(self, start_ts, end_ts) -> list[NewsItem]:
        start_iso = ensure_utc(start_ts).isoformat()
        end_iso = ensure_utc(end_ts).isoformat()
        rows = self.conn.execute(
            """
            SELECT *
            FROM items
            WHERE timestamp_utc BETWEEN ? AND ?
            ORDER BY timestamp_utc ASC
            """,
            (start_iso, end_iso),
        ).fetchall()
        return [self._row_to_item(r) for r in rows]

    def get_items_since(self, since_ts) -> list[NewsItem]:
        since_iso = ensure_utc(since_ts).isoformat()
        rows = self.conn.execute(
            """
            SELECT *
            FROM items
            WHERE timestamp_utc >= ?
            ORDER BY timestamp_utc DESC
            """,
            (since_iso,),
        ).fetchall()
        return [self._row_to_item(r) for r in rows]

    def get_items_with_ids(self) -> list[tuple[int, NewsItem]]:
        rows = self.conn.execute(
            """
            SELECT *
            FROM items
            ORDER BY id ASC
            """
        ).fetchall()
        return [(int(r["id"]), self._row_to_item(r)) for r in rows]

    def update_item_analysis(self, updates: list[tuple[int, str, int, float]]) -> None:
        self.conn.executemany(
            """
            UPDATE items
            SET category = ?, polarity = ?, impact = ?
            WHERE id = ?
            """,
            [(category, int(polarity), float(impact), int(item_id)) for item_id, category, polarity, impact in updates],
        )
        self.conn.commit()

    def get_unlabeled_items(self, horizon_minutes: int, cutoff_ts, limit: int) -> list[tuple[int, NewsItem]]:
        cutoff_iso = ensure_utc(cutoff_ts).isoformat()
        rows = self.conn.execute(
            """
            SELECT i.*
            FROM items i
            LEFT JOIN item_labels l
              ON l.item_id = i.id AND l.horizon_minutes = ?
            WHERE l.item_id IS NULL
              AND i.timestamp_utc <= ?
            ORDER BY i.timestamp_utc DESC
            LIMIT ?
            """,
            (int(horizon_minutes), cutoff_iso, int(limit)),
        ).fetchall()
        return [(int(r["id"]), self._row_to_item(r)) for r in rows]

    def get_labeled_items(self, horizon_minutes: int, since_ts, limit: int | None = None) -> list[tuple[int, NewsItem, float]]:
        since_iso = ensure_utc(since_ts).isoformat()
        params: list[object] = [int(horizon_minutes), since_iso]
        limit_sql = ""
        if limit is not None:
            limit_sql = " LIMIT ?"
            params.append(int(limit))

        rows = self.conn.execute(
            f"""
            SELECT i.*, l.return_value
            FROM item_labels l
            INNER JOIN items i ON i.id = l.item_id
            WHERE l.horizon_minutes = ?
              AND i.timestamp_utc >= ?
            ORDER BY i.timestamp_utc ASC
            {limit_sql}
            """,
            tuple(params),
        ).fetchall()
        out: list[tuple[int, NewsItem, float]] = []
        for row in rows:
            out.append((int(row["id"]), self._row_to_item(row), float(row["return_value"])))
        return out

    def upsert_price_points(self, points: dict[int, float], source: str = "binance") -> None:
        if not points:
            return
        now_iso = utcnow().isoformat()
        self.conn.executemany(
            """
            INSERT INTO price_points(ts_minute, price, source, created_at)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(ts_minute) DO UPDATE SET
              price = excluded.price,
              source = excluded.source,
              created_at = excluded.created_at
            """,
            [(int(ts_m), float(price), source, now_iso) for ts_m, price in points.items()],
        )
        self.conn.commit()

    def get_price_points_range(self, start_minute: int, end_minute: int) -> dict[int, float]:
        rows = self.conn.execute(
            """
            SELECT ts_minute, price
            FROM price_points
            WHERE ts_minute BETWEEN ? AND ?
            """,
            (int(start_minute), int(end_minute)),
        ).fetchall()
        return {int(r["ts_minute"]): float(r["price"]) for r in rows}

    def upsert_item_label(self, item_id: int, horizon_minutes: int, return_value: float) -> None:
        self.upsert_item_labels(
            [(int(item_id), int(horizon_minutes), float(return_value))],
        )

    def upsert_item_labels(self, labels: list[tuple[int, int, float]]) -> None:
        if not labels:
            return
        now_iso = utcnow().isoformat()
        self.conn.executemany(
            """
            INSERT INTO item_labels(item_id, horizon_minutes, return_value, labeled_at)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(item_id, horizon_minutes) DO UPDATE SET
              return_value = excluded.return_value,
              labeled_at = excluded.labeled_at
            """,
            [(int(item_id), int(horizon_minutes), float(return_value), now_iso) for item_id, horizon_minutes, return_value in labels],
        )
        self.conn.commit()

    def apply_feature_updates(self, updates: dict[tuple[str, int], tuple[int, float]]) -> None:
        now_iso = utcnow().isoformat()
        for (feature_key, horizon_minutes), (count, sum_returns) in updates.items():
            if count <= 0:
                continue

            row = self.conn.execute(
                """
                SELECT n, mean_return
                FROM feature_stats
                WHERE feature_key = ? AND horizon_minutes = ?
                """,
                (feature_key, int(horizon_minutes)),
            ).fetchone()

            if row is None:
                new_n = int(count)
                new_mean = float(sum_returns) / float(count)
                self.conn.execute(
                    """
                    INSERT INTO feature_stats(feature_key, horizon_minutes, n, mean_return, updated_at)
                    VALUES (?, ?, ?, ?, ?)
                    """,
                    (feature_key, int(horizon_minutes), new_n, new_mean, now_iso),
                )
                continue

            old_n = int(row["n"])
            old_mean = float(row["mean_return"])
            new_n = old_n + int(count)
            new_mean = (old_mean * old_n + float(sum_returns)) / new_n
            self.conn.execute(
                """
                UPDATE feature_stats
                SET n = ?, mean_return = ?, updated_at = ?
                WHERE feature_key = ? AND horizon_minutes = ?
                """,
                (new_n, new_mean, now_iso, feature_key, int(horizon_minutes)),
            )

        self.conn.commit()

    def get_feature_stats(self, feature_keys: list[str], horizon_minutes: int) -> dict[str, tuple[int, float]]:
        if not feature_keys:
            return {}
        placeholders = ",".join("?" for _ in feature_keys)
        params: list[object] = list(feature_keys)
        params.append(int(horizon_minutes))
        rows = self.conn.execute(
            f"""
            SELECT feature_key, n, mean_return
            FROM feature_stats
            WHERE feature_key IN ({placeholders})
              AND horizon_minutes = ?
            """,
            tuple(params),
        ).fetchall()
        return {str(r["feature_key"]): (int(r["n"]), float(r["mean_return"])) for r in rows}

    def get_feature_stats_with_meta(
        self,
        feature_keys: list[str],
        horizon_minutes: int,
    ) -> dict[str, tuple[int, float, str]]:
        if not feature_keys:
            return {}
        placeholders = ",".join("?" for _ in feature_keys)
        params: list[object] = list(feature_keys)
        params.append(int(horizon_minutes))
        rows = self.conn.execute(
            f"""
            SELECT feature_key, n, mean_return, updated_at
            FROM feature_stats
            WHERE feature_key IN ({placeholders})
              AND horizon_minutes = ?
            """,
            tuple(params),
        ).fetchall()
        return {
            str(r["feature_key"]): (int(r["n"]), float(r["mean_return"]), str(r["updated_at"]))
            for r in rows
        }
