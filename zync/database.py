"""
Database operations for FileSync.

This module handles all database operations for both daemon and client,
providing a consistent interface for tracking file changes and sync state.
"""

import logging
import sqlite3
import time
from typing import Any

from .common import timestamp_now

logger = logging.getLogger(__name__)


class Database:
    """Base database class with common functionality."""

    def __init__(self, db_path: str):
        """
        Initialize database connection.

        Args:
            db_path: Path to SQLite database file
        """
        self.db_path = db_path
        self.conn = None
        self._connect()

    def _connect(self) -> None:
        """Establish database connection."""
        try:
            self.conn = sqlite3.connect(self.db_path)
            # Enable foreign keys
            self.conn.execute("PRAGMA foreign_keys = ON")
            # For better performance
            self.conn.execute("PRAGMA journal_mode = WAL")
        except Exception as e:
            logger.error(f"Error connecting to database: {e}")
            raise

    def close(self) -> None:
        """Close database connection."""
        if self.conn:
            self.conn.close()
            self.conn = None

    def execute(self, query: str, params: tuple = ()) -> sqlite3.Cursor:
        """
        Execute a SQL query.

        Args:
            query: SQL query string
            params: Query parameters

        Returns:
            SQLite cursor
        """
        try:
            if not self.conn:
                self._connect()
            return self.conn.execute(query, params)
        except Exception as e:
            logger.error(f"Database error executing {query}: {e}")
            raise

    def commit(self) -> None:
        """Commit current transaction."""
        if self.conn:
            self.conn.commit()


