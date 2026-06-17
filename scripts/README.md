# scripts/

Developer and operator utilities. All scripts auto-discover credentials from the database — no manual key management needed.

## Host Access

| Script | Usage | Description |
|--------|-------|-------------|
| `host-ssh.sh` | `./host-ssh.sh <id-prefix> [cmd]` | SSH into a host by ID prefix. Use `--list` to see all hosts, `pb` for the pattern buffer host |
| `host-db.sh` | `./host-db.sh ["code"]` | Interactive Python shell with a DB session and all models imported. Pass inline code as an argument for one-shot queries |
| `bastion-exec.sh` | `./bastion-exec.sh [project-id] [cmd]` | Execute commands on a bastion VM via the serial exec API. No arguments lists projects with bastion VMs |

## Agent Management

| Script | Usage | Description |
|--------|-------|-------------|
| `update-agent.sh` | `./update-agent.sh [host-id] [--force]` | Push troshkad update via the API (fast, no SSH). Updates all connected hosts if no ID given |
| `reinstall-agent.sh` | `./reinstall-agent.sh [host-id]` | Full SSH reinstall of the agent — use for broken agents or first-time setup |

## Testing

| Script | Usage | Description |
|--------|-------|-------------|
| `test-agnosticd-flow.sh` | `./test-agnosticd-flow.sh <pattern> [guid]` | End-to-end test simulating the AAP2/agnosticd deploy flow. Supports `--destroy`, `--status`, `--stop`, `--start` lifecycle commands |
