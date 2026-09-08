"""Generic terminal/code error classification.

This module is intentionally deterministic: it uses structured pattern matching to
produce findings that can be tested, versioned, and audited. LLM-based enrichment
is left as an optional plugin layer outside the production core.
"""

from __future__ import annotations

from typing import Any

from .models import Severity

ErrorPattern = tuple[tuple[str, ...], Severity, str, str]

GENERIC_ERROR_PATTERNS: dict[str, ErrorPattern] = {
    "COMMAND_NOT_FOUND": (
        (
            "command not found",
            "is not recognized as an internal or external command",
            "is not recognized",
            "was not found in this repository",
        ),
        Severity.HIGH,
        "The shell could not resolve the command name.",
        "Check PATH, shell spelling, and platform-specific command syntax.",
    ),
    "MODULE_NOT_FOUND": (
        (
            "module not found",
            "cannot find module",
            "no module named",
            "modulenotfounderror",
            "cannot import name",
        ),
        Severity.HIGH,
        "The runtime could not import a required dependency.",
        "Install the missing package or verify the active interpreter/environment.",
    ),
    "PACKAGE_NOT_FOUND": (
        (
            "package was not found",
            "cannot find package",
            "error: cannot find module",
            "cannot resolve package",
            "npm err! code enoent",
            "could not find package",
        ),
        Severity.HIGH,
        "A package manager could not locate the declared package.",
        "Verify the package name, registry, lockfile, and network connectivity.",
    ),
    "PERMISSION_DENIED": (
        (
            "permission denied",
            "access is denied",
            "operation not permitted",
            "errno 13",
            "unauthorized",
        ),
        Severity.HIGH,
        "The process hit a filesystem or OS permission boundary.",
        "Inspect file ownership, ACLs, sudo requirements, and execution context.",
    ),
    "FILE_NOT_FOUND": (
        (
            "no such file or directory",
            "file not found",
            "cannot open file",
            "enoent",
            "path does not exist",
        ),
        Severity.MEDIUM,
        "A referenced file or path was missing.",
        "Verify relative paths, working directory, and generated artifact locations.",
    ),
    "SYNTAX_ERROR": (
        (
            "syntaxerror",
            "unexpected token",
            "invalid syntax",
            "syntax error",
            "unexpected indent",
        ),
        Severity.HIGH,
        "The source file or command text contains a syntax problem.",
        "Check the parser line and reduce the input to the smallest failing example.",
    ),
    "TEST_FAILURE": (
        (
            "assertionerror",
            "assertion failed",
            "expected",
            "actual",
            "tests failed",
            "test failure",
        ),
        Severity.MEDIUM,
        "A test assertion or verification step failed.",
        "Inspect the assertion diff, fixture setup, and recent code changes.",
    ),
    "TYPE_ERROR": (
        (
            "typeerror",
            "type error",
            "cannot be applied to operands of type",
            "incompatible types",
            "mismatched types",
        ),
        Severity.MEDIUM,
        "A runtime or compile-time type mismatch was detected.",
        "Check argument types, generics, and recent API signature changes.",
    ),
    "NETWORK_FAILURE": (
        (
            "connection refused",
            "connection timed out",
            "could not resolve host",
            "network is unreachable",
            "econnrefused",
            "err_name_not_resolved",
            "temporary failure in name resolution",
        ),
        Severity.HIGH,
        "A network-dependent operation could not reach its target.",
        "Verify DNS, proxy, firewall, VPN, and target service availability.",
    ),
    "PORT_IN_USE": (
        (
            "address already in use",
            "port is already in use",
            "eaddrinuse",
            "bind: address already in use",
        ),
        Severity.MEDIUM,
        "A local socket bind failed because the port is occupied.",
        "Identify the conflicting process or configure a different port.",
    ),
    "ENV_VAR_MISSING": (
        (
            "environment variable",
            "env var",
            "keyerror: '",
            "required environment variable",
            "is not set",
        ),
        Severity.MEDIUM,
        "An expected environment variable is missing or empty.",
        "Set the variable in the current shell or .env file and retry.",
    ),
    "DEPENDENCY_CONFLICT": (
        (
            "version conflict",
            "incompatible with",
            "dependency resolution failed",
            "conflicting dependencies",
            "peer dep",
        ),
        Severity.HIGH,
        "Installed dependencies have incompatible version requirements.",
        "Review lockfiles, constraints, and consider a clean install.",
    ),
    "BUILD_FAILURE": (
        (
            "build failed",
            "compilation failed",
            "failed to compile",
            "error: command 'gcc'",
            "exit status 1",
            "ld: symbol(s) not found",
        ),
        Severity.HIGH,
        "A build or compilation step failed.",
        "Inspect compiler output, missing headers/libs, and build environment.",
    ),
    "STACK_OVERFLOW": (
        (
            "maximum call stack size exceeded",
            "recursionerror",
            "stack overflow",
        ),
        Severity.HIGH,
        "Infinite recursion or excessive call depth was detected.",
        "Check termination conditions and recursive function logic.",
    ),
    "MEMORY_EXHAUSTED": (
        (
            "out of memory",
            "memoryerror",
            "killed process",
            "cannot allocate memory",
            "oom",
        ),
        Severity.CRITICAL,
        "The process exhausted available memory.",
        "Reduce data size, batch size, or memory reservations before retrying.",
    ),
    "EXIT_NON_ZERO": (
        (
            "exited with code",
            "exit status",
            "returned non-zero",
            "process finished with exit code",
        ),
        Severity.MEDIUM,
        "A command or process exited with a non-zero status.",
        "Inspect the preceding stderr/stdout for the underlying cause.",
    ),
    # ── Docker ────────────────────────────────────────────────────────
    "DOCKER_NOT_RUNNING": (
        (
            "docker daemon is not running",
            "cannot connect to the docker daemon",
            "is the docker daemon running",
            "error during connect",
        ),
        Severity.HIGH,
        "The Docker daemon is not reachable.",
        "Start Docker Desktop or the dockerd service, and verify the DOCKER_HOST variable.",
    ),
    "DOCKER_IMAGE_NOT_FOUND": (
        (
            "pull access denied",
            "repository does not exist",
            "manifest unknown",
            "not found: manifest",
            "image not found",
        ),
        Severity.HIGH,
        "A Docker image or repository could not be located.",
        "Verify the image tag, registry login, and network connectivity.",
    ),
    "DOCKER_BUILD_FAILED": (
        (
            "docker build failed",
            "the command '/bin/sh -c' returned a non-zero code",
            "error building image",
            "failed to build",
        ),
        Severity.HIGH,
        "A Docker build step failed.",
        "Inspect the failing RUN/COPY layer, check Dockerfile syntax and base image availability.",
    ),
    # ── Git ───────────────────────────────────────────────────────────
    "GIT_AUTH_FAILED": (
        (
            "authentication failed",
            "fatal: authentication failed",
            "remote: invalid username or password",
            "could not read from remote repository",
            "permission denied (publickey)",
            "please make sure you have the correct access rights",
        ),
        Severity.HIGH,
        "Git authentication to the remote repository failed.",
        "Verify SSH key, personal access token, or credential helper configuration.",
    ),
    "GIT_MERGE_CONFLICT": (
        (
            "merge conflict",
            "automatic merge failed",
            "conflict: merge",
            "both modified:",
            "unmerged paths",
        ),
        Severity.MEDIUM,
        "A git merge or rebase encountered conflicts.",
        "Resolve conflicts in the listed files, then git add and commit.",
    ),
    "GIT_REMOTE_REJECTED": (
        (
            "remote rejected",
            "failed to push some refs",
            "non-fast-forward",
            "updates were rejected",
        ),
        Severity.MEDIUM,
        "A git push was rejected by the remote.",
        "Pull latest changes first, resolve conflicts, or check branch protection rules.",
    ),
    # ── SSL / TLS ─────────────────────────────────────────────────────
    "SSL_CERT_ERROR": (
        (
            "certificate verify failed",
            "ssl certificate",
            "self-signed certificate",
            "unable to get local issuer certificate",
            "certificate has expired",
            "ssl: certificate",
        ),
        Severity.HIGH,
        "An SSL/TLS certificate verification failed.",
        "Check the certificate chain, expiration date, or add the CA to the trust store.",
    ),
    # ── Timeout ───────────────────────────────────────────────────────
    "TIMEOUT_ERROR": (
        (
            "timed out",
            "timeout",
            "deadline exceeded",
            "context deadline exceeded",
            "request timed out",
        ),
        Severity.MEDIUM,
        "An operation exceeded its time limit.",
        "Increase the timeout setting, check network latency, or optimize the operation.",
    ),
    # ── Disk ──────────────────────────────────────────────────────────
    "DISK_FULL": (
        (
            "no space left on device",
            "disk full",
            "enospc",
            "insufficient disk space",
            "quota exceeded",
        ),
        Severity.CRITICAL,
        "The filesystem has run out of available space.",
        "Free disk space, prune logs/caches, or extend the volume.",
    ),
    # ── Rate Limit ────────────────────────────────────────────────────
    "RATE_LIMITED": (
        (
            "rate limit exceeded",
            "too many requests",
            "429",
            "rate limited",
            "api rate limit",
            "throttled",
        ),
        Severity.MEDIUM,
        "An API or service call was rate-limited.",
        "Wait for the rate window to reset, or check quota/plan limits.",
    ),
    # ── Config / YAML ─────────────────────────────────────────────────
    "CONFIG_PARSE_ERROR": (
        (
            "yaml",
            "yaml:",
            "could not find expected",
            "mapping values are not allowed",
            "duplicate key",
            "invalid yaml",
        ),
        Severity.MEDIUM,
        "A configuration file failed to parse (likely YAML indentation or syntax).",
        "Validate the YAML/JSON with a linter and fix indentation or duplicate keys.",
    ),
    "TOML_PARSE_ERROR": (
        (
            "toml",
            "toml parse error",
            "invalid toml",
            "expected newline",
            "key is not closed",
        ),
        Severity.MEDIUM,
        "A TOML configuration file failed to parse.",
        "Check for missing quotes, invalid keys, or malformed inline tables.",
    ),
    # ── Database ──────────────────────────────────────────────────────
    "DB_CONNECTION_FAILED": (
        (
            "could not connect to server",
            "connection refused",
            "database is locked",
            "unable to connect",
            "cannot connect to database",
            "could not connect to database",
        ),
        Severity.HIGH,
        "A database connection could not be established.",
        "Verify the database host, port, credentials, and that the service is running.",
    ),
    # ── Encoding ──────────────────────────────────────────────────────
    "ENCODING_ERROR": (
        (
            "unicodeencodeerror",
            "unicodedecodeerror",
            "codec can't decode",
            "codec can't encode",
            "invalid byte",
            "invalid utf-8",
        ),
        Severity.MEDIUM,
        "A text encoding or decoding operation failed.",
        "Specify the correct encoding or handle binary data with errors='replace'.",
    ),
    # ── Process ───────────────────────────────────────────────────────
    "PROCESS_KILLED": (
        (
            "signal: killed",
            "signal: terminated",
            "process was killed",
            "killed by signal",
            "sigterm",
            "sighup",
        ),
        Severity.MEDIUM,
        "The process was terminated by an external signal.",
        "Check for OOM killer, systemd limits, or manual kill signals.",
    ),
    # ── Lock File ─────────────────────────────────────────────────────
    "LOCK_CONFLICT": (
        (
            "unable to acquire lock",
            "lock file exists",
            "already locked",
            "another process is running",
            "lock timeout",
        ),
        Severity.MEDIUM,
        "A lock file or mutex prevented the operation from proceeding.",
        "Remove stale lock files or wait for the holding process to finish.",
    ),
    # ── Path / CWD ────────────────────────────────────────────────────
    "PATH_RESOLUTION_ERROR": (
        (
            "not a git repository",
            "not a valid working directory",
            "outside of a project",
            "cannot find project root",
            "no such directory",
        ),
        Severity.MEDIUM,
        "The current working directory or project root is not as expected.",
        "Change to the correct project directory or verify the project structure.",
    ),
}


