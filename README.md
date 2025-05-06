# zync

A simple, reliable file synchronization system for one-way sync between machines through a reverse tunnel.

## Overview

zync is designed for scenarios where you need to sync files from one machine (E) to another (R) through a reverse tunnel, with machine R initiating connections. The system uses WebSockets for communication and SQLite for persistent state management.

Key features:

- **One-way sync**: Changes on machine E are synchronized to machine R
- **Conflict resolution**: Machine E changes always win in case of conflicts
- **Reliable tracking**: All changes are recorded with sequence numbers for consistent syncing
- **Efficient transfer**: Only changed files are transferred, with checksum verification
- **Fault tolerance**: Can recover from network issues or daemon restarts

## Components

1. **Daemon (Machine E)**: 
   - Monitors directories for file changes
   - Records changes in SQLite database
   - Serves changes through WebSocket API

2. **Client (Machine R)**:
   - Connects to daemon through reverse tunnel
   - Pulls changes since last sync
   - Applies changes to local filesystem

## Installation

```bash
# Install from source
git clone https://github.com/ashwinb/zync.git
cd zync
pip install -e .

# Or install from PyPI
pip install zync
```

## Usage

### Daemon (Machine E)

```bash
# Start the daemon
zync-daemon --host localhost --port 8000 --dirs docs:/home/user/documents code:/home/user/projects
```

Configuration options:
- `--host`: Hostname to bind to (default: localhost)
- `--port`: Port to listen on (default: 8000)
- `--db`: SQLite database path (default: sync.db)
- `--dirs`: Directories to monitor, format: `name:path`

### Client (Machine R)

```bash
# Start the client
zync-client --server ws://localhost:8000 --dirs docs:/home/user/synced/docs code:/home/user/synced/code --interval 30
```

Configuration options:
- `--server`: WebSocket server URL
- `--db`: SQLite database path (default: sync_client.db)
- `--interval`: Sync interval in seconds (default: 60)
- `--dirs`: Directory mappings, format: `remote_name:local_path`
- `--once`: Run once and exit (default: continuous sync)

## Directory Structure

```
zync/
├── __init__.py
├── daemon.py      # Daemon implementation
├── client.py      # Client implementation
├── common.py      # Shared utilities
└── database.py    # Database operations

tests/
├── __init__.py
├── test_daemon.py
├── test_client.py
└── test_common.py
```

## Protocol

The sync protocol uses WebSockets with JSON messages:

- `PING`: Check server connectivity
- `GET_LAST_SEQUENCE`: Get latest sequence number
- `GET_CHANGES`: Retrieve changes since a sequence
- `GET_FILE`: Download file content
- `ACK_CHANGES`: Acknowledge processed changes

## Development

Setup development environment:

```bash
pip install -e ".[dev]"
```

Run tests:

```bash
pytest
```

Format code:

```bash
black zync tests
isort zync tests
```

## License

This project is licensed under the MIT License - see the LICENSE file for details.
