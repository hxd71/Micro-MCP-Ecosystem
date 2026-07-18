# Native Linux systemd acceptance

The `native Ubuntu 22.04 systemd` GitHub Actions job runs on a clean GitHub-hosted Ubuntu 22.04
virtual machine with systemd as PID 1. It complements, but does not replace, the physical RTX 3050
WSL2 Docker/NVIDIA/vLLM acceptance.

The job performs these checks against the production unit and a `live` profile installation:

1. Creates the `aiops`, `aiops-operators`, and test operator identities.
2. Installs the package into `/opt/aiops-agent` and the configuration under `/etc/aiops-agent`.
3. Validates and enables `aiops-agent.service` through systemd.
4. Confirms the loopback `/healthz` endpoint and Unix socket are available.
5. Reads the operator token and calls the management API as a member of `aiops-operators`.
6. Stops the service cleanly and starts it again.
7. Sends `SIGKILL` to the daemon and confirms systemd starts a new PID with `NRestarts >= 1`.
8. Uploads the complete unit journal as the `native-systemd-journal` artifact.

The workflow must pass on the exact commit released to `main`. A successful remote run is the release
evidence for native systemd installation, lifecycle, permission, and crash-recovery behavior. GPU and
inference evidence remains in `hardware-acceptance-rtx3050-wsl2.md`.