def generic_error_evidence(text: str) -> list[dict[str, Any]]:
    """Return structured matches from free-form terminal or log text."""
    matches: list[dict[str, Any]] = []
    for line_number, line in enumerate(text.splitlines(), start=1):
        lowered = line.lower()
        for code, (patterns, severity, meaning, remediation) in GENERIC_ERROR_PATTERNS.items():
            if any(pattern in lowered for pattern in patterns):
                matches.append(
                    {
                        "code": code,
                        "severity": severity.value,
                        "meaning": meaning,
                        "remediation": remediation,
                        "line": line_number,
                        "text": line.strip()[:500],
                    }
                )
    return matches[:50]


def classify_error(text: str, exit_code: int | None = None) -> dict[str, Any]:
    """High-level classification used by the planning layer."""
    findings = generic_error_evidence(text)
    codes = {f["code"] for f in findings}
    if exit_code not in (None, 0) and "EXIT_NON_ZERO" not in codes:
        findings.append(
            {
                "code": "EXIT_NON_ZERO",
                "severity": Severity.MEDIUM.value,
                "meaning": "The analyzed command exited with a non-zero status.",
                "remediation": "Inspect the command's stderr/stdout and surrounding context before retrying.",
                "line": 1,
                "text": f"exit_code={exit_code}",
            }
        )
        codes.add("EXIT_NON_ZERO")

    primary = None
    for code in (
        "MEMORY_EXHAUSTED", "DISK_FULL", "DOCKER_NOT_RUNNING", "GIT_AUTH_FAILED",
        "COMMAND_NOT_FOUND", "MODULE_NOT_FOUND", "NETWORK_FAILURE", "PERMISSION_DENIED",
        "SSL_CERT_ERROR", "DB_CONNECTION_FAILED",
    ):
        if code in codes:
            primary = code
            break

    return {
        "findings": findings,
        "codes": sorted(codes),
        "primary_code": primary,
        "has_actionable": bool(codes),
    }


