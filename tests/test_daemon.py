import asyncio
import json
import os
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

from watchdog.events import FileSystemEvent

import zync.daemon
from zync.daemon import FileChangeHandler, handle_client
from zync.database import DaemonDatabase  # Actual import for type hinting, will be mocked


class TestFileChangeHandler(unittest.TestCase):
    def setUp(self):
        # Mock the DaemonDatabase
        self.mock_db = MagicMock(spec=DaemonDatabase)
        self.base_dirs_config = {"test_dir": "/test_base"}
        self.handler = FileChangeHandler(self.mock_db, self.base_dirs_config)
        # Convert base_dirs_config to the format used internally by FileChangeHandler for easier path manipulation
        self.abs_base_dir_path = "/test_base"

    def _create_event(self, event_type: str, src_path: str, is_directory: bool = False, dest_path: str | None = None):
        """Helper to create a FileSystemEvent object."""
        event = FileSystemEvent(src_path)
        event.event_type = event_type
        event.is_directory = is_directory
        if dest_path:
            event.dest_path = dest_path
        return event

    @patch("zync.daemon.get_file_info")
    def test_on_created_new_file(self, mock_get_file_info):
        mock_get_file_info.return_value = {"checksum": "fake_checksum", "size": 123}

        file_path = os.path.join(self.abs_base_dir_path, "new_file.txt")
        event = self._create_event("created", file_path)

        self.handler.on_created(event)

        self.mock_db.record_change.assert_called_once_with("add", "new_file.txt", "test_dir", "fake_checksum", 123)
        mock_get_file_info.assert_called_once_with(file_path)

    @patch("zync.daemon.get_file_info")
    def test_on_created_ignored_file(self, mock_get_file_info):
        file_path = os.path.join(self.abs_base_dir_path, ".git", "some_file")
        event = self._create_event("created", file_path)

        self.handler.on_created(event)

        self.mock_db.record_change.assert_not_called()
        mock_get_file_info.assert_not_called()

    @patch("zync.daemon.get_file_info")
    def test_on_created_directory(self, mock_get_file_info):
        dir_path = os.path.join(self.abs_base_dir_path, "new_dir")
        event = self._create_event("created", dir_path, is_directory=True)

        self.handler.on_created(event)

        self.mock_db.record_change.assert_not_called()
        mock_get_file_info.assert_not_called()

    @patch("zync.daemon.get_file_info")
    def test_on_created_file_outside_basedir(self, mock_get_file_info):
        file_path = "/another_dir/some_file.txt"
        event = self._create_event("created", file_path)

        self.handler.on_created(event)
        self.mock_db.record_change.assert_not_called()
        mock_get_file_info.assert_not_called()

    # Tests for on_deleted
    def test_on_deleted_file(self):
        file_path = os.path.join(self.abs_base_dir_path, "existing_file.txt")
        event = self._create_event("deleted", file_path)

        self.handler.on_deleted(event)

        self.mock_db.record_change.assert_called_once_with("delete", "existing_file.txt", "test_dir")

    def test_on_deleted_ignored_file(self):
        file_path = os.path.join(self.abs_base_dir_path, ".git", "some_file")
        event = self._create_event("deleted", file_path)

        self.handler.on_deleted(event)

        self.mock_db.record_change.assert_not_called()

    def test_on_deleted_directory(self):
        dir_path = os.path.join(self.abs_base_dir_path, "existing_dir")
        event = self._create_event("deleted", dir_path, is_directory=True)

        self.handler.on_deleted(event)

        self.mock_db.record_change.assert_not_called()

    def test_on_deleted_file_outside_basedir(self):
        file_path = "/another_dir/some_file.txt"
        event = self._create_event("deleted", file_path)

        self.handler.on_deleted(event)
        self.mock_db.record_change.assert_not_called()

    # Tests for on_modified
    @patch("zync.daemon.get_file_info")
    def test_on_modified_file(self, mock_get_file_info):
        mock_get_file_info.return_value = {"checksum": "new_checksum", "size": 456}
        file_path = os.path.join(self.abs_base_dir_path, "existing_file.txt")
        event = self._create_event("modified", file_path)

        self.handler.on_modified(event)

        self.mock_db.record_change.assert_called_once_with(
            "modify", "existing_file.txt", "test_dir", "new_checksum", 456
        )
        mock_get_file_info.assert_called_once_with(file_path)

    @patch("zync.daemon.get_file_info")
    def test_on_modified_ignored_file(self, mock_get_file_info):
        file_path = os.path.join(self.abs_base_dir_path, ".git", "some_file")
        event = self._create_event("modified", file_path)

        self.handler.on_modified(event)

        self.mock_db.record_change.assert_not_called()
        mock_get_file_info.assert_not_called()

    @patch("zync.daemon.get_file_info")
    def test_on_modified_directory(self, mock_get_file_info):
        dir_path = os.path.join(self.abs_base_dir_path, "existing_dir")
        event = self._create_event("modified", dir_path, is_directory=True)

        self.handler.on_modified(event)

        self.mock_db.record_change.assert_not_called()
        mock_get_file_info.assert_not_called()

    @patch("zync.daemon.get_file_info")
    def test_on_modified_file_outside_basedir(self, mock_get_file_info):
        file_path = "/another_dir/some_file.txt"
        event = self._create_event("modified", file_path)

        self.handler.on_modified(event)
        self.mock_db.record_change.assert_not_called()
        mock_get_file_info.assert_not_called()

    # Tests for on_moved
    @patch("zync.daemon.get_file_info")
    def test_on_moved_file(self, mock_get_file_info):
        mock_get_file_info.return_value = {"checksum": "dest_checksum", "size": 789}  # For the new destination file
        src_path = os.path.join(self.abs_base_dir_path, "source_file.txt")
        dest_path = os.path.join(self.abs_base_dir_path, "dest_file.txt")
        event = self._create_event("moved", src_path, dest_path=dest_path)

        self.handler.on_moved(event)

        self.mock_db.record_change.assert_any_call("delete", "source_file.txt", "test_dir")
        self.mock_db.record_change.assert_any_call("add", "dest_file.txt", "test_dir", "dest_checksum", 789)
        self.assertEqual(self.mock_db.record_change.call_count, 2)
        mock_get_file_info.assert_called_once_with(dest_path)

    @patch("zync.daemon.get_file_info")
    def test_on_moved_file_src_ignored(self, mock_get_file_info):
        mock_get_file_info.return_value = {"checksum": "dest_checksum", "size": 789}
        src_path = os.path.join(self.abs_base_dir_path, ".git", "ignored_source.txt")
        dest_path = os.path.join(self.abs_base_dir_path, "dest_file.txt")
        event = self._create_event("moved", src_path, dest_path=dest_path)

        self.handler.on_moved(event)

        self.mock_db.record_change.assert_called_once_with("add", "dest_file.txt", "test_dir", "dest_checksum", 789)
        mock_get_file_info.assert_called_once_with(dest_path)

    @patch("zync.daemon.get_file_info")
    def test_on_moved_file_dest_ignored(self, mock_get_file_info):
        # No need to mock get_file_info return if dest is ignored, as it won't be called for dest
        src_path = os.path.join(self.abs_base_dir_path, "source_file.txt")
        dest_path = os.path.join(self.abs_base_dir_path, ".git", "ignored_dest.txt")
        event = self._create_event("moved", src_path, dest_path=dest_path)

        self.handler.on_moved(event)

        self.mock_db.record_change.assert_called_once_with("delete", "source_file.txt", "test_dir")
        mock_get_file_info.assert_not_called()  # Not called because dest is ignored

    @patch("zync.daemon.get_file_info")
    def test_on_moved_file_both_ignored(self, mock_get_file_info):
        src_path = os.path.join(self.abs_base_dir_path, ".git", "ignored_source.txt")
        dest_path = os.path.join(self.abs_base_dir_path, "build", "ignored_dest.txt")
        event = self._create_event("moved", src_path, dest_path=dest_path)

        self.handler.on_moved(event)

        self.mock_db.record_change.assert_not_called()
        mock_get_file_info.assert_not_called()

    @patch("zync.daemon.get_file_info")
    def test_on_moved_directory(self, mock_get_file_info):
        src_path = os.path.join(self.abs_base_dir_path, "src_dir")
        dest_path = os.path.join(self.abs_base_dir_path, "dest_dir")
        event = self._create_event("moved", src_path, dest_path=dest_path, is_directory=True)

        self.handler.on_moved(event)

        self.mock_db.record_change.assert_not_called()
        mock_get_file_info.assert_not_called()

    @patch("zync.daemon.get_file_info")
    def test_on_moved_file_outside_basedir_src(self, mock_get_file_info):
        mock_get_file_info.return_value = {"checksum": "dest_checksum", "size": 789}
        src_path = "/another_dir/source_file.txt"
        dest_path = os.path.join(self.abs_base_dir_path, "dest_file.txt")
        event = self._create_event("moved", src_path, dest_path=dest_path)

        self.handler.on_moved(event)

        self.mock_db.record_change.assert_called_once_with("add", "dest_file.txt", "test_dir", "dest_checksum", 789)
        mock_get_file_info.assert_called_once_with(dest_path)

    @patch("zync.daemon.get_file_info")
    def test_on_moved_file_outside_basedir_dest(self, mock_get_file_info):
        src_path = os.path.join(self.abs_base_dir_path, "source_file.txt")
        dest_path = "/another_dir/dest_file.txt"
        event = self._create_event("moved", src_path, dest_path=dest_path)

        self.handler.on_moved(event)

        self.mock_db.record_change.assert_called_once_with("delete", "source_file.txt", "test_dir")
        mock_get_file_info.assert_not_called()

    @patch("zync.daemon.get_file_info")
    def test_on_moved_file_outside_basedir_both(self, mock_get_file_info):
        src_path = "/another_dir/source_file.txt"
        dest_path = "/yet_another_dir/dest_file.txt"
        event = self._create_event("moved", src_path, dest_path=dest_path)

        self.handler.on_moved(event)

        self.mock_db.record_change.assert_not_called()
        mock_get_file_info.assert_not_called()


