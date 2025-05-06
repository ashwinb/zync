# zync

A reliable file synchronization system for sync between machines through a reverse tunnel.

## Overview

zync is designed for scenarios where you need to sync files between two machines (E and R) through a reverse tunnel, with machine R initiating connections. The system uses WebSockets for communication and SQLite for persistent state management.

Key features:

- **Bidirectional sync**:
  - Primary direction: Changes on machine E are synchronized to machine R (automatic)
  - Secondary direction: Changes on machine R can be pushed upstream to machine E (on demand)
- **Conflict resolution**: Conflicts are detected and can be resolved interactively
- **Reliable tracking**: All changes are recorded with sequence numbers for consistent syncing
- **Efficient transfer**: Only changed files are transferred, with checksum verification
- **Fault tolerance**: Can recover from network issues or daemon restarts

## Components

1. **Daemon (Machine E)**:
   - Monitors directories for file changes
   - Records changes in SQLite database
   - Serves changes through WebSocket API
   - Receives and stages upstream changes from machine R

2. **Client (Machine R)**:
   - Connects to daemon through reverse tunnel
   - Pulls changes since last sync
   - Applies changes to local filesystem
   - Can send local changes upstream to machine E

3. **Apply Tool (Machine E)**:
   - Interactive tool to review and apply upstream changes
   - Detects conflicts with local changes
   - Provides diff viewing and conflict resolution

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
# Start the client for normal syncing
zync-client --server ws://localhost:8000 --dirs docs:/home/user/synced/docs code:/home/user/synced/code --interval 30

# Send upstream changes to machine E
zync-client --server ws://localhost:8000 --dirs docs:/home/user/synced/docs code:/home/user/synced/code --update-upstream
```

Configuration options:
- `--server`: WebSocket server URL
- `--db`: SQLite database path (default: sync_client.db)
- `--interval`: Sync interval in seconds (default: 60)
- `--dirs`: Directory mappings, format: `remote_name:local_path`
- `--once`: Run once and exit (default: continuous sync)
- `--update-upstream`: Send local changes upstream to machine E

### Apply Tool (Machine E)

```bash
# List staged changes from machine R
zync-apply --db sync.db list

# Show details of a specific staged session
zync-apply --db sync.db show SESSION_ID --dirs docs:/home/user/documents code:/home/user/projects

# Apply changes interactively
zync-apply --db sync.db apply SESSION_ID --dirs docs:/home/user/documents code:/home/user/projects

# Apply all changes non-interactively (even those with conflicts)
zync-apply --db sync.db apply SESSION_ID --dirs docs:/home/user/documents code:/home/user/projects --force --non-interactive
```

Configuration options:
- `--db`: SQLite database path (default: sync.db)
- `list`: List all pending staged sessions
- `show`: View details of a specific session
- `apply`: Apply changes from a staged session
- `--force`: Apply changes without stopping for conflicts
- `--non-interactive`: Don't prompt for confirmation on each file

## Bidirectional Sync Workflow

### Default Direction (E -> R)
1. Machine E's daemon monitors files for changes
2. Machine R's client connects and pulls changes automatically

### Reverse Direction (R -> E)
1. Make changes on machine R
2. Run `zync-client --update-upstream` to send changes to machine E
3. On machine E, run `zync-apply list` to see pending changes
4. View details with `zync-apply show SESSION_ID`
5. Apply changes with `zync-apply apply SESSION_ID`
6. Resolve any conflicts interactively

## Directory Structure

```
zync/
├── __init__.py
├── daemon.py         # Daemon implementation
├── client.py         # Client implementation
├── apply_upstream.py # Tool for applying upstream changes
├── common.py         # Shared utilities
└── database.py       # Database operations

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
- `PREPARE_UPSTREAM`: Start an upstream transfer session
- `UPSTREAM_FILE`: Send a file upstream
- `FINALIZE_UPSTREAM`: Complete an upstream session

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
