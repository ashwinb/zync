"""
Daemon for file synchronization from machine E to machine R.

This module implements a WebSocket server that:
1. Monitors specified directories for file changes
2. Records changes in a SQLite database
3. Serves changes to clients on request
"""

import argparse
import asyncio
import json
import logging
import os
import time

import websockets
from watchdog.events import FileSystemEventHandler
from watchdog.observers import Observer

from .common import (
    calculate_checksum,
    get_file_info,
    get_relative_path,
    is_subpath,
    timestamp_now,
)
from .database import DaemonDatabase

START_TIME = 0

# Configure logging
logger = logging.getLogger(__name__)

DEFAULT_IGNORE_PATTERNS = [
    ".git",
    ".DS_Store",
    "__pycache__",
    "*.pyc",
    "*.pyo",
    ".idea",
    ".vscode",
    "node_modules",
    "build",
    "dist",
    "*.swp",  # Vim swap files
    "*.swo",  # Vim swap files
    "*~",  # Backup files
]


class FileChangeHandler(FileSystemEventHandler):
    """Watchdog handler to detect file system changes"""

    def __init__(self, db: DaemonDatabase, base_dirs: dict[str, str]):
        """
        Initialize the file change handler.

        Args:
            db: Database instance for recording changes
            base_dirs: Mapping of base_dir names to absolute paths
        """
        self.db = db
        self.base_dirs = {os.path.abspath(v): k for k, v in base_dirs.items()}
        self.ignore_patterns = DEFAULT_IGNORE_PATTERNS

    def _is_ignored(self, path: str) -> bool:
        """Check if the path should be ignored."""
        path_parts = os.path.normpath(path).split(os.sep)
        for pattern in self.ignore_patterns:
            if pattern.startswith("*."):  # Glob pattern like *.pyc
                if path_parts[-1].endswith(pattern[1:]):
                    return True
            elif pattern.endswith("~") and pattern.startswith("*"):  # Glob pattern like *~
                if path_parts[-1].endswith(pattern[1:]):
                    return True
            elif pattern in path_parts:  # Directory or specific file name in path
                return True
        return False

    def _get_base_dir(self, path: str) -> tuple[str, str] | None:
        """
        Determine which base directory a path belongs to.

        Args:
            path: Absolute path to check

        Returns:
            Tuple of (base_dir_name, base_dir_path) or None if not in any base dir
        """
        path = os.path.abspath(path)
        for base_path, base_name in self.base_dirs.items():
            if is_subpath(base_path, path):
                return (base_name, base_path)
        return None

    def on_created(self, event):
        """Handle file creation event"""
        if event.is_directory:
            return

        path = event.src_path
        if self._is_ignored(path):
            logger.debug(f"Ignored create event for: {path}")
            return

        base_info = self._get_base_dir(path)
        if not base_info:
            return

        base_name, base_path = base_info
        rel_path = get_relative_path(base_path, path)

        # Get file information
        file_info = get_file_info(path)
        checksum = file_info["checksum"]
        size = file_info["size"]

        # Record the change
        sequence = self.db.record_change("add", rel_path, base_name, checksum, size)
        logger.info(f"Created: {rel_path} in {base_name} (seq: {sequence})")

    def on_deleted(self, event):
        """Handle file deletion event"""
        if event.is_directory:
            return

        path = event.src_path
        if self._is_ignored(path):
            logger.debug(f"Ignored delete event for: {path}")
            return

        base_info = self._get_base_dir(path)
        if not base_info:
            return

        base_name, base_path = base_info
        rel_path = get_relative_path(base_path, path)

        # Record the change
        sequence = self.db.record_change("delete", rel_path, base_name)
        logger.info(f"Deleted: {rel_path} in {base_name} (seq: {sequence})")

    def on_modified(self, event):
        """Handle file modification event"""
        if event.is_directory:
            return

        path = event.src_path
        if self._is_ignored(path):
            logger.debug(f"Ignored modify event for: {path}")
            return

        base_info = self._get_base_dir(path)
        if not base_info:
            return

        base_name, base_path = base_info
        rel_path = get_relative_path(base_path, path)

        # Get file information
        file_info = get_file_info(path)
        checksum = file_info["checksum"]
        size = file_info["size"]

        # Record the change
        sequence = self.db.record_change("modify", rel_path, base_name, checksum, size)
        logger.info(f"Modified: {rel_path} in {base_name} (seq: {sequence})")

    def on_moved(self, event):
        """Handle file move event"""
        if event.is_directory:
            return

        # Handle source deletion
        src_path = event.src_path
        dest_path = event.dest_path

        src_ignored = self._is_ignored(src_path)
        dest_ignored = self._is_ignored(dest_path)

        if src_ignored and dest_ignored:
            logger.debug(f"Ignored move event for source {src_path} and destination {dest_path}")
            return

        if not src_ignored:
            src_base_info = self._get_base_dir(src_path)
            if src_base_info:
                src_base_name, src_base_path = src_base_info
                src_rel_path = get_relative_path(src_base_path, src_path)
                sequence = self.db.record_change("delete", src_rel_path, src_base_name)
                logger.info(f"Move (deleted): {src_rel_path} in {src_base_name} (seq: {sequence})")
        else:
            logger.debug(f"Ignored move source (delete part) for: {src_path}")

        # Handle destination creation
        if not dest_ignored:
            dest_base_info = self._get_base_dir(dest_path)
            if dest_base_info:
                dest_base_name, dest_base_path = dest_base_info
                dest_rel_path = get_relative_path(dest_base_path, dest_path)

                # Get file information
                file_info = get_file_info(dest_path)
                checksum = file_info["checksum"]
                size = file_info["size"]

                sequence = self.db.record_change("add", dest_rel_path, dest_base_name, checksum, size)
                logger.info(f"Move (added): {dest_rel_path} in {dest_base_name} (seq: {sequence})")
        else:
            logger.debug(f"Ignored move destination (add part) for: {dest_path}")


