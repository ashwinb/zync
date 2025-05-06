import unittest
from unittest.mock import MagicMock

from zync.daemon import FileChangeHandler
from zync.database import DaemonDatabase  # Actual import for type hinting, will be mocked


class TestFileChangeHandler(unittest.TestCase):
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


if __name__ == "__main__":
    unittest.main()
