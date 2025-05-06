"""
Database operations for FileSync.

This module handles all database operations for both daemon and client,
providing a consistent interface for tracking file changes and sync state.
"""

import os
import time
import sqlite3
import logging
from typing import List, Dict, Any, Optional, Tuple

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
        self.execute('''
        CREATE TABLE IF NOT EXISTS files (
            path TEXT NOT NULL,
            base_dir TEXT NOT NULL,
            checksum TEXT,
            size INTEGER,
            modified_time INTEGER,
            is_deleted INTEGER DEFAULT 0,
            PRIMARY KEY (path, base_dir)
        )
        ''')
        
        self.execute('''
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
        ''')
        
        self.execute('''
        CREATE INDEX IF NOT EXISTS idx_changes_ack ON changes(acknowledged)
        ''')
        
        self.execute('''
        CREATE INDEX IF NOT EXISTS idx_files_base_dir ON files(base_dir)
        ''')
        
        self.commit()
    
    def record_change(self, operation: str, path: str, base_dir: str, 
                      checksum: Optional[str] = None, size: Optional[int] = None) -> int:
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
        if operation == 'delete':
            self.execute(
                "UPDATE files SET is_deleted = 1 WHERE path = ? AND base_dir = ?",
                (path, base_dir)
            )
        else:  # add or modify
            self.execute(
                """
                INSERT OR REPLACE INTO files 
                (path, base_dir, checksum, size, modified_time, is_deleted) 
                VALUES (?, ?, ?, ?, ?, 0)
                """,
                (path, base_dir, checksum, size, timestamp)
            )
        
        # Record the change
        cursor = self.execute(
            """
            INSERT INTO changes 
            (timestamp, operation, path, base_dir, checksum, size) 
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (timestamp, operation, path, base_dir, checksum, size)
        )
        
        self.commit()
        
        # Return the sequence number
        return cursor.lastrowid
    
    def get_changes_since(self, sequence: int = 0, limit: int = 1000) -> List[Dict[str, Any]]:
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
            (sequence, limit)
        )
        
        changes = []
        for row in cursor.fetchall():
            changes.append({
                'sequence': row[0],
                'timestamp': row[1],
                'operation': row[2],
                'path': row[3],
                'base_dir': row[4],
                'checksum': row[5],
                'size': row[6]
            })
        
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
        self.execute(
            "UPDATE changes SET acknowledged = 1 WHERE sequence <= ?",
            (sequence,)
        )
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
            (retention_timestamp,)
        )
        
        self.commit()
        return cursor.rowcount
    
    def get_file_info(self, base_dir: str, path: str) -> Optional[Dict[str, Any]]:
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
            (base_dir, path)
        )
        
        row = cursor.fetchone()
        if not row:
            return None
        
        return {
            'checksum': row[0],
            'size': row[1],
            'modified_time': row[2],
            'is_deleted': bool(row[3])
        }


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
        self.execute('''
        CREATE TABLE IF NOT EXISTS sync_state (
            id INTEGER PRIMARY KEY,
            last_sequence INTEGER DEFAULT 0,
            last_sync_time INTEGER
        )
        ''')
        
        # Initialize sync state if not present
        cursor = self.execute("SELECT COUNT(*) FROM sync_state")
        if cursor.fetchone()[0] == 0:
            self.execute(
                "INSERT INTO sync_state (last_sequence, last_sync_time) VALUES (0, ?)",
                (timestamp_now(),)
            )
        
        self.commit()
    
    def get_last_sequence(self) -> int:
        """
        Get the last processed sequence number.
        
        Returns:
            Last processed sequence number
        """
        cursor = self.execute("SELECT last_sequence FROM sync_state WHERE id = 1")
        return cursor.fetchone()[0]
    
    def update_last_sequence(self, sequence: int) -> None:
        """
        Update the last processed sequence number.
        
        Args:
            sequence: New sequence number
        """
        self.execute(
            "UPDATE sync_state SET last_sequence = ?, last_sync_time = ? WHERE id = 1",
            (sequence, timestamp_now())
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
