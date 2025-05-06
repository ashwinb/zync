import hashlib
import json
import os
import unittest
from unittest.mock import AsyncMock, MagicMock, mock_open, patch

from zync.client import FileSyncClient, parse_directory_mappings
from zync.database import ClientDatabase  # Actual import for type hinting, will be mocked


class TestFileSyncClient(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        # Create the database mock
        self.mock_db = MagicMock(spec=ClientDatabase)

        # Set up some test data
        self.server_url = "ws://test.server:8765"
        self.sync_dirs = {"remote_dir": "/local/test_dir"}
        self.db_path = "/path/to/test.db"

        # Create client with mocked database constructor
        with patch("zync.client.ClientDatabase") as mock_db_class:
            mock_db_class.return_value = self.mock_db
            self.client = FileSyncClient(self.server_url, self.sync_dirs, self.db_path)

        # Create a mock websocket
        self.mock_websocket = AsyncMock()

    async def test_send_request(self):
        # Set up the mock
        self.mock_websocket.recv.return_value = json.dumps({"status": "ok", "data": "test"})

        # Call the method
        response = await self.client.send_request(self.mock_websocket, "TEST_OP", param1="value1")

        # Check results
        self.mock_websocket.send.assert_called_once()
        send_data = json.loads(self.mock_websocket.send.call_args[0][0])
        self.assertEqual(send_data["operation"], "TEST_OP")
        self.assertEqual(send_data["param1"], "value1")
        self.assertEqual(response, json.dumps({"status": "ok", "data": "test"}))

    async def test_get_last_sequence(self):
        # Set up the mock
        self.mock_websocket.recv.return_value = json.dumps({"status": "ok", "last_sequence": 42})

        # Call the method
        result = await self.client.get_last_sequence(self.mock_websocket)

        # Check results
        self.assertEqual(result, 42)
        send_data = json.loads(self.mock_websocket.send.call_args[0][0])
        self.assertEqual(send_data["operation"], "GET_LAST_SEQUENCE")

    async def test_get_last_sequence_error(self):
        # Set up the mock to return an error
        self.mock_websocket.recv.return_value = json.dumps({"status": "error", "error": "Test error"})

        # Call the method
        result = await self.client.get_last_sequence(self.mock_websocket)

        # Check results - should return 0 on error
        self.assertEqual(result, 0)

    async def test_get_changes(self):
        # Set up the mock
        changes = [
            {"sequence": 1, "operation": "add", "path": "file1.txt", "base_dir": "remote_dir"},
            {"sequence": 2, "operation": "modify", "path": "file2.txt", "base_dir": "remote_dir"},
        ]
        self.mock_websocket.recv.return_value = json.dumps({"status": "ok", "changes": changes, "more_available": True})

        # Call the method
        result_changes, more_available = await self.client.get_changes(self.mock_websocket, 0, 10)

        # Check results
        self.assertEqual(result_changes, changes)
        self.assertTrue(more_available)
        send_data = json.loads(self.mock_websocket.send.call_args[0][0])
        self.assertEqual(send_data["operation"], "GET_CHANGES")
        self.assertEqual(send_data["since_sequence"], 0)
        self.assertEqual(send_data["limit"], 10)

    async def test_get_changes_error(self):
        # Set up the mock to return an error
        self.mock_websocket.recv.return_value = json.dumps({"status": "error", "error": "Test error"})

        # Call the method
        result_changes, more_available = await self.client.get_changes(self.mock_websocket, 0)

        # Check results - should return empty list and False on error
        self.assertEqual(result_changes, [])
        self.assertFalse(more_available)

    async def test_get_file(self):
        # Create test file data
        file_data = b"test file content"
        file_checksum = hashlib.md5(file_data).hexdigest()

        # Set up the mocks
        self.mock_websocket.recv.side_effect = [
            json.dumps({"status": "ok", "checksum": file_checksum, "size": len(file_data)}),
            file_data,
        ]

        # Call the method
        result_data, result_metadata = await self.client.get_file(self.mock_websocket, "remote_dir", "test_file.txt")

        # Check results
        self.assertEqual(result_data, file_data)
        self.assertEqual(result_metadata["checksum"], file_checksum)
        self.assertEqual(result_metadata["size"], len(file_data))
        send_data = json.loads(self.mock_websocket.send.call_args[0][0])
        self.assertEqual(send_data["operation"], "GET_FILE")
        self.assertEqual(send_data["base_dir"], "remote_dir")
        self.assertEqual(send_data["path"], "test_file.txt")

    async def test_get_file_checksum_mismatch(self):
        # Create test file data with incorrect checksum
        file_data = b"test file content"
        wrong_checksum = "wrong_checksum"

        # Set up the mocks
        self.mock_websocket.recv.side_effect = [
            json.dumps({"status": "ok", "checksum": wrong_checksum, "size": len(file_data)}),
            file_data,
        ]

        # Call the method
        result_data, result_metadata = await self.client.get_file(self.mock_websocket, "remote_dir", "test_file.txt")

        # Check results - should return None on checksum mismatch
        self.assertIsNone(result_data)
        self.assertIsNone(result_metadata)

    async def test_get_file_error(self):
        # Set up the mock to return an error
        self.mock_websocket.recv.return_value = json.dumps({"status": "error", "error": "File not found"})

        # Call the method
        result_data, result_metadata = await self.client.get_file(self.mock_websocket, "remote_dir", "nonexistent.txt")

        # Check results - should return None on error
        self.assertIsNone(result_data)
        self.assertIsNone(result_metadata)

    async def test_acknowledge_changes(self):
        # Set up the mock
        self.mock_websocket.recv.return_value = json.dumps({"status": "ok"})

        # Call the method
        result = await self.client.acknowledge_changes(self.mock_websocket, 42)

        # Check results
        self.assertTrue(result)
        send_data = json.loads(self.mock_websocket.send.call_args[0][0])
        self.assertEqual(send_data["operation"], "ACK_CHANGES")
        self.assertEqual(send_data["sequence_number"], 42)

    async def test_acknowledge_changes_error(self):
        # Set up the mock to return an error
        self.mock_websocket.recv.return_value = json.dumps({"status": "error", "error": "Test error"})

        # Call the method
        result = await self.client.acknowledge_changes(self.mock_websocket, 42)

        # Check results - should return False on error
        self.assertFalse(result)

    @patch("os.path.exists")
    @patch("os.path.isfile")
    @patch("os.unlink")
    async def test_apply_change_delete(self, mock_unlink, mock_isfile, mock_exists):
        # Set up mocks
        mock_exists.return_value = True
        mock_isfile.return_value = True

        # Create a delete change
        change = {"operation": "delete", "path": "file_to_delete.txt", "base_dir": "remote_dir"}

        # Call the method
        result = await self.client.apply_change(self.mock_websocket, change)

        # Check results
        self.assertTrue(result)
        mock_exists.assert_called_once_with(os.path.join("/local/test_dir", "file_to_delete.txt"))
        mock_isfile.assert_called_once_with(os.path.join("/local/test_dir", "file_to_delete.txt"))
        mock_unlink.assert_called_once_with(os.path.join("/local/test_dir", "file_to_delete.txt"))

    @patch("os.makedirs")
    @patch("builtins.open", new_callable=mock_open)
    async def test_apply_change_add(self, mock_file_open, mock_makedirs):
        # Create test file data
        file_data = b"test file content"
        file_checksum = hashlib.md5(file_data).hexdigest()

        # Mock the get_file method
        self.client.get_file = AsyncMock(return_value=(file_data, {"checksum": file_checksum, "size": len(file_data)}))

        # Create an add change
        change = {
            "operation": "add",
            "path": "new_file.txt",
            "base_dir": "remote_dir",
            "checksum": file_checksum,
            "size": len(file_data),
        }

        # Call the method
        result = await self.client.apply_change(self.mock_websocket, change)

        # Check results
        self.assertTrue(result)
        mock_makedirs.assert_called_once_with(
            os.path.dirname(os.path.join("/local/test_dir", "new_file.txt")), exist_ok=True
        )
        mock_file_open.assert_called_once_with(os.path.join("/local/test_dir", "new_file.txt"), "wb")
        mock_file_open().write.assert_called_once_with(file_data)
        self.client.get_file.assert_called_once_with(self.mock_websocket, "remote_dir", "new_file.txt")

    @patch("os.makedirs")
    @patch("builtins.open", new_callable=mock_open)
    async def test_apply_change_modify(self, mock_file_open, mock_makedirs):
        # Create test file data
        file_data = b"updated file content"
        file_checksum = hashlib.md5(file_data).hexdigest()

        # Mock the get_file method
        self.client.get_file = AsyncMock(return_value=(file_data, {"checksum": file_checksum, "size": len(file_data)}))

        # Create a modify change
        change = {
            "operation": "modify",
            "path": "existing_file.txt",
            "base_dir": "remote_dir",
            "checksum": file_checksum,
            "size": len(file_data),
        }

        # Call the method
        result = await self.client.apply_change(self.mock_websocket, change)

        # Check results
        self.assertTrue(result)
        mock_makedirs.assert_called_once_with(
            os.path.dirname(os.path.join("/local/test_dir", "existing_file.txt")), exist_ok=True
        )
        mock_file_open.assert_called_once_with(os.path.join("/local/test_dir", "existing_file.txt"), "wb")
        mock_file_open().write.assert_called_once_with(file_data)
        self.client.get_file.assert_called_once_with(self.mock_websocket, "remote_dir", "existing_file.txt")

    async def test_apply_change_unsupported_operation(self):
        # Create a change with an unsupported operation
        change = {"operation": "unsupported_op", "path": "file.txt", "base_dir": "remote_dir"}

        # Call the method
        result = await self.client.apply_change(self.mock_websocket, change)

        # Check results - should return False for unsupported operations
        self.assertFalse(result)

    @patch.object(FileSyncClient, "connect")
    @patch.object(FileSyncClient, "get_last_sequence")
    @patch.object(FileSyncClient, "get_changes")
    @patch.object(FileSyncClient, "apply_change")
    @patch.object(FileSyncClient, "acknowledge_changes")
    async def test_sync_changes(self, mock_ack, mock_apply, mock_get_changes, mock_get_seq, mock_connect):
        # Set up mocks
        mock_websocket = AsyncMock()
        mock_connect.return_value.__aenter__.return_value = mock_websocket

        # Mock database to return sequence 10
        self.mock_db.get_last_sequence.return_value = 10

        # Mock server to return sequence 12
        mock_get_seq.return_value = 12

        # Set up changes
        changes = [
            {"sequence": 11, "operation": "add", "path": "file1.txt", "base_dir": "remote_dir"},
            {"sequence": 12, "operation": "modify", "path": "file2.txt", "base_dir": "remote_dir"},
        ]
        mock_get_changes.return_value = (changes, False)

        # Mock apply_change to succeed
        mock_apply.return_value = True

        # Call the method
        result = await self.client.sync_changes()

        # Check results
        self.assertTrue(result)
        mock_get_seq.assert_called_once_with(mock_websocket)
        mock_get_changes.assert_called_once_with(mock_websocket, 10, limit=100)
        self.assertEqual(mock_apply.call_count, 2)
        mock_ack.assert_called_once_with(mock_websocket, 12)
        self.mock_db.update_last_sequence.assert_called_once_with(12)

    @patch.object(FileSyncClient, "connect")
    async def test_ping_server(self, mock_connect):
        # Set up mocks
        mock_websocket = AsyncMock()
        mock_connect.return_value.__aenter__.return_value = mock_websocket
        mock_websocket.recv.return_value = json.dumps(
            {"status": "ok", "server_time": "2023-09-01T12:00:00", "uptime": 3600}
        )

        # Call the method
        result = await self.client.ping_server()

        # Check results
        self.assertTrue(result)
        send_data = json.loads(mock_websocket.send.call_args[0][0])
        self.assertEqual(send_data["operation"], "PING")


class TestDirectoryMappings(unittest.TestCase):
    @patch("os.path.abspath")
    @patch("zync.client.ensure_directory")
    def test_parse_directory_mappings(self, mock_ensure_dir, mock_abspath):
        # Set up mocks
        mock_abspath.side_effect = lambda p: f"/abs/{p}"

        # Test valid mappings
        dir_specs = ["remote1:local1", "remote2:local2"]
        result = parse_directory_mappings(dir_specs)

        # Check results
        self.assertEqual(result, {"remote1": "/abs/local1", "remote2": "/abs/local2"})
        self.assertEqual(mock_ensure_dir.call_count, 2)
        mock_ensure_dir.assert_any_call("/abs/local1")
        mock_ensure_dir.assert_any_call("/abs/local2")

    @patch("os.path.abspath")
    @patch("zync.client.ensure_directory")
    def test_parse_directory_mappings_invalid(self, mock_ensure_dir, mock_abspath):
        # Set up mocks
        mock_abspath.side_effect = lambda p: f"/abs/{p}"

        # Test invalid mapping
        dir_specs = ["remote1:local1", "invalid_format"]
        result = parse_directory_mappings(dir_specs)

        # Check results - should only include valid mappings
        self.assertEqual(result, {"remote1": "/abs/local1"})
        mock_ensure_dir.assert_called_once_with("/abs/local1")


if __name__ == "__main__":
    unittest.main()
