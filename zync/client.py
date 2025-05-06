"""
Client for file synchronization from machine E to machine R.

This module implements a client that:
1. Connects to the daemon on machine E through a WebSocket
2. Retrieves changes since the last sync
3. Applies those changes to the local filesystem
"""

import argparse
import asyncio
import hashlib
import json
import logging
import os
from typing import Any

import websockets

from .common import ensure_directory
from .database import ClientDatabase

# Configure logging
logger = logging.getLogger(__name__)


class FileSyncClient:
    """Client for syncing files from Machine E to Machine R"""

    def __init__(self, server_url: str, sync_dirs: dict[str, str], db_path: str):
        """
        Initialize the file sync client.

        Args:
            server_url: WebSocket URL of the sync server
            sync_dirs: Mapping of remote base_dir names to local directories
            db_path: Path to SQLite database file
        """
        self.server_url = server_url
        self.sync_dirs = sync_dirs
        self.db = ClientDatabase(db_path)

    async def connect(self) -> websockets.WebSocketClientProtocol:
        """
        Connect to the sync server.

        Returns:
            WebSocket connection
        """
        return await websockets.connect(self.server_url)

    async def send_request(self, websocket: websockets.WebSocketClientProtocol, operation: str, **params) -> str:
        """
        Send a request to the server.

        Args:
            websocket: WebSocket connection
            operation: Operation name
            **params: Additional parameters for the request

        Returns:
            JSON response string
        """
        request = {"operation": operation, **params}
        await websocket.send(json.dumps(request))
        return await websocket.recv()

    async def get_last_sequence(self, websocket: websockets.WebSocketClientProtocol) -> int:
        """
        Get the last sequence number from the server.

        Args:
            websocket: WebSocket connection

        Returns:
            Latest sequence number
        """
        response = await self.send_request(websocket, "GET_LAST_SEQUENCE")
        data = json.loads(response)
        if data["status"] == "ok":
            return data["last_sequence"]
        else:
            logger.error(f"Error getting last sequence: {data.get('error')}")
            return 0

    async def get_changes(
        self, websocket: websockets.WebSocketClientProtocol, since_sequence: int, limit: int = 1000
    ) -> tuple[list[dict[str, Any]], bool]:
        """
        Get changes from the server.

        Args:
            websocket: WebSocket connection
            since_sequence: Sequence number to start from
            limit: Maximum number of changes to return

        Returns:
            Tuple of (changes list, more_available flag)
        """
        response = await self.send_request(websocket, "GET_CHANGES", since_sequence=since_sequence, limit=limit)
        data = json.loads(response)
        if data["status"] == "ok":
            return data["changes"], data.get("more_available", False)
        else:
            logger.error(f"Error getting changes: {data.get('error')}")
            return [], False

    async def get_file(
        self, websocket: websockets.WebSocketClientProtocol, base_dir: str, path: str
    ) -> tuple[bytes | None, dict[str, Any] | None]:
        """
        Get a file from the server.

        Args:
            websocket: WebSocket connection
            base_dir: Base directory identifier
            path: Relative file path

        Returns:
            Tuple of (file data, metadata) or (None, None) on error
        """
        response = await self.send_request(websocket, "GET_FILE", base_dir=base_dir, path=path)

        try:
            metadata = json.loads(response)
            if metadata["status"] != "ok":
                logger.error(f"Error getting file {path}: {metadata.get('error')}")
                return None, None

            # Receive the binary data
            file_data = await websocket.recv()

            # Verify checksum
            actual_checksum = hashlib.md5(file_data).hexdigest()
            if actual_checksum != metadata["checksum"]:
                logger.error(f"Checksum mismatch for {path}: expected {metadata['checksum']}, got {actual_checksum}")
                return None, None

            return file_data, metadata
        except Exception as e:
            logger.error(f"Error processing file {path}: {str(e)}")
            return None, None

    async def acknowledge_changes(self, websocket: websockets.WebSocketClientProtocol, sequence: int) -> bool:
        """
        Acknowledge changes up to a sequence number.

        Args:
            websocket: WebSocket connection
            sequence: Sequence number to acknowledge

        Returns:
            True if successful, False otherwise
        """
        response = await self.send_request(websocket, "ACK_CHANGES", sequence_number=sequence)
        data = json.loads(response)
        if data["status"] != "ok":
            logger.error(f"Error acknowledging changes: {data.get('error')}")
            return False
        return True

    async def apply_change(self, websocket: websockets.WebSocketClientProtocol, change: dict[str, Any]) -> bool:
        """
        Apply a single change to the local filesystem.

        Args:
            websocket: WebSocket connection
            change: Change record from the server

        Returns:
            True if successful, False otherwise
        """
        base_dir = change["base_dir"]
        path = change["path"]
        operation = change["operation"]

        # Check if we have this base directory mapped
        if base_dir not in self.sync_dirs:
            logger.warning(f"Ignoring change for unmapped base directory: {base_dir}")
            return True

        local_dir = self.sync_dirs[base_dir]
        local_path = os.path.join(local_dir, path)

        try:
            if operation == "delete":
                if os.path.exists(local_path):
                    if os.path.isfile(local_path):
                        os.unlink(local_path)
                        logger.info(f"Deleted file: {local_path}")
                    else:
                        logger.warning(f"Cannot delete non-file: {local_path}")
                return True

            elif operation in ("add", "modify"):
                # Create parent directories if needed
                os.makedirs(os.path.dirname(local_path), exist_ok=True)

                # Download the file
                file_data, metadata = await self.get_file(websocket, base_dir, path)
                if file_data is None:
                    return False

                # Write the file
                with open(local_path, "wb") as f:
                    f.write(file_data)

                logger.info(f"{operation.capitalize()}d file: {local_path}")
                return True

            else:
                logger.warning(f"Unknown operation: {operation}")
                return False

        except Exception as e:
            logger.error(f"Error applying change: {str(e)}")
            return False

    async def sync_changes(self) -> bool:
        """
        Sync changes from the server.

        Returns:
            True if sync completed successfully, False otherwise
        """
        try:
            async with await self.connect() as websocket:
                # Get the last sequence we processed
                local_sequence = self.db.get_last_sequence()

                # Get the server's last sequence
                server_sequence = await self.get_last_sequence(websocket)

                if server_sequence <= local_sequence:
                    logger.info("No new changes to sync")
                    return True

                logger.info(f"Syncing changes from sequence {local_sequence + 1} to {server_sequence}")

                # Get and apply changes in batches
                current_sequence = local_sequence
                more_available = True
                batch_size = 100

                while more_available and current_sequence < server_sequence:
                    changes, more_available = await self.get_changes(websocket, current_sequence, limit=batch_size)

                    if not changes:
                        break

                    # Apply each change
                    success_count = 0
                    for change in changes:
                        success = await self.apply_change(websocket, change)
                        if success:
                            current_sequence = max(current_sequence, change["sequence"])
                            success_count += 1

                    logger.info(f"Applied {success_count}/{len(changes)} changes in batch")

                    # Update our last processed sequence
                    self.db.update_last_sequence(current_sequence)

                    # Acknowledge changes on the server
                    await self.acknowledge_changes(websocket, current_sequence)

                    logger.info(f"Synced up to sequence {current_sequence}")

                logger.info("Sync completed successfully")
                return True

        except Exception as e:
            logger.error(f"Error during sync: {str(e)}")
            return False

    async def ping_server(self) -> bool:
        """
        Ping the server to check connectivity.

        Returns:
            True if server is reachable, False otherwise
        """
        try:
            async with await self.connect() as websocket:
                response = await self.send_request(websocket, "PING")
                data = json.loads(response)
                if data["status"] == "ok":
                    logger.info(f"Server is up, time: {data['server_time']}, uptime: {data['uptime']} seconds")
                    return True
                else:
                    logger.error(f"Error pinging server: {data.get('error')}")
                    return False
        except Exception as e:
            logger.error(f"Connection error: {str(e)}")
            return False


