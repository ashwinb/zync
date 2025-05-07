import os
import shutil
import subprocess
import tempfile
import time
import unittest
from unittest.mock import patch


class TestCommandLineTools(unittest.TestCase):
    """
    Integration tests for command-line tools.

    These tests spawn actual processes for the daemon, client, and apply tools
    to test their integration in a realistic scenario.
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

        # Create database paths
        self.daemon_db_path = os.path.join(self.temp_dir, "daemon.db")
        self.client_db_path = os.path.join(self.temp_dir, "client.db")

        # Daemon and client processes
        self.daemon_process = None
        self.client_process = None

        # WebSocket server settings
        self.host = "localhost"
        self.port = 8765
        self.server_url = f"ws://{self.host}:{self.port}"

    def tearDown(self):
        # Kill any running processes
        if self.daemon_process and self.daemon_process.poll() is None:
            self.daemon_process.terminate()
            self.daemon_process.wait(timeout=5)

        if self.client_process and self.client_process.poll() is None:
            self.client_process.terminate()
            self.client_process.wait(timeout=5)

        # Clean up temporary directory
        shutil.rmtree(self.temp_dir)

    def create_file(self, path, content):
        """Create a test file with the given content"""
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w") as f:
            f.write(content)

    def read_file(self, path):
        """Read a file and return its content"""
        with open(path) as f:
            return f.read()

    def assert_files_equal(self, path1, path2):
        """Assert that two files have the same content"""
        content1 = self.read_file(path1)
        content2 = self.read_file(path2)
        self.assertEqual(content1, content2)

    def start_daemon(self):
        """Start the daemon process"""
        daemon_cmd = [
            "python",
            "-m",
            "zync.daemon",
            "--host",
            self.host,
            "--port",
            str(self.port),
            "--db",
            self.daemon_db_path,
            "--dirs",
            f"docs:{self.machine_e_docs}",
            f"code:{self.machine_e_code}",
            "--log-level",
            "DEBUG",
        ]

        self.daemon_process = subprocess.Popen(daemon_cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)

        # Wait for daemon to start
        time.sleep(2)

        # Check if daemon is running
        if self.daemon_process.poll() is not None:
            stdout, stderr = self.daemon_process.communicate()
            raise RuntimeError(f"Daemon failed to start: {stderr.decode()}")

    def start_client_sync(self):
        """Start the client process in sync mode"""
        client_cmd = [
            "python",
            "-m",
            "zync.client",
            "--server",
            self.server_url,
            "--db",
            self.client_db_path,
            "--dirs",
            f"docs:{self.machine_r_docs}",
            f"code:{self.machine_r_code}",
            "--once",
            "--log-level",
            "DEBUG",
        ]

        self.client_process = subprocess.Popen(client_cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)

        # Wait for client to complete
        self.client_process.wait(timeout=30)

        # Check return code
        if self.client_process.returncode != 0:
            stdout, stderr = self.client_process.communicate()
            raise RuntimeError(f"Client sync failed: {stderr.decode()}")

    def run_client_upstream(self):
        """Run the client in upstream mode"""
        client_cmd = [
            "python",
            "-m",
            "zync.client",
            "--server",
            self.server_url,
            "--db",
            self.client_db_path,
            "--dirs",
            f"docs:{self.machine_r_docs}",
            f"code:{self.machine_r_code}",
            "--update-upstream",
            "--log-level",
            "DEBUG",
        ]

        result = subprocess.run(
            client_cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=True,  # Raises exception on non-zero return code
        )

        return result

    def list_staging_sessions(self):
        """Run the apply tool to list staging sessions"""
        apply_cmd = ["python", "-m", "zync.apply_upstream", "--db", self.daemon_db_path, "list"]

        result = subprocess.run(apply_cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=True)

        return result.stdout.decode()

    def apply_staging_session(self, staging_id, force=False, interactive=False):
        """Run the apply tool to apply a staging session"""
        apply_cmd = [
            "python",
            "-m",
            "zync.apply_upstream",
            "--db",
            self.daemon_db_path,
            "apply",
            staging_id,
            "--dirs",
            f"docs:{self.machine_e_docs}",
            f"code:{self.machine_e_code}",
        ]

        if force:
            apply_cmd.append("--force")

        if not interactive:
            apply_cmd.append("--non-interactive")

        result = subprocess.run(apply_cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=True)

        return result

    def extract_staging_id(self, list_output):
        """Extract the staging ID from the list output"""
        lines = list_output.strip().split("\n")
        for line in lines:
            parts = line.split()
            if len(parts) >= 1 and len(parts[0]) == 36:  # UUID length
                return parts[0]
        return None

    @unittest.skip("This test starts actual processes, only run manually")
    def test_end_to_end_workflow(self):
        """Test the end-to-end workflow with actual CLI processes"""
        try:
            # Start the daemon
            self.start_daemon()

            # STEP 1: Create files on machine E
            self.create_file(os.path.join(self.machine_e_docs, "e_doc.txt"), "Document from E")
            self.create_file(os.path.join(self.machine_e_code, "e_script.py"), "print('From E')")

            # Wait for files to be detected
            time.sleep(2)

            # STEP 2: Run client to sync from E to R
            self.start_client_sync()

            # Verify files synced to machine R
            self.assert_files_equal(
                os.path.join(self.machine_e_docs, "e_doc.txt"), os.path.join(self.machine_r_docs, "e_doc.txt")
            )
            self.assert_files_equal(
                os.path.join(self.machine_e_code, "e_script.py"), os.path.join(self.machine_r_code, "e_script.py")
            )

            # STEP 3: Create files on machine R
            self.create_file(os.path.join(self.machine_r_docs, "r_doc.txt"), "Document from R")
            self.create_file(os.path.join(self.machine_r_code, "r_script.py"), "print('From R')")

            # STEP 4: Run upstream sync
            self.run_client_upstream()

            # STEP 5: List staging sessions
            list_output = self.list_staging_sessions()
            staging_id = self.extract_staging_id(list_output)
            self.assertIsNotNone(staging_id, "No staging session found")

            # STEP 6: Apply changes
            self.apply_staging_session(staging_id, force=True)

            # Verify files synced to machine E
            self.assert_files_equal(
                os.path.join(self.machine_r_docs, "r_doc.txt"), os.path.join(self.machine_e_docs, "r_doc.txt")
            )
            self.assert_files_equal(
                os.path.join(self.machine_r_code, "r_script.py"), os.path.join(self.machine_e_code, "r_script.py")
            )

            # STEP 7: Create more files on machine E
            self.create_file(os.path.join(self.machine_e_docs, "e_doc2.txt"), "Second document from E")

            # Wait for files to be detected
            time.sleep(2)

            # STEP 8: Run client to sync again
            self.start_client_sync()

            # Verify all files now exist on both sides
            self.assert_files_equal(
                os.path.join(self.machine_e_docs, "e_doc2.txt"), os.path.join(self.machine_r_docs, "e_doc2.txt")
            )

        except Exception as e:
            # If daemon process is running, capture output for debugging
            if self.daemon_process and self.daemon_process.poll() is None:
                stdout, stderr = self.daemon_process.communicate()
                print(f"Daemon stdout: {stdout.decode()}")
                print(f"Daemon stderr: {stderr.decode()}")
            raise e

    @patch("builtins.input")
    @unittest.skip("This test starts actual processes, only run manually")
    def test_interactive_conflict_resolution(self, mock_input):
        """Test interactive conflict resolution with actual CLI processes"""
        try:
            # Start the daemon
            self.start_daemon()

            # STEP 1: Create a file on machine E
            self.create_file(os.path.join(self.machine_e_docs, "conflict.txt"), "Version from E")

            # Wait for files to be detected
            time.sleep(2)

            # STEP 2: Run client to sync from E to R
            self.start_client_sync()

            # STEP 3: Modify the file on both machines
            self.create_file(os.path.join(self.machine_e_docs, "conflict.txt"), "Modified on E")
            self.create_file(os.path.join(self.machine_r_docs, "conflict.txt"), "Modified on R")

            # Wait for E's changes to be detected
            time.sleep(2)

            # STEP 4: Run upstream sync from R to E
            self.run_client_upstream()

            # STEP 5: List staging sessions
            list_output = self.list_staging_sessions()
            staging_id = self.extract_staging_id(list_output)
            self.assertIsNotNone(staging_id, "No staging session found")

            # STEP 6: Set up input mock to simulate user choosing 'y' (accept)
            mock_input.return_value = "y"

            # STEP 7: Apply changes interactively
            self.apply_staging_session(staging_id, interactive=True)

            # STEP 8: Verify R's version was applied to E
            self.assertEqual(self.read_file(os.path.join(self.machine_e_docs, "conflict.txt")), "Modified on R")

        except Exception as e:
            # If daemon process is running, capture output for debugging
            if self.daemon_process and self.daemon_process.poll() is None:
                stdout, stderr = self.daemon_process.communicate()
                print(f"Daemon stdout: {stdout.decode()}")
                print(f"Daemon stderr: {stderr.decode()}")
            raise e


if __name__ == "__main__":
    unittest.main()
