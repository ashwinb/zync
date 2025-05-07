"""
Utility for applying staged upstream changes on machine E.

This module provides a command-line interface to:
1. List staged changes received from machine R
2. Review and apply changes to the local filesystem
3. Handle conflicts between local and upstream changes
"""

import argparse
import difflib
import logging
import os
import shutil
import sys

from .database import DaemonDatabase

# Configure logging
logger = logging.getLogger(__name__)


def list_staged_sessions(db: DaemonDatabase, show_completed: bool = False) -> None:
    """
    List all staged sessions.

    Args:
        db: Database instance
        show_completed: Whether to show completed/applied sessions
    """
    sessions = db.get_staging_sessions()

    if not sessions:
        print("No staged sessions found.")
        return

    print(f"Found {len(sessions)} staged sessions:")
    print("-" * 80)
    print(f"{'ID':<36} {'Status':<12} {'Files':<6} {'Conflicts':<9} {'Created':<20}")
    print("-" * 80)

    for session in sessions:
        if not show_completed and session["status"] in ("applied", "aborted"):
            continue

        print(
            f"{session['staging_id']:<36} "
            f"{session['status']:<12} "
            f"{session['actual_file_count']:<6} "
            f"{session['conflict_count']:<9} "
            f"{session['created_timestamp']:<20}"
        )

    print("-" * 80)


def show_staged_session(db: DaemonDatabase, staging_id: str, base_dirs: dict[str, str]) -> None:
    """
    Show detailed information about a staged session.

    Args:
        db: Database instance
        staging_id: Session ID to show
        base_dirs: Mapping of base_dir names to absolute paths
    """
    # Get session info
    sessions = db.get_staging_sessions()
    session = next((s for s in sessions if s["staging_id"] == staging_id), None)

    if not session:
        print(f"Session {staging_id} not found")
        return

    print(f"Session: {staging_id}")
    print(f"Status: {session['status']}")
    print(f"Created: {session['created_timestamp']}")
    print(f"Files: {session['actual_file_count']} (Conflicts: {session['conflict_count']})")
    print("-" * 80)

    # Get files in this session
    files = db.get_staged_files(staging_id)

    if not files:
        print("No files in this session.")
        return

    print(f"{'Base Dir':<15} {'Path':<40} {'Size':<10} {'Conflict':<8}")
    print("-" * 80)

    for file in files:
        print(
            f"{file['base_dir']:<15} {file['path']:<40} {file['size']:<10} {'YES' if file['has_conflict'] else 'NO':<8}"
        )

    print("-" * 80)


def generate_diff(staged_path: str, target_path: str) -> str:
    """
    Generate a unified diff between the staged file and the target file.

    Args:
        staged_path: Path to the staged file
        target_path: Path to the target file

    Returns:
        Unified diff as a string
    """
    try:
        with open(staged_path) as f:
            staged_lines = f.readlines()
    except UnicodeDecodeError:
        return "[Binary file - diff not available]"
    except Exception as e:
        return f"[Error reading staged file: {str(e)}]"

    try:
        with open(target_path) as f:
            target_lines = f.readlines()
    except FileNotFoundError:
        return "[New file]"
    except UnicodeDecodeError:
        return "[Binary file - diff not available]"
    except Exception as e:
        return f"[Error reading target file: {str(e)}]"

    diff = difflib.unified_diff(
        target_lines,
        staged_lines,
        fromfile=f"current/{os.path.basename(target_path)}",
        tofile=f"upstream/{os.path.basename(target_path)}",
        lineterm="",
    )

    return "\n".join(diff)