class TestFileChangeHandlerIgnores(unittest.TestCase):
    def setUp(self):
        # Mock the DaemonDatabase as it's not relevant for _is_ignored
        self.mock_db = MagicMock(spec=DaemonDatabase)
        self.base_dirs_config = {"test_dir": "/test_base"}
        self.handler = FileChangeHandler(self.mock_db, self.base_dirs_config)

    def test_is_ignored_git_directory(self):
        # Test that a path within a .git directory is ignored
        self.assertTrue(self.handler._is_ignored("/some/path/.git/config"))
        self.assertTrue(self.handler._is_ignored("another/path/.git/hooks"))
        self.assertTrue(self.handler._is_ignored(".git/objects"))  # Relative path

    def test_is_ignored_pyc_file(self):
        # Test that a .pyc file is ignored
        self.assertTrue(self.handler._is_ignored("/some/path/module.pyc"))
        self.assertTrue(self.handler._is_ignored("another/script.pyc"))

    def test_is_ignored_backup_file(self):
        # Test that a backup file (ending with ~) is ignored
        self.assertTrue(self.handler._is_ignored("/some/path/file.txt~"))
        self.assertTrue(self.handler._is_ignored("another/document.doc~"))

    def test_is_not_ignored_regular_file(self):
        # Test that a regular file path is not ignored
        self.assertFalse(self.handler._is_ignored("/some/path/regular_file.txt"))
        self.assertFalse(self.handler._is_ignored("another/path/important_script.py"))

    def test_is_ignored_node_modules(self):
        # Test that a path within node_modules is ignored
        self.assertTrue(self.handler._is_ignored("/some/project/node_modules/library/file.js"))
        self.assertTrue(self.handler._is_ignored("node_modules/another-lib/index.js"))

    def test_is_ignored_ds_store(self):
        # Test .DS_Store file
        self.assertTrue(self.handler._is_ignored("/some/folder/.DS_Store"))
        self.assertTrue(self.handler._is_ignored(".DS_Store"))