def suggest_followup_command(text: str, codes: set[str], language: str, command: str) -> str | None:
    """Suggest a safe, read-only verification command based on the primary finding."""
    lang = language.lower().strip()
    if "MODULE_NOT_FOUND" in codes and lang in {"python", "py", ""}:
        return f'"{__import__("sys").executable}" -c "import sys; print(sys.executable)"'
    if "COMMAND_NOT_FOUND" in codes and command:
        first = command.strip().split()[0]
        if first:
            return f"where {first}" if __import__("os").name == "nt" else f"which {first}"
    if "FILE_NOT_FOUND" in codes:
        return 'python -c "import os; print(os.getcwd())"'
    if "ENV_VAR_MISSING" in codes:
        return "set" if __import__("os").name == "nt" else "env"
    if "NETWORK_FAILURE" in codes:
        return "ping 127.0.0.1"
    if "DOCKER_NOT_RUNNING" in codes:
        return "docker info"
    if "DOCKER_IMAGE_NOT_FOUND" in codes:
        return "docker images"
    if "GIT_AUTH_FAILED" in codes:
        return "git remote -v"
    if "GIT_MERGE_CONFLICT" in codes:
        return "git status"
    if "DISK_FULL" in codes:
        return "wmic logicaldisk get size,freespace,caption" if __import__("os").name == "nt" else "df -h"
    if "PORT_IN_USE" in codes:
        return "netstat -ano" if __import__("os").name == "nt" else "ss -tlnp"
    if "TIMEOUT_ERROR" in codes:
        return "ping 8.8.8.8"
    if "DB_CONNECTION_FAILED" in codes:
        return "netstat -ano | findstr :5432" if __import__("os").name == "nt" else "ss -tlnp | grep :5432"
    if "PATH_RESOLUTION_ERROR" in codes:
        return "cd" if __import__("os").name == "nt" else "pwd"
    if "LOCK_CONFLICT" in codes:
        return "ls -la *.lock" if __import__("os").name == "nt" else "ls -la *.lock 2>/dev/null; echo 'No lock files'"
    return None