async def handle_client(websocket, base_dirs: dict[str, str], db: DaemonDatabase):
    """
    Handle a client WebSocket connection.

    Args:
        websocket: WebSocket connection
        base_dirs: Mapping of base_dir names to absolute paths
        db: Database instance
    """
    try:
        async for message in websocket:
            try:
                request = json.loads(message)
                operation = request.get("operation")
                response = {"status": "error", "error": "Unknown operation"}

                if operation == "PING":
                    response = {"status": "ok", "server_time": timestamp_now(), "uptime": int(time.time() - START_TIME)}

                elif operation == "GET_LAST_SEQUENCE":
                    last_sequence = db.get_last_sequence()
                    response = {"status": "ok", "last_sequence": last_sequence}

                elif operation == "GET_CHANGES":
                    since_sequence = request.get("since_sequence", 0)
                    limit = request.get("limit", 1000)
                    changes = db.get_changes_since(since_sequence, limit)

                    response = {
                        "status": "ok",
                        "changes": changes,
                        "more_available": len(changes) >= limit,
                        "highest_sequence": db.get_last_sequence(),
                    }

                elif operation == "GET_FILE":
                    base_dir = request.get("base_dir")
                    path = request.get("path")

                    if not base_dir or not path:
                        response = {"status": "error", "error": "Missing parameters"}
                    elif base_dir not in base_dirs:
                        response = {"status": "error", "error": "Invalid base directory"}
                    else:
                        try:
                            full_path = os.path.join(base_dirs[base_dir], path)

                            # Security check to prevent path traversal
                            norm_path = os.path.normpath(full_path)
                            if not is_subpath(base_dirs[base_dir], norm_path):
                                response = {"status": "error", "error": "Invalid path"}
                            else:
                                with open(full_path, "rb") as f:
                                    file_data = f.read()

                                checksum = calculate_checksum(full_path)

                                # Send metadata response
                                meta_response = {"status": "ok", "checksum": checksum, "size": len(file_data)}
                                await websocket.send(json.dumps(meta_response))

                                # Send binary data
                                await websocket.send(file_data)

                                # Skip the normal response
                                continue
                        except FileNotFoundError:
                            response = {"status": "error", "error": "File not found"}
                        except Exception as e:
                            response = {"status": "error", "error": str(e)}

                elif operation == "ACK_CHANGES":
                    sequence = request.get("sequence_number")
                    if sequence is not None:
                        db.acknowledge_changes(sequence)
                        response = {"status": "ok"}
                    else:
                        response = {"status": "error", "error": "Missing sequence_number"}

                await websocket.send(json.dumps(response))

            except json.JSONDecodeError:
                await websocket.send(json.dumps({"status": "error", "error": "Invalid JSON"}))
            except Exception as e:
                logger.error(f"Error handling message: {str(e)}")
                await websocket.send(json.dumps({"status": "error", "error": str(e)}))

    except websockets.exceptions.ConnectionClosed:
        logger.info("Client disconnected")


