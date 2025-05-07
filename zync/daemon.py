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
import uuid
from datetime import UTC, datetime

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
    # Store current upstream session info
    upstream_session = {"active": False, "files": {}, "staging_dir": None, "staging_id": None}

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

                # --- New upstream operations ---

                elif operation == "PREPARE_UPSTREAM":
                    # Create a session for receiving upstream changes
                    file_count = request.get("file_count", 0)

                    if file_count <= 0:
                        response = {"status": "error", "error": "Invalid file count"}
                    else:
                        # Generate a unique ID for this staging session
                        staging_id = str(uuid.uuid4())

                        # Create staging directory
                        staging_dir = os.path.join(os.path.dirname(db.db_path), f".zync-staged-{staging_id}")
                        os.makedirs(staging_dir, exist_ok=True)

                        # Store session info
                        upstream_session = {
                            "active": True,
                            "files": {},
                            "staging_dir": staging_dir,
                            "staging_id": staging_id,
                            "created_at": datetime.now(UTC).isoformat(),
                        }

                        # Record in database
                        db.create_staging_session(staging_id, file_count)

                        response = {"status": "ok", "staging_id": staging_id}

                elif operation == "UPSTREAM_FILE":
                    # Handle receiving a file from the client
                    if not upstream_session["active"]:
                        response = {"status": "error", "error": "No active upstream session"}
                        await websocket.send(json.dumps(response))
                        continue

                    base_dir = request.get("base_dir")
                    path = request.get("path")
                    checksum = request.get("checksum")
                    size = request.get("size")

                    if not all([base_dir, path, checksum, size]):
                        response = {"status": "error", "error": "Missing file parameters"}
                        await websocket.send(json.dumps(response))
                        continue

                    if base_dir not in base_dirs:
                        response = {"status": "error", "error": "Invalid base directory"}
                        await websocket.send(json.dumps(response))
                        continue

                    # Prepare to receive file data
                    try:
                        # Receive binary file data
                        file_data = await websocket.recv()

                        # Verify checksum
                        import hashlib

                        actual_checksum = hashlib.md5(file_data).hexdigest()
                        if actual_checksum != checksum:
                            response = {"status": "error", "error": "Checksum mismatch"}
                            await websocket.send(json.dumps(response))
                            continue

                        # Check for conflicts
                        target_path = os.path.join(base_dirs[base_dir], path)
                        has_conflict = False
                        current_checksum = None

                        if os.path.exists(target_path):
                            current_checksum = calculate_checksum(target_path)
                            # Get last known checksum from database
                            last_known = db.get_last_file_state(base_dir, path)

                            # If the file exists and either:
                            # 1. There's no last known state (new file), or
                            # 2. The current file differs from last known state
                            if last_known is None or current_checksum != last_known["checksum"]:
                                has_conflict = True
                                logger.debug(
                                    f"Conflict detected for {base_dir}/{path}: current={current_checksum}, last_known={last_known['checksum'] if last_known else None}"
                                )

                        # Create directory structure in staging
                        staged_dir = os.path.join(upstream_session["staging_dir"], base_dir)
                        staged_file = os.path.join(staged_dir, path)
                        os.makedirs(os.path.dirname(staged_file), exist_ok=True)

                        # Write file to staging
                        with open(staged_file, "wb") as f:
                            f.write(file_data)

                        # Record file info
                        file_info = {
                            "path": path,
                            "base_dir": base_dir,
                            "checksum": checksum,
                            "size": size,
                            "has_conflict": has_conflict,
                            "current_checksum": current_checksum,
                        }

                        upstream_session["files"][f"{base_dir}:{path}"] = file_info

                        # Update staging session in database
                        db.add_staged_file(upstream_session["staging_id"], base_dir, path, checksum, size, has_conflict)

                        response = {"status": "ok"}

                    except Exception as e:
                        logger.error(f"Error processing upstream file: {str(e)}")
                        response = {"status": "error", "error": str(e)}

                elif operation == "FINALIZE_UPSTREAM":
                    # Complete the upstream session
                    if not upstream_session["active"]:
                        response = {"status": "error", "error": "No active upstream session"}
                    else:
                        # Create metadata file with conflict information
                        meta_path = os.path.join(upstream_session["staging_dir"], "metadata.json")
                        with open(meta_path, "w") as f:
                            json.dump(
                                {
                                    "staging_id": upstream_session["staging_id"],
                                    "created_at": upstream_session["created_at"],
                                    "files": upstream_session["files"],
                                    "conflict_count": sum(
                                        1 for f in upstream_session["files"].values() if f["has_conflict"]
                                    ),
                                },
                                f,
                                indent=2,
                            )

                        # Mark session as complete in database
                        db.complete_staging_session(
                            upstream_session["staging_id"],
                            len(upstream_session["files"]),
                            sum(1 for f in upstream_session["files"].values() if f["has_conflict"]),
                        )

                        # Send response with staging info
                        response = {
                            "status": "ok",
                            "staging_id": upstream_session["staging_id"],
                            "file_count": len(upstream_session["files"]),
                            "conflict_count": sum(1 for f in upstream_session["files"].values() if f["has_conflict"]),
                        }

                        # Reset session
                        staging_id = upstream_session["staging_id"]
                        upstream_session = {"active": False, "files": {}, "staging_dir": None, "staging_id": None}

                        logger.info(f"Upstream changes staged with ID: {staging_id}")

                await websocket.send(json.dumps(response))

            except json.JSONDecodeError:
                await websocket.send(json.dumps({"status": "error", "error": "Invalid JSON"}))
            except Exception as e:
                logger.error(f"Error handling message: {str(e)}")
                await websocket.send(json.dumps({"status": "error", "error": str(e)}))

    except websockets.exceptions.ConnectionClosed:
        logger.info("Client disconnected")

        # Clean up if there's an active session
        if upstream_session["active"] and upstream_session["staging_dir"]:
            try:
                # Mark session as aborted in database
                db.abort_staging_session(upstream_session["staging_id"])
                logger.info(f"Marked upstream session {upstream_session['staging_id']} as aborted")
            except Exception as e:
                logger.error(f"Error cleaning up upstream session: {str(e)}")


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