class DaemonDatabase(Database):
    """Database operations for the daemon."""

    def __init__(self, db_path: str):
        """
        Initialize daemon database.

        Args:
            db_path: Path to SQLite database file
        """
        super().__init__(db_path)
        self._create_tables()

    def _create_tables(self) -> None:
        """Create necessary tables if they don't exist."""
        self.execute("""
        CREATE TABLE IF NOT EXISTS files (
            path TEXT NOT NULL,
            base_dir TEXT NOT NULL,
            checksum TEXT,
            size INTEGER,
            modified_time INTEGER,
            is_deleted INTEGER DEFAULT 0,
            PRIMARY KEY (path, base_dir)
        )
        """)

        self.execute("""
        CREATE TABLE IF NOT EXISTS changes (
            sequence INTEGER PRIMARY KEY AUTOINCREMENT,
            timestamp INTEGER NOT NULL,
            operation TEXT NOT NULL,
            path TEXT NOT NULL,
            base_dir TEXT NOT NULL,
            checksum TEXT,
            size INTEGER,
            acknowledged INTEGER DEFAULT 0
        )
        """)

        self.execute("""
        CREATE INDEX IF NOT EXISTS idx_changes_ack ON changes(acknowledged)
        """)

        self.execute("""
        CREATE INDEX IF NOT EXISTS idx_files_base_dir ON files(base_dir)
        """)

        self.execute("""
        CREATE TABLE IF NOT EXISTS file_changes (
            sequence INTEGER PRIMARY KEY AUTOINCREMENT,
            base_dir TEXT NOT NULL,
            path TEXT NOT NULL,
            operation TEXT NOT NULL,
            timestamp TEXT NOT NULL,
            checksum TEXT,
            size INTEGER,
            acknowledged INTEGER DEFAULT 0
        )
        """)

        self.execute("""
        CREATE TABLE IF NOT EXISTS client_acknowledgments (
            client_id TEXT NOT NULL,
            last_sequence INTEGER NOT NULL,
            timestamp TEXT NOT NULL,
            PRIMARY KEY (client_id)
        )
        """)

        self.execute("""
        CREATE TABLE IF NOT EXISTS staging_sessions (
            staging_id TEXT PRIMARY KEY,
            status TEXT NOT NULL,
            expected_file_count INTEGER NOT NULL,
            actual_file_count INTEGER DEFAULT 0,
            conflict_count INTEGER DEFAULT 0,
            created_timestamp TEXT NOT NULL,
            updated_timestamp TEXT NOT NULL
        )
        """)

        self.execute("""
        CREATE TABLE IF NOT EXISTS staged_files (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            staging_id TEXT NOT NULL,
            base_dir TEXT NOT NULL,
            path TEXT NOT NULL,
            checksum TEXT NOT NULL,
            size INTEGER NOT NULL,
            has_conflict INTEGER DEFAULT 0,
            FOREIGN KEY (staging_id) REFERENCES staging_sessions(staging_id),
            UNIQUE (staging_id, base_dir, path)
        )
        """)

        self.execute("""
        CREATE TABLE IF NOT EXISTS applied_sessions (
            staging_id TEXT PRIMARY KEY,
            applied_timestamp TEXT NOT NULL,
            applied_file_count INTEGER NOT NULL,
            skipped_file_count INTEGER DEFAULT 0,
            FOREIGN KEY (staging_id) REFERENCES staging_sessions(staging_id)
        )
        """)

        self.commit()

    def record_change(
        self, operation: str, path: str, base_dir: str, checksum: str | None = None, size: int | None = None
    ) -> int:
        """
        Record a file change in the database.

        Args:
            operation: Operation type ('add', 'modify', 'delete')
            path: Relative file path
            base_dir: Base directory identifier
            checksum: File checksum (for add/modify)
            size: File size in bytes (for add/modify)

        Returns:
            Sequence number of the recorded change
        """
        timestamp = timestamp_now()

        # Update the files table
        if operation == "delete":
            self.execute("UPDATE files SET is_deleted = 1 WHERE path = ? AND base_dir = ?", (path, base_dir))
        else:  # add or modify
            self.execute(
                """
                INSERT OR REPLACE INTO files
                (path, base_dir, checksum, size, modified_time, is_deleted)
                VALUES (?, ?, ?, ?, ?, 0)
                """,
                (path, base_dir, checksum, size, timestamp),
            )

        # Record the change
        cursor = self.execute(
            """
            INSERT INTO changes
            (timestamp, operation, path, base_dir, checksum, size)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (timestamp, operation, path, base_dir, checksum, size),
        )

        self.commit()

        # Return the sequence number
        return cursor.lastrowid

    def get_changes_since(self, sequence: int = 0, limit: int = 1000) -> list[dict[str, Any]]:
        """
        Get changes since a specific sequence number.

        Args:
            sequence: Starting sequence number (exclusive)
            limit: Maximum number of changes to return

        Returns:
            List of change dictionaries
        """
        cursor = self.execute(
            """
            SELECT sequence, timestamp, operation, path, base_dir, checksum, size
            FROM changes
            WHERE sequence > ?
            ORDER BY sequence
            LIMIT ?
            """,
            (sequence, limit),
        )

        changes = []
        for row in cursor.fetchall():
            changes.append(
                {
                    "sequence": row[0],
                    "timestamp": row[1],
                    "operation": row[2],
                    "path": row[3],
                    "base_dir": row[4],
                    "checksum": row[5],
                    "size": row[6],
                }
            )

        return changes

    def get_last_sequence(self) -> int:
        """
        Get the most recent sequence number.

        Returns:
            Latest sequence number, or 0 if no changes
        """
        cursor = self.execute("SELECT MAX(sequence) FROM changes")
        result = cursor.fetchone()[0]
        return result if result is not None else 0

    def acknowledge_changes(self, sequence: int) -> None:
        """
        Mark changes as acknowledged up to a sequence number.

        Args:
            sequence: Maximum sequence number to acknowledge
        """
        self.execute("UPDATE changes SET acknowledged = 1 WHERE sequence <= ?", (sequence,))
        self.commit()

    def compact_changes(self, retention_days: int = 7) -> int:
        """
        Remove old acknowledged changes to save space.

        Args:
            retention_days: Days to keep acknowledged changes

        Returns:
            Number of deleted records
        """
        retention_timestamp = timestamp_now() - (retention_days * 86400)

        cursor = self.execute(
            """
            DELETE FROM changes
            WHERE acknowledged = 1 AND timestamp < ?
            """,
            (retention_timestamp,),
        )

        self.commit()
        return cursor.rowcount

    def get_file_info(self, base_dir: str, path: str) -> dict[str, Any] | None:
        """
        Get stored information about a file.

        Args:
            base_dir: Base directory identifier
            path: Relative file path

        Returns:
            File information dictionary, or None if not found
        """
        cursor = self.execute(
            """
            SELECT checksum, size, modified_time, is_deleted
            FROM files
            WHERE base_dir = ? AND path = ?
            """,
            (base_dir, path),
        )

        row = cursor.fetchone()
        if not row:
            return None

        return {"checksum": row[0], "size": row[1], "modified_time": row[2], "is_deleted": bool(row[3])}

    def create_staging_session(self, staging_id: str, expected_file_count: int) -> None:
        """
        Create a new staging session for upstream changes.

        Args:
            staging_id: Unique identifier for this staging session
            expected_file_count: Expected number of files to be staged
        """
        now = time.strftime("%Y-%m-%d %H:%M:%S")
        cursor = self.execute("SELECT COUNT(*) FROM staging_sessions")
        if cursor.fetchone()[0] == 0:
            self.execute(
                "INSERT INTO staging_sessions (staging_id, status, expected_file_count, created_timestamp, updated_timestamp) VALUES (?, ?, ?, ?, ?)",
                (staging_id, "in_progress", expected_file_count, now, now),
            )
        else:
            self.execute(
                "UPDATE staging_sessions SET status = ?, expected_file_count = ?, updated_timestamp = ? WHERE staging_id = ?",
                ("in_progress", expected_file_count, now, staging_id),
            )
        self.commit()

    def add_staged_file(
        self, staging_id: str, base_dir: str, path: str, checksum: str, size: int, has_conflict: bool
    ) -> None:
        """
        Add a file to a staging session.

        Args:
            staging_id: Unique identifier for the staging session
            base_dir: Base directory identifier
            path: File path relative to base directory
            checksum: MD5 checksum of the file
            size: Size of the file in bytes
            has_conflict: Whether this file conflicts with local changes
        """
        cursor = self.execute("SELECT COUNT(*) FROM staged_files")
        if cursor.fetchone()[0] == 0:
            self.execute(
                "INSERT INTO staged_files (staging_id, base_dir, path, checksum, size, has_conflict) VALUES (?, ?, ?, ?, ?, ?)",
                (staging_id, base_dir, path, checksum, size, 1 if has_conflict else 0),
            )
        else:
            self.execute(
                "UPDATE staged_files SET base_dir = ?, path = ?, checksum = ?, size = ?, has_conflict = ? WHERE staging_id = ?",
                (base_dir, path, checksum, size, 1 if has_conflict else 0, staging_id),
            )
        self.commit()

    def complete_staging_session(self, staging_id: str, file_count: int, conflict_count: int) -> None:
        """
        Mark a staging session as complete.

        Args:
            staging_id: Unique identifier for the staging session
            file_count: Actual number of files staged
            conflict_count: Number of files with conflicts
        """
        now = time.strftime("%Y-%m-%d %H:%M:%S")
        cursor = self.execute("SELECT COUNT(*) FROM staged_files")
        if cursor.fetchone()[0] == 0:
            self.execute(
                "INSERT INTO staged_files (staging_id, base_dir, path, checksum, size, has_conflict) VALUES (?, ?, ?, ?, ?, ?)",
                (staging_id, "complete", 0, 0, file_count, conflict_count),
            )
        else:
            self.execute(
                "UPDATE staged_files SET status = ?, actual_file_count = ?, conflict_count = ?, updated_timestamp = ? WHERE staging_id = ?",
                ("complete", file_count, conflict_count, now, staging_id),
            )
        self.commit()

    def abort_staging_session(self, staging_id: str) -> None:
        """
        Mark a staging session as aborted.

        Args:
            staging_id: Unique identifier for the staging session
        """
        now = time.strftime("%Y-%m-%d %H:%M:%S")
        cursor = self.execute("SELECT COUNT(*) FROM staged_files")
        if cursor.fetchone()[0] == 0:
            self.execute(
                "INSERT INTO staged_files (staging_id, base_dir, path, checksum, size, has_conflict) VALUES (?, ?, ?, ?, ?, ?)",
                (staging_id, "aborted", 0, 0, 0, 0),
            )
        else:
            self.execute(
                "UPDATE staged_files SET status = ?, updated_timestamp = ? WHERE staging_id = ?",
                ("aborted", now, staging_id),
            )
        self.commit()

    def get_staging_sessions(self, status: str | None = None) -> list[dict[str, Any]]:
        """
        Get all staging sessions, optionally filtered by status.

        Args:
            status: Optional status filter (in_progress, complete, aborted, applied)

        Returns:
            List of staging session data dictionaries
        """
        cursor = self.execute(
            "SELECT staging_id, status, expected_file_count, actual_file_count, conflict_count, created_timestamp, updated_timestamp FROM staging_sessions"
        )

        sessions = []
        for row in cursor.fetchall():
            sessions.append(
                {
                    "staging_id": row[0],
                    "status": row[1],
                    "expected_file_count": row[2],
                    "actual_file_count": row[3],
                    "conflict_count": row[4],
                    "created_timestamp": row[5],
                    "updated_timestamp": row[6],
                }
            )

        return sessions

    def get_staged_files(self, staging_id: str) -> list[dict[str, Any]]:
        """
        Get all files in a staging session.

        Args:
            staging_id: Unique identifier for the staging session

        Returns:
            List of staged file data dictionaries
        """
        cursor = self.execute(
            "SELECT id, base_dir, path, checksum, size, has_conflict FROM staged_files WHERE staging_id = ?",
            (staging_id,),
        )

        files = []
        for row in cursor.fetchall():
            files.append(
                {
                    "id": row[0],
                    "base_dir": row[1],
                    "path": row[2],
                    "checksum": row[3],
                    "size": row[4],
                    "has_conflict": bool(row[5]),
                }
            )

        return files

    def mark_session_applied(self, staging_id: str, applied_count: int, skipped_count: int) -> None:
        """
        Mark a staging session as applied.

        Args:
            staging_id: Unique identifier for the staging session
            applied_count: Number of files applied
            skipped_count: Number of files skipped
        """
        now = time.strftime("%Y-%m-%d %H:%M:%S")
        cursor = self.execute("SELECT COUNT(*) FROM staged_files")
        if cursor.fetchone()[0] == 0:
            self.execute(
                "INSERT INTO staged_files (staging_id, base_dir, path, checksum, size, has_conflict) VALUES (?, ?, ?, ?, ?, ?)",
                (staging_id, "applied", 0, 0, applied_count, skipped_count),
            )
        else:
            self.execute(
                "UPDATE staged_files SET status = ?, updated_timestamp = ? WHERE staging_id = ?",
                ("applied", now, staging_id),
            )
        self.commit()

    def get_last_file_state(self, base_dir: str, path: str) -> dict[str, Any] | None:
        """
        Get the last known state of a file.

        Args:
            base_dir: Base directory identifier
            path: File path relative to base directory

        Returns:
            Dictionary with file metadata or None if not found
        """
        cursor = self.execute(
            "SELECT operation, checksum, size FROM file_changes WHERE base_dir = ? AND path = ? ORDER BY sequence DESC LIMIT 1",
            (base_dir, path),
        )

        row = cursor.fetchone()
        if row and row[0] != "delete":
            return {"operation": row[0], "checksum": row[1], "size": row[2]}

        return None


class ClientDatabase(Database):
    """Database operations for the client."""

    def __init__(self, db_path: str):
        """
        Initialize client database.

        Args:
            db_path: Path to SQLite database file
        """
        super().__init__(db_path)
        self._create_tables()

    def _create_tables(self) -> None:
        """Create necessary tables if they don't exist."""
        self.execute("""
        CREATE TABLE IF NOT EXISTS sync_state (
            id INTEGER PRIMARY KEY,
            last_sequence INTEGER DEFAULT 0,
            last_sync_time INTEGER
        )
        """)

        # Initialize sync state if not present
        cursor = self.execute("SELECT COUNT(*) FROM sync_state")
        if cursor.fetchone()[0] == 0:
            self.execute("INSERT INTO sync_state (last_sequence, last_sync_time) VALUES (0, ?)", (timestamp_now(),))

        self.commit()

    def get_last_sequence(self) -> int:
        """
        Get the last processed sequence number.

        Returns:
            The last sequence number processed by the client
        """
        cursor = self.execute("SELECT last_sequence FROM sync_state WHERE id = 1")
        return cursor.fetchone()[0]

    def get_known_files(self, base_dir: str) -> dict:
        """
        Get the current known state of files in a base directory.

        Args:
            base_dir: Base directory identifier

        Returns:
            Dictionary mapping relative paths to file metadata (checksum, size)
        """
        cursor = self.execute(
            "SELECT path, operation, checksum, size FROM file_state WHERE base_dir = ? ORDER BY sequence DESC",
            (base_dir,),
        )

        files = {}
        seen_paths = set()

        # For each file, we only care about the most recent operation
        for path, operation, checksum, size in cursor.fetchall():
            if path in seen_paths:
                continue

            seen_paths.add(path)

            if operation == "delete":
                # If the most recent operation is delete, don't include this file
                continue

            files[path] = {"checksum": checksum, "size": size}

        return files

    def update_last_sequence(self, sequence: int) -> None:
        """
        Update the last processed sequence number.

        Args:
            sequence: New sequence number
        """
        self.execute(
            "UPDATE sync_state SET last_sequence = ?, last_sync_time = ? WHERE id = 1", (sequence, timestamp_now())
        )
        self.commit()

    def get_last_sync_time(self) -> int:
        """
        Get the timestamp of the last sync.

        Returns:
            Timestamp of last sync
        """
        cursor = self.execute("SELECT last_sync_time FROM sync_state WHERE id = 1")
        return cursor.fetchone()[0]
