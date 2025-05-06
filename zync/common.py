"""
Common utilities and helper functions for FileSync.
"""

import os
import time
import hashlib
import logging
from pathlib import Path
from typing import Optional, Dict, List, Tuple, Any, Union

# Configure logging
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

def calculate_checksum(file_path: str) -> Optional[str]:
    """
    Calculate MD5 checksum of a file.
    
    Args:
        file_path: Path to the file
        
    Returns:
        MD5 checksum as a hexadecimal string, or None if file cannot be read
    """
    try:
        with open(file_path, 'rb') as f:
            return hashlib.md5(f.read()).hexdigest()
    except Exception as e:
        logger.error(f"Error calculating checksum for {file_path}: {e}")
        return None

def get_file_info(file_path: str) -> Dict[str, Any]:
    """
    Get file information including size, modification time, and checksum.
    
    Args:
        file_path: Path to the file
        
    Returns:
        Dictionary with file information
    """
    try:
        stat = os.stat(file_path)
        return {
            'size': stat.st_size,
            'modified_time': int(stat.st_mtime),
            'checksum': calculate_checksum(file_path)
        }
    except Exception as e:
        logger.error(f"Error getting file info for {file_path}: {e}")
        return {
            'size': 0,
            'modified_time': 0,
            'checksum': None
        }

def normalize_path(path: str) -> str:
    """
    Normalize a file path to use standard separators and remove redundancies.
    
    Args:
        path: Path to normalize
        
    Returns:
        Normalized path string
    """
    return os.path.normpath(path).replace('\\', '/')

def is_subpath(parent: str, child: str) -> bool:
    """
    Check if child path is a subpath of parent path.
    
    Args:
        parent: Parent directory path
        child: Potential child path
        
    Returns:
        True if child is a subpath of parent, False otherwise
    """
    parent = os.path.abspath(parent)
    child = os.path.abspath(child)
    
    return child == parent or child.startswith(parent + os.sep)

def get_relative_path(base_path: str, full_path: str) -> Optional[str]:
    """
    Get path relative to a base directory.
    
    Args:
        base_path: Base directory path
        full_path: Full path to convert
        
    Returns:
        Relative path, or None if full_path is not under base_path
    """
    if not is_subpath(base_path, full_path):
        return None
    
    return os.path.relpath(full_path, base_path)

def ensure_directory(directory: str) -> bool:
    """
    Ensure a directory exists, creating it if necessary.
    
    Args:
        directory: Directory path
        
    Returns:
        True if directory exists or was created, False on error
    """
    try:
        os.makedirs(directory, exist_ok=True)
        return True
    except Exception as e:
        logger.error(f"Error creating directory {directory}: {e}")
        return False

def timestamp_now() -> int:
    """
    Get current Unix timestamp.
    
    Returns:
        Current time as Unix timestamp (seconds since epoch)
    """
    return int(time.time())

def format_size(size_bytes: int) -> str:
    """
    Format file size in human-readable format.
    
    Args:
        size_bytes: Size in bytes
        
    Returns:
        Formatted size string (e.g., "1.23 MB")
    """
    if size_bytes < 1024:
        return f"{size_bytes} B"
    elif size_bytes < 1024 * 1024:
        return f"{size_bytes / 1024:.2f} KB"
    elif size_bytes < 1024 * 1024 * 1024:
        return f"{size_bytes / (1024 * 1024):.2f} MB"
    else:
        return f"{size_bytes / (1024 * 1024 * 1024):.2f} GB"
