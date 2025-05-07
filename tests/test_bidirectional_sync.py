import asyncio
import os
import shutil
import tempfile
import time
import unittest
from unittest.mock import patch

from zync.apply_upstream import apply_session
from zync.client import FileSyncClient
from zync.daemon import handle_client
from zync.database import ClientDatabase, DaemonDatabase


class TestBidirectionalSync(unittest.IsolatedAsyncioTestCase):
    """
    End-to-end tests for bidirectional sync functionality.

    This test class sets up both daemon and client components and tests
    the bidirectional sync workflow without excessive mocking.
    """

    def setUp(self):
        # Create temporary directories
        self.temp_dir = tempfile.mkdtemp()

        # Machine E directories
        self.machine_e_dir = os.path.join(self.temp_dir, "machine_e")
        self.machine_e_docs = os.path.join(self.machine_e_dir, "docs")
        self.machine_e_code = os.path.join(self.machine_e_dir, "code")

        # Machine R directories
        self.machine_r_dir = os.path.join(self.temp_dir, "machine_r")
        self.machine_r_docs = os.path.join(self.machine_r_dir, "docs")
        self.machine_r_code = os.path.join(self.machine_r_dir, "code")

        # Create all directories
        for directory in [self.machine_e_docs, self.machine_e_code, self.machine_r_docs, self.machine_r_code]:
            os.makedirs(directory)

        # Create databases
        self.daemon_db_path = os.path.join(self.temp_dir, "daemon.db")
        self.client_db_path = os.path.join(self.temp_dir, "client.db")

        self.daemon_db = DaemonDatabase(self.daemon_db_path)
        self.client_db = ClientDatabase(self.client_db_path)

        # Create necessary tables for tracking file state in the client database
        self.client_db.execute("""
        CREATE TABLE IF NOT EXISTS file_state (
            sequence INTEGER PRIMARY KEY AUTOINCREMENT,
            base_dir TEXT NOT NULL,
            path TEXT NOT NULL,
            operation TEXT NOT NULL,
            checksum TEXT,
            size INTEGER,
            timestamp INTEGER NOT NULL,
            UNIQUE(base_dir, path, sequence)
        )
        """)
        self.client_db.commit()

        # Set up directory mappings
        self.daemon_dirs = {"docs": self.machine_e_docs, "code": self.machine_e_code}

        self.client_dirs = {"docs": self.machine_r_docs, "code": self.machine_r_code}

        # Create client
        self.client = FileSyncClient("ws://test.server", self.client_dirs, self.client_db_path)

        # Save original methods before patching
        self.original_connect = self.client.connect

    def tearDown(self):
        # Clean up temporary directory
        shutil.rmtree(self.temp_dir)

    async def simulate_websocket_connection(self):
        """Create a simulated WebSocket connection between client and daemon"""
        # Use in-memory queues for bidirectional communication
        client_to_daemon_queue = asyncio.Queue()
        daemon_to_client_queue = asyncio.Queue()

        # Create a client websocket that works with the FileSyncClient class
        class MockClientWebSocket:
            async def send(self, data):
                await client_to_daemon_queue.put(data)
                await asyncio.sleep(0)

            async def recv(self):
                result = await daemon_to_client_queue.get()
                return result

            async def __aenter__(self):
                return self

            async def __aexit__(self, *args):
                pass

        # For daemon side
        class MockDaemonWebSocket:
            async def send(self, data):
                await daemon_to_client_queue.put(data)

            async def recv(self):
                result = await client_to_daemon_queue.get()
                return result

            def close(self):
                pass

            def __aiter__(self):
                return self

            async def __anext__(self):
                try:
                    # Assuming recv() will raise an exception (e.g., CancelledError)
                    # or return a special value if the connection is "closed"
                    # For this mock, it will block on queue.get() until a message or cancellation.
                    message = await self.recv()
                    # If your WebSocket protocol uses a specific message to signal end, handle it here.
                    # For example, if recv() could return None on close:
                    # if message is None:
                    #     raise StopAsyncIteration
                    return message
                except asyncio.CancelledError:
                    # If the task is cancelled, it's a way to stop iteration.
                    raise StopAsyncIteration  # noqa: B904

        # Create the instances
        client_ws = MockClientWebSocket()
        daemon_ws = MockDaemonWebSocket()

        # Start the daemon handler
        daemon_task = asyncio.create_task(handle_client(daemon_ws, self.daemon_dirs, self.daemon_db))
        await asyncio.sleep(0)  # Give the daemon_task a chance to start up

        # Create a simple function to return the client websocket
        async def get_client_ws():
            return client_ws

        return get_client_ws, daemon_task

    def create_file(self, path, content):
        """Create a test file with the given content"""
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "wb") as f:
            f.write(content)

    def read_file(self, path):
        """Read a file and return its content"""
        with open(path, "rb") as f:
            return f.read()

    def assert_files_equal(self, path1, path2):
        """Assert that two files have the same content"""
        content1 = self.read_file(path1)
        content2 = self.read_file(path2)
        self.assertEqual(content1, content2)

    async def test_full_bidirectional_workflow(self):
        """Test the complete bidirectional workflow (E→R and R→E)"""
        # Set up the simulated WebSocket connection
        connect_func_for_client, daemon_task = await self.simulate_websocket_connection()

        # Patch the client's connect method to return our mock websocket
        self.client.connect = connect_func_for_client

        try:
            # STEP 1: Create files on machine E and sync to machine R
            self.create_file(os.path.join(self.machine_e_docs, "original.txt"), b"Original content from E")
            self.create_file(os.path.join(self.machine_e_code, "script.py"), b"print('From E')")

            # Record changes in daemon database
            self.daemon_db.record_change("add", "original.txt", "docs", "e_checksum1", 22)
            self.daemon_db.record_change("add", "script.py", "code", "e_checksum2", 16)

            # STEP 2: Sync from E to R
            result = await self.client.sync_changes()
            self.assertTrue(result)

            # Verify files synced to machine R
            self.assert_files_equal(
                os.path.join(self.machine_e_docs, "original.txt"), os.path.join(self.machine_r_docs, "original.txt")
            )
            self.assert_files_equal(
                os.path.join(self.machine_e_code, "script.py"), os.path.join(self.machine_r_code, "script.py")
            )

            # STEP 3: Create new files on machine R and modify existing files
            self.create_file(os.path.join(self.machine_r_docs, "new.txt"), b"New content from R")
            self.create_file(os.path.join(self.machine_r_docs, "original.txt"), b"Modified by R")

            # STEP 4: Send updates upstream from R to E
            result = await self.client.update_upstream()
            self.assertTrue(result)

            # Get the staging ID from the daemon's database
            sessions = self.daemon_db.get_staging_sessions()
            self.assertEqual(len(sessions), 1)
            staging_id = sessions[0]["staging_id"]

            # Verify one conflict (modified file) and one new file
            staged_files = self.daemon_db.get_staged_files(staging_id)
            self.assertEqual(len(staged_files), 3)

            # Find the files with conflicts
            conflict_files = [f for f in staged_files if f["has_conflict"]]
            self.assertTrue(len(conflict_files) > 0, "Expected at least one file with conflict")

            # Check if original.txt is among the staged files and has the expected content
            original_file = next((f for f in staged_files if f["path"] == "original.txt"), None)
            self.assertIsNotNone(original_file, "Expected to find original.txt among staged files")

            # STEP 5: Apply upstream changes with force
            result = apply_session(self.daemon_db, staging_id, self.daemon_dirs, force=True, interactive=False)
            self.assertTrue(result)

            # Verify changes applied to machine E
            self.assertEqual(self.read_file(os.path.join(self.machine_e_docs, "original.txt")), b"Modified by R")
            self.assertEqual(self.read_file(os.path.join(self.machine_e_docs, "new.txt")), b"New content from R")

            # STEP 6: Create a new file on machine E
            self.create_file(os.path.join(self.machine_e_code, "another.py"), b"print('Another from E')")
            self.daemon_db.record_change("add", "another.py", "code", "e_checksum3", 23)

            # STEP 7: Sync back from E to R
            result = await self.client.sync_changes()
            self.assertTrue(result)

            # Verify all files are now in sync between E and R
            self.assert_files_equal(
                os.path.join(self.machine_e_docs, "original.txt"), os.path.join(self.machine_r_docs, "original.txt")
            )
            self.assert_files_equal(
                os.path.join(self.machine_e_docs, "new.txt"), os.path.join(self.machine_r_docs, "new.txt")
            )
            self.assert_files_equal(
                os.path.join(self.machine_e_code, "script.py"), os.path.join(self.machine_r_code, "script.py")
            )
            self.assert_files_equal(
                os.path.join(self.machine_e_code, "another.py"), os.path.join(self.machine_r_code, "another.py")
            )

        finally:
            # Clean up and restore
            daemon_task.cancel()
            try:
                await daemon_task
            except asyncio.CancelledError:
                pass
            self.client.connect = self.original_connect

    async def test_apply_with_force(self):
        """Test applying changes with --force flag overriding conflicts"""
        # Create test file on Machine R with conflict
        self.create_file(os.path.join(self.machine_e_docs, "conflict.txt"), b"Machine E version")
        self.create_file(os.path.join(self.machine_r_docs, "conflict.txt"), b"Machine R version")

        # Record the file in the daemon database
        self.daemon_db.record_change("add", "conflict.txt", "docs", "e_checksum", 16)

        # Add entry to the client's file_state table to track what it knows about
        current_time = int(time.time())
        self.client_db.execute(
            """
            INSERT INTO file_state (base_dir, path, operation, checksum, size, timestamp)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            ("docs", "conflict.txt", "add", "orig_checksum", 16, current_time),
        )
        self.client_db.commit()

        # Set up the simulated WebSocket connection
        connect_func_for_client, daemon_task = await self.simulate_websocket_connection()

        # Patch the client's connect method to return our mock websocket
        self.client.connect = connect_func_for_client

        try:
            # Run the upload operation
            result = await self.client.update_upstream()
            self.assertTrue(result)

            # Get the staging ID from the daemon's database
            sessions = self.daemon_db.get_staging_sessions()
            self.assertEqual(len(sessions), 1)
            staging_id = sessions[0]["staging_id"]

            # Verify conflict detection
            self.assertEqual(sessions[0]["conflict_count"], 1)

            # Apply the staged changes with force flag
            result = apply_session(self.daemon_db, staging_id, self.daemon_dirs, force=True, interactive=False)
            self.assertTrue(result)

            # Verify the Machine R version was applied
            machine_e_content = self.read_file(os.path.join(self.machine_e_docs, "conflict.txt"))
            self.assertEqual(machine_e_content, b"Machine R version")

        finally:
            # Clean up and restore
            daemon_task.cancel()
            try:
                await daemon_task
            except asyncio.CancelledError:
                pass
            self.client.connect = self.original_connect

    async def test_upload_from_r_to_e(self):
        """Test uploading changes from Machine R to Machine E"""
        # Create test files on Machine R
        self.create_file(os.path.join(self.machine_r_docs, "test1.txt"), b"Test file 1 content")
        self.create_file(os.path.join(self.machine_r_docs, "test2.txt"), b"Test file 2 content")
        self.create_file(os.path.join(self.machine_r_code, "main.py"), b"print('Hello, World!')")

        # Add entries to the client's file_state table
        current_time = int(time.time())
        self.client_db.execute(
            """
            INSERT INTO file_state (base_dir, path, operation, checksum, size, timestamp)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            ("docs", "test1.txt", "add", "test1_checksum", 19, current_time),
        )
        self.client_db.execute(
            """
            INSERT INTO file_state (base_dir, path, operation, checksum, size, timestamp)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            ("docs", "test2.txt", "add", "test2_checksum", 19, current_time),
        )
        self.client_db.execute(
            """
            INSERT INTO file_state (base_dir, path, operation, checksum, size, timestamp)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            ("code", "main.py", "add", "main_checksum", 21, current_time),
        )
        self.client_db.commit()

        # Set up the simulated WebSocket connection
        connect_func_for_client, daemon_task = await self.simulate_websocket_connection()

        # Patch the client's connect method to return our mock websocket
        self.client.connect = connect_func_for_client

        try:
            # Run the upload operation
            result = await self.client.update_upstream()
            self.assertTrue(result)

            # Get the staging ID from the daemon's database
            sessions = self.daemon_db.get_staging_sessions()
            self.assertEqual(len(sessions), 1)
            staging_id = sessions[0]["staging_id"]

            # Verify staged files
            staged_files = self.daemon_db.get_staged_files(staging_id)
            self.assertEqual(len(staged_files), 3)

            # Verify staged files in the staging directory
            staging_dir = os.path.join(os.path.dirname(self.daemon_db_path), f".zync-staged-{staging_id}")
            self.assertTrue(os.path.exists(staging_dir))

            # Apply the staged changes
            result = apply_session(self.daemon_db, staging_id, self.daemon_dirs, force=True, interactive=False)
            self.assertTrue(result)

            # Verify the files were applied to Machine E
            self.assert_files_equal(
                os.path.join(self.machine_r_docs, "test1.txt"), os.path.join(self.machine_e_docs, "test1.txt")
            )
            self.assert_files_equal(
                os.path.join(self.machine_r_docs, "test2.txt"), os.path.join(self.machine_e_docs, "test2.txt")
            )
            self.assert_files_equal(
                os.path.join(self.machine_r_code, "main.py"), os.path.join(self.machine_e_code, "main.py")
            )

        finally:
            # Clean up and restore
            daemon_task.cancel()
            try:
                await daemon_task
            except asyncio.CancelledError:
                pass
            self.client.connect = self.original_connect

    async def test_conflict_detection(self):
        """Test conflict detection when Machine E has local changes"""
        # Create initial test file on both machines
        initial_content = b"Initial content"
        self.create_file(os.path.join(self.machine_e_docs, "conflict.txt"), initial_content)
        self.create_file(os.path.join(self.machine_r_docs, "conflict.txt"), initial_content)

        # Record the initial file in the daemon database
        self.daemon_db.record_change("add", "conflict.txt", "docs", "initial_checksum", len(initial_content))

        # Add entry to the client's file_state table to track what it knows about
        current_time = int(time.time())
        self.client_db.execute(
            """
            INSERT INTO file_state (base_dir, path, operation, checksum, size, timestamp)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            ("docs", "conflict.txt", "add", "initial_checksum", len(initial_content), current_time),
        )
        self.client_db.commit()

        # Modify the file on Machine E
        e_modified_content = b"Modified on Machine E"
        self.create_file(os.path.join(self.machine_e_docs, "conflict.txt"), e_modified_content)
        self.daemon_db.record_change("modify", "conflict.txt", "docs", "e_modified_checksum", len(e_modified_content))

        # Modify the file on Machine R
        r_modified_content = b"Modified on Machine R"
        self.create_file(os.path.join(self.machine_r_docs, "conflict.txt"), r_modified_content)

        # Set up the simulated WebSocket connection
        connect_func_for_client, daemon_task = await self.simulate_websocket_connection()

        # Patch the client's connect method to return our mock websocket
        self.client.connect = connect_func_for_client

        try:
            # Run the upload operation
            result = await self.client.update_upstream()
            self.assertTrue(result)

            # Get the staging ID from the daemon's database
            sessions = self.daemon_db.get_staging_sessions()
            self.assertEqual(len(sessions), 1)
            staging_id = sessions[0]["staging_id"]

            # Verify conflict detection
            self.assertEqual(sessions[0]["conflict_count"], 1)

            # Verify staged files
            staged_files = self.daemon_db.get_staged_files(staging_id)
            self.assertEqual(len(staged_files), 1)

            # Verify the conflict flag is set
            self.assertTrue(staged_files[0]["has_conflict"])

        finally:
            # Clean up and restore
            daemon_task.cancel()
            try:
                await daemon_task
            except asyncio.CancelledError:
                pass
            self.client.connect = self.original_connect

    @patch("builtins.input")
    async def test_interactive_apply(self, mock_input):
        """Test interactive conflict resolution"""
        # Create test file on Machine R with conflict
        self.create_file(os.path.join(self.machine_e_docs, "conflict1.txt"), b"Machine E version")
        self.create_file(os.path.join(self.machine_r_docs, "conflict1.txt"), b"Machine R version")

        # Record the file in the daemon database
        self.daemon_db.record_change("add", "conflict1.txt", "docs", "e_checksum", 16)

        # Add entry to the client's file_state table to track what it knows about
        current_time = int(time.time())
        self.client_db.execute(
            """
            INSERT INTO file_state (base_dir, path, operation, checksum, size, timestamp)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            ("docs", "conflict1.txt", "add", "orig_checksum", 16, current_time),
        )
        self.client_db.commit()

        # Set up the simulated WebSocket connection
        connect_func_for_client, daemon_task = await self.simulate_websocket_connection()

        # Patch the client's connect method to return our mock websocket
        self.client.connect = connect_func_for_client

        try:
            # Run the upload operation
            result = await self.client.update_upstream()
            self.assertTrue(result)

            # Get the staging ID from the daemon's database
            sessions = self.daemon_db.get_staging_sessions()
            self.assertEqual(len(sessions), 1)
            staging_id = sessions[0]["staging_id"]

            # Set up input mock to simulate user choosing 'n' (reject)
            def mock_input_side_effect(prompt):
                return "n"

            mock_input.side_effect = mock_input_side_effect

            # Apply the staged changes interactively
            result = apply_session(self.daemon_db, staging_id, self.daemon_dirs, force=False, interactive=True)
            self.assertTrue(result)

            # Verify the Machine E version was kept
            machine_e_content = self.read_file(os.path.join(self.machine_e_docs, "conflict1.txt"))
            self.assertEqual(machine_e_content, b"Machine E version")

            # For the second part, create new files with a different name
            self.create_file(os.path.join(self.machine_e_docs, "conflict2.txt"), b"Machine E version")
            self.create_file(os.path.join(self.machine_r_docs, "conflict2.txt"), b"Machine R version")

            # Record the file in the daemon database
            self.daemon_db.record_change("add", "conflict2.txt", "docs", "e_checksum", 16)

            # Add entry to the client's file_state table to track what it knows about
            current_time = int(time.time())
            self.client_db.execute(
                """
                INSERT INTO file_state (base_dir, path, operation, checksum, size, timestamp)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                ("docs", "conflict2.txt", "add", "orig_checksum", 16, current_time),
            )
            self.client_db.commit()

            # Run the upload operation for the second file
            result = await self.client.update_upstream()
            self.assertTrue(result)

            # Get the new staging ID
            sessions = self.daemon_db.get_staging_sessions()
            new_sessions = [s for s in sessions if s["staging_id"] != staging_id]
            self.assertTrue(len(new_sessions) > 0)
            new_staging_id = new_sessions[0]["staging_id"]

            # Set up input mock to simulate user choosing 'y' (accept)
            def mock_input_side_effect_yes(prompt):
                return "y"

            mock_input.side_effect = mock_input_side_effect_yes

            # Apply the staged changes interactively
            result = apply_session(self.daemon_db, new_staging_id, self.daemon_dirs, force=False, interactive=True)
            self.assertTrue(result)

            # Verify the Machine R version was applied
            machine_e_content = self.read_file(os.path.join(self.machine_e_docs, "conflict2.txt"))
            self.assertEqual(machine_e_content, b"Machine R version")

        finally:
            # Clean up and restore
            daemon_task.cancel()
            try:
                await daemon_task
            except asyncio.CancelledError:
                pass
            self.client.connect = self.original_connect


if __name__ == "__main__":
    unittest.main()
