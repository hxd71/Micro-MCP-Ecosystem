#!/usr/bin/env bash
set -Eeuo pipefail

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
log_path="${SYSTEMD_ACCEPTANCE_LOG:-/tmp/aiops-agent-systemd.log}"

capture_journal() {
  sudo journalctl --no-pager -u aiops-agent.service >"$log_path" 2>&1 || true
}

cleanup() {
  status=$?
  capture_journal
  sudo systemctl disable --now aiops-agent.service >/dev/null 2>&1 || true
  sudo systemctl daemon-reload >/dev/null 2>&1 || true
  trap - EXIT
  exit "$status"
}
trap cleanup EXIT

wait_for_active() {
  for _ in $(seq 1 60); do
    if systemctl is-active --quiet aiops-agent.service; then
      return 0
    fi
    sleep 0.5
  done
  sudo systemctl status --no-pager aiops-agent.service || true
  return 1
}

wait_for_http() {
  for _ in $(seq 1 60); do
    if curl --fail --silent --show-error http://127.0.0.1:8787/healthz >/tmp/aiops-health.json; then
      python -c 'import json; p=json.load(open("/tmp/aiops-health.json")); assert p == {"ok": True, "profile": "live"}'
      return 0
    fi
    sleep 0.5
  done
  return 1
}

wait_for_socket() {
  for _ in $(seq 1 60); do
    if [[ -S /run/aiops-agent/agent.sock ]] &&
      [[ "$(stat -c '%a:%U:%G' /run/aiops-agent/agent.sock)" == "660:aiops:aiops-operators" ]]; then
      return 0
    fi
    sleep 0.5
  done
  return 1
}

sudo systemctl start docker.service
getent group aiops >/dev/null || sudo groupadd --system aiops
getent group aiops-operators >/dev/null || sudo groupadd --system aiops-operators
getent group docker >/dev/null || sudo groupadd --system docker
id aiops >/dev/null 2>&1 ||
  sudo useradd --system --gid aiops --home-dir /var/lib/aiops-agent --shell /usr/sbin/nologin aiops
id aiops-test-operator >/dev/null 2>&1 ||
  sudo useradd --system --gid aiops-operators --home-dir /nonexistent --shell /usr/sbin/nologin aiops-test-operator
sudo usermod --append --groups docker,aiops-operators aiops

runner_user="$(id -un)"
runner_group="$(id -gn)"
sudo install -d -o "$runner_user" -g "$runner_group" -m 0755 /opt/aiops-agent
python -m venv /opt/aiops-agent
/opt/aiops-agent/bin/python -m pip install --disable-pip-version-check "$repo_root"

sudo install -d -o root -g aiops -m 0750 /etc/aiops-agent /etc/aiops-agent/secrets
sudo install -d -o aiops -g aiops -m 0750 /models
sudo install -o root -g aiops -m 0640 \
  "$repo_root/config/aiops-agent.toml.example" /etc/aiops-agent/aiops-agent.toml
sudo install -o root -g root -m 0644 \
  "$repo_root/deploy/systemd/aiops-agent.service" /etc/systemd/system/aiops-agent.service

sudo systemctl daemon-reload
sudo systemd-analyze verify /etc/systemd/system/aiops-agent.service
sudo systemctl enable --now aiops-agent.service
wait_for_active
wait_for_http
wait_for_socket

[[ "$(stat -c '%a:%U:%G' /var/lib/aiops-agent/operator.token)" == "640:aiops:aiops-operators" ]]
operator_token="$(sudo -u aiops-test-operator cat /var/lib/aiops-agent/operator.token)"
sudo -u aiops-test-operator curl --fail --silent --show-error \
  --unix-socket /run/aiops-agent/agent.sock \
  --header "X-AIOPS-Token: $operator_token" \
  http://aiops.local/v1/capabilities >/tmp/aiops-capabilities.json
python -c 'import json; p=json.load(open("/tmp/aiops-capabilities.json")); assert p["profile"] == "live"'

sudo systemctl stop aiops-agent.service
if systemctl is-active --quiet aiops-agent.service; then
  echo "aiops-agent remained active after systemctl stop" >&2
  exit 1
fi
sudo systemctl start aiops-agent.service
wait_for_active
wait_for_http
wait_for_socket

old_pid="$(systemctl show --property MainPID --value aiops-agent.service)"
sudo kill -KILL "$old_pid"
for _ in $(seq 1 90); do
  new_pid="$(systemctl show --property MainPID --value aiops-agent.service)"
  if systemctl is-active --quiet aiops-agent.service &&
    [[ "$new_pid" != "0" ]] && [[ "$new_pid" != "$old_pid" ]] &&
    curl --fail --silent http://127.0.0.1:8787/healthz >/dev/null; then
    break
  fi
  sleep 0.5
done

new_pid="$(systemctl show --property MainPID --value aiops-agent.service)"
restart_count="$(systemctl show --property NRestarts --value aiops-agent.service)"
[[ "$new_pid" != "0" && "$new_pid" != "$old_pid" ]]
[[ "$restart_count" -ge 1 ]]
wait_for_socket

echo "native systemd acceptance passed: old_pid=$old_pid new_pid=$new_pid restarts=$restart_count"