async def run_server(host: str, port: int, base_dirs: dict[str, str], db: DaemonDatabase):
    """
    Run the WebSocket server.

    Args:
        host: Hostname to bind to
        port: Port to listen on
        base_dirs: Mapping of base_dir names to absolute paths
        db: Database instance
    """
    async with websockets.serve(lambda ws: handle_client(ws, base_dirs, db), host, port):
        logger.info(f"Server started on {host}:{port}")
        logger.info(f"Monitoring directories: {', '.join(f'{k}: {v}' for k, v in base_dirs.items())}")
        await asyncio.Future()  # Run forever


def validate_directories(dir_specs: list[str]) -> dict[str, str]:
    """
    Validate directory specifications and return mapping.

    Args:
        dir_specs: List of directory specifications (name:path)

    Returns:
        Mapping of directory names to absolute paths
    """
    base_dirs = {}
    for dir_spec in dir_specs:
        try:
            name, path = dir_spec.split(":", 1)
            abs_path = os.path.abspath(path)
            if not os.path.isdir(abs_path):
                logger.error(f"Directory not found: {path}")
                continue
            base_dirs[name] = abs_path
        except ValueError:
            logger.error(f"Invalid directory specification: {dir_spec}, use format name:path")

    return base_dirs


def periodic_maintenance(db: DaemonDatabase, interval: int = 3600):
    """
    Perform periodic database maintenance.

    Args:
        db: Database instance
        interval: Maintenance interval in seconds
    """
    while True:
        time.sleep(interval)
        try:
            deleted_count = db.compact_changes()
            if deleted_count > 0:
                logger.info(f"Maintenance: removed {deleted_count} old change records")
        except Exception as e:
            logger.error(f"Error during maintenance: {e}")


def main():
    """Main entry point for the daemon"""
    parser = argparse.ArgumentParser(description="File Sync Daemon (Machine E)")
    parser.add_argument("--host", default="localhost", help="Hostname to bind to")
    parser.add_argument("--port", type=int, default=8000, help="Port to listen on")
    parser.add_argument("--db", default="sync.db", help="SQLite database path")
    parser.add_argument("--dirs", required=True, nargs="+", help="Directories to monitor (format: name:path)")
    parser.add_argument(
        "--log-level", default="INFO", choices=["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"], help="Logging level"
    )
    args = parser.parse_args()

    # Configure logging
    logging.basicConfig(
        level=getattr(logging, args.log_level),
        format="%(asctime)s - %(levelname)s - %(message)s",
        handlers=[logging.StreamHandler(), logging.FileHandler("zync_daemon.log")],
    )

    # Validate directories
    base_dirs = validate_directories(args.dirs)

    if not base_dirs:
        logger.error("No valid directories to monitor")
        return 1

    # Initialize the database
    db_path = os.path.abspath(args.db)
    db = DaemonDatabase(db_path)

    # Set up file system watchers
    event_handler = FileChangeHandler(db, base_dirs)
    observer = Observer()

    for base_path in base_dirs:
        observer.schedule(event_handler, base_path, recursive=True)

    observer.start()
    logger.info("File monitoring started")

    # Start maintenance thread
    import threading

    maintenance_thread = threading.Thread(target=periodic_maintenance, args=(db,), daemon=True)
    maintenance_thread.start()

    # Set global start time
    global START_TIME
    START_TIME = time.time()

    try:
        # Run the WebSocket server
        asyncio.run(run_server(args.host, args.port, base_dirs, db))
    except KeyboardInterrupt:
        logger.info("Stopping daemon")
        observer.stop()
        db.close()

    observer.join()
    return 0


if __name__ == "__main__":
    import sys

    sys.exit(main())