async def run_sync_loop(client: FileSyncClient, interval: int):
    """
    Run the sync loop at specified intervals.

    Args:
        client: FileSyncClient instance
        interval: Sync interval in seconds
    """
    while True:
        try:
            await client.sync_changes()
        except Exception as e:
            logger.error(f"Error in sync loop: {str(e)}")

        await asyncio.sleep(interval)


def parse_directory_mappings(dir_specs: list[str]) -> dict[str, str]:
    """
    Parse directory mapping specifications.

    Args:
        dir_specs: List of directory specifications (remote_name:local_path)

    Returns:
        Mapping of remote directory names to local directory paths
    """
    sync_dirs = {}
    for dir_spec in dir_specs:
        try:
            remote_name, local_path = dir_spec.split(":", 1)
            abs_path = os.path.abspath(local_path)
            ensure_directory(abs_path)
            sync_dirs[remote_name] = abs_path
        except ValueError:
            logger.error(f"Invalid directory mapping: {dir_spec}, use format remote_name:local_path")

    return sync_dirs


def main():
    """Main entry point for the client"""
    parser = argparse.ArgumentParser(description="File Sync Client (Machine R)")
    parser.add_argument("--server", required=True, help="WebSocket server URL (ws://host:port)")
    parser.add_argument("--db", default="sync_client.db", help="SQLite database path")
    parser.add_argument("--interval", type=int, default=60, help="Sync interval in seconds")
    parser.add_argument("--dirs", required=True, nargs="+", help="Directory mappings (format: remote_name:local_path)")
    parser.add_argument("--once", action="store_true", help="Run once and exit")
    parser.add_argument(
        "--log-level", default="INFO", choices=["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"], help="Logging level"
    )
    args = parser.parse_args()

    # Configure logging
    logging.basicConfig(
        level=getattr(logging, args.log_level),
        format="%(asctime)s - %(levelname)s - %(message)s",
        handlers=[logging.StreamHandler(), logging.FileHandler("zync_client.log")],
    )

    # Parse directory mappings
    sync_dirs = parse_directory_mappings(args.dirs)

    if not sync_dirs:
        logger.error("No valid directory mappings")
        return 1

    # Create sync client
    client = FileSyncClient(args.server, sync_dirs, args.db)

    if args.once:
        # Run once and exit
        success = asyncio.run(client.sync_changes())
        return 0 if success else 1
    else:
        # Run the ping to check connectivity
        if not asyncio.run(client.ping_server()):
            logger.error("Cannot connect to server, exiting")
            return 1

        # Run continuous sync loop
        logger.info(f"Starting sync loop with {len(sync_dirs)} directories, interval: {args.interval} seconds")
        try:
            asyncio.run(run_sync_loop(client, args.interval))
        except KeyboardInterrupt:
            logger.info("Stopping sync client")

    return 0


if __name__ == "__main__":
    import sys

    sys.exit(main())