class TestSyncProtocol(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.mock_db = MagicMock(spec=DaemonDatabase)
        self.base_dirs_config = {"test_dir": "/test_base"}
        # Mock the websocket
        self.mock_websocket = AsyncMock()

        # Mock time.time() for consistent uptime in PING response
        self.mock_time_patcher = patch("zync.daemon.time")
        self.mock_time = self.mock_time_patcher.start()
        self.mock_time.time.return_value = 1000  # A fixed start time for uptime calculation

        # Set the global START_TIME in the zync.daemon module for the test
        # This assumes handle_client uses a global `START_TIME` variable defined in zync.daemon
        self.original_start_time = getattr(zync.daemon, "START_TIME", None)
        zync.daemon.START_TIME = 0  # Assume daemon started at time 0 for uptime calculation

    async def tearDown(self):
        self.mock_time_patcher.stop()
        # Restore original start_time or remove if it wasn't there
        if self.original_start_time is not None:
            zync.daemon.START_TIME = self.original_start_time
        elif hasattr(zync.daemon, "START_TIME"):
            del zync.daemon.START_TIME

    async def test_ping_operation(self):
        ping_request = {"operation": "PING"}
        ping_request_json = json.dumps(ping_request)

        # Set up the websocket receive behavior
        self.mock_websocket.__aiter__.return_value = [ping_request_json]

        # Run the handler with a task that we'll cancel after the first message
        task = asyncio.create_task(handle_client(self.mock_websocket, self.base_dirs_config, self.mock_db))

        # Wait a short time for the handler to process the message
        await asyncio.sleep(0.1)

        # Cancel the task (simulating client disconnect)
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

        # Check that the send method was called once with the expected response
        self.mock_websocket.send.assert_called_once()
        response_json = self.mock_websocket.send.call_args[0][0]
        response = json.loads(response_json)
        self.assertEqual(response["status"], "ok")
        self.assertIn("server_time", response)
        self.assertEqual(response["uptime"], 1000)  # Expected uptime based on mocked time


if __name__ == "__main__":
    unittest.main()
