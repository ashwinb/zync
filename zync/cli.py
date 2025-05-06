"""
Command-line interface for the Zync file synchronization tool.

This module provides a unified command-line interface for both
the daemon and client components of Zync.
"""

import argparse
import logging

from . import client, daemon

logger = logging.getLogger(__name__)


def setup_logging(log_level: str, log_file: str | None = None) -> None:
    """
    Set up logging configuration.

    Args:
        log_level: Logging level (DEBUG, INFO, etc.)
        log_file: Optional log file path
    """
    handlers = [logging.StreamHandler()]

    if log_file:
        handlers.append(logging.FileHandler(log_file))

    logging.basicConfig(
        level=getattr(logging, log_level),
        format="%(asctime)s - %(levelname)s - %(name)s - %(message)s",
        handlers=handlers,
    )


def daemon_command(args: argparse.Namespace) -> int:
    """
    Run the daemon command.

    Args:
        args: Command-line arguments

    Returns:
        Exit code
    """
    setup_logging(args.log_level, args.log_file)
    return daemon.main()


def client_command(args: argparse.Namespace) -> int:
    """
    Run the client command.

    Args:
        args: Command-line arguments

    Returns:
        Exit code
    """
    setup_logging(args.log_level, args.log_file)
    return client.main()


def status_command(args: argparse.Namespace) -> int:
    """
    Run the status command.

    Args:
        args: Command-line arguments

    Returns:
        Exit code
    """