def apply_session(
    db: DaemonDatabase, staging_id: str, base_dirs: dict[str, str], force: bool = False, interactive: bool = True
) -> bool:
    """
    Apply changes from a staged session.

    Args:
        db: Database instance
        staging_id: Session ID to apply
        base_dirs: Mapping of base_dir names to absolute paths
        force: Whether to force apply changes even with conflicts
        interactive: Whether to prompt for confirmation on each file

    Returns:
        True if successful, False otherwise
    """
    # Get session info
    sessions = db.get_staging_sessions()
    session = next((s for s in sessions if s["staging_id"] == staging_id), None)

    if not session:
        logger.error(f"Session {staging_id} not found")
        return False

    if session["status"] != "complete":
        logger.error(f"Cannot apply session with status: {session['status']}")
        return False

    # Check if there are conflicts
    if session["conflict_count"] > 0 and not force and not interactive:
        logger.error(
            f"Session has {session['conflict_count']} conflicts. "
            "Use --force to apply anyway or --interactive to resolve conflicts."
        )
        return False

    # Get staging directory path
    staging_dir = os.path.join(os.path.dirname(db.db_path), f".zync-staged-{staging_id}")

    if not os.path.exists(staging_dir):
        logger.error(f"Staging directory not found: {staging_dir}")
        return False

    # Get files in this session
    files = db.get_staged_files(staging_id)

    if not files:
        logger.error("No files in this session")
        return False

    # Apply each file
    applied_count = 0
    skipped_count = 0

    for file in files:
        base_dir = file["base_dir"]
        path = file["path"]

        if base_dir not in base_dirs:
            logger.warning(f"Skipping file with unknown base directory: {base_dir}")
            skipped_count += 1
            continue

        # Source and target paths
        source_path = os.path.join(staging_dir, base_dir, path)
        target_path = os.path.join(base_dirs[base_dir], path)

        if not os.path.exists(source_path):
            logger.warning(f"Source file not found: {source_path}")
            skipped_count += 1
            continue

        # If there's a conflict and we're in interactive mode
        skip_this_file = False
        if file["has_conflict"] and interactive:
            if os.path.exists(target_path):
                print("\n" + "=" * 80)
                print(f"CONFLICT: {base_dir}/{path}")
                print("=" * 80)

                # Show diff
                diff = generate_diff(source_path, target_path)
                print(diff)

                # Ask for action
                while True:
                    choice = input("\nApply this change? [y]es/[n]o/[d]iff/[q]uit: ").lower()

                    if choice in ("y", "yes"):
                        break
                    elif choice in ("n", "no"):
                        logger.info(f"Skipping {base_dir}/{path}")
                        skipped_count += 1
                        skip_this_file = True
                        break
                    elif choice in ("d", "diff"):
                        print("\n" + diff)
                    elif choice in ("q", "quit"):
                        logger.info("Application aborted by user")
                        return False
                    else:
                        print("Invalid choice")

        if skip_this_file:
            continue

        try:
            # Create target directory if it doesn't exist
            os.makedirs(os.path.dirname(target_path), exist_ok=True)

            # Copy the file
            shutil.copy2(source_path, target_path)
            logger.info(f"Applied {base_dir}/{path}")

            # Record the change if needed
            db.record_change("modify", path, base_dir, file["checksum"], file["size"])

            applied_count += 1
        except Exception as e:
            logger.error(f"Error applying {base_dir}/{path}: {str(e)}")
            skipped_count += 1

    # Mark session as applied
    db.mark_session_applied(staging_id, applied_count, skipped_count)

    logger.info(f"Applied {applied_count} files, skipped {skipped_count} files")

    return True


def main():
    """Main entry point for the apply tool"""
    parser = argparse.ArgumentParser(description="zync-apply - Apply upstream changes")

    # Common arguments
    parser.add_argument("--db", default="sync.db", help="SQLite database path")
    parser.add_argument(
        "--log-level", default="INFO", choices=["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"], help="Logging level"
    )

    # Subcommands
    subparsers = parser.add_subparsers(dest="command", help="Command to run")

    # List command
    list_parser = subparsers.add_parser("list", help="List staged sessions")
    list_parser.add_argument("--all", action="store_true", help="Show all sessions including applied/aborted")

    # Show command
    show_parser = subparsers.add_parser("show", help="Show details of a staged session")
    show_parser.add_argument("id", help="Session ID to show")
    show_parser.add_argument("--dirs", required=True, nargs="+", help="Directory mappings (format: name:path)")

    # Apply command
    apply_parser = subparsers.add_parser("apply", help="Apply a staged session")
    apply_parser.add_argument("id", help="Session ID to apply")
    apply_parser.add_argument("--dirs", required=True, nargs="+", help="Directory mappings (format: name:path)")
    apply_parser.add_argument("--force", action="store_true", help="Force apply even with conflicts")
    apply_parser.add_argument("--non-interactive", action="store_true", help="Do not prompt for confirmation")

    args = parser.parse_args()

    # Configure logging
    logging.basicConfig(
        level=getattr(logging, args.log_level),
        format="%(asctime)s - %(levelname)s - %(message)s",
        handlers=[logging.StreamHandler(), logging.FileHandler("zync_apply.log")],
    )

    # Initialize database
    db_path = os.path.abspath(args.db)
    db = DaemonDatabase(db_path)

    if args.command == "list":
        list_staged_sessions(db, args.all)

    elif args.command == "show":
        # Parse directory mappings
        base_dirs = {}
        for dir_spec in args.dirs:
            try:
                name, path = dir_spec.split(":", 1)
                abs_path = os.path.abspath(path)
                base_dirs[name] = abs_path
            except ValueError:
                logger.error(f"Invalid directory mapping: {dir_spec}, use format name:path")

        if not base_dirs:
            logger.error("No valid directory mappings")
            return 1

        show_staged_session(db, args.id, base_dirs)

    elif args.command == "apply":
        # Parse directory mappings
        base_dirs = {}
        for dir_spec in args.dirs:
            try:
                name, path = dir_spec.split(":", 1)
                abs_path = os.path.abspath(path)
                base_dirs[name] = abs_path
            except ValueError:
                logger.error(f"Invalid directory mapping: {dir_spec}, use format name:path")

        if not base_dirs:
            logger.error("No valid directory mappings")
            return 1

        success = apply_session(db, args.id, base_dirs, force=args.force, interactive=not args.non_interactive)

        return 0 if success else 1

    else:
        parser.print_help()

    return 0


if __name__ == "__main__":
    sys.exit(main())
