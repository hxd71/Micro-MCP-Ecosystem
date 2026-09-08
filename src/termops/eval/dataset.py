"""Evaluation dataset loader and built-in terminal error benchmarks.

Dataset design rationale:
    Following SWE-bench (Jimenez et al., 2024), each sample is a
    (input, expected_output) pair with metadata for stratified evaluation.

    Categories follow the error taxonomy from diagnostics.py:
    - python: ModuleNotFoundError, SyntaxError, TypeError, etc.
    - git: Auth failure, merge conflict, remote rejected
    - docker: Daemon not running, image not found, build failure
    - network: Connection refused, DNS failure, timeout
    - system: Permission denied, disk full, env var missing
    - package: Dependency conflict, package not found

    Difficulty levels:
    - easy: Single unambiguous keyword match (e.g., "command not found")
    - medium: Requires context or multiple pattern matches
    - hard: Ambiguous or multi-cause errors
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path


@dataclass
class EvalSample:
    """A single evaluation sample.

    Mirrors the SWE-bench instance format (Jimenez et al., 2024, §3.1)
    adapted for terminal error analysis.
    """

    id: str
    text: str  # The raw error output (as captured from terminal)
    expected_code: str  # Expected error code from diagnostics.py taxonomy
    expected_root_cause: str  # Ground-truth root cause description
    expected_command: str  # Ground-truth remediation command (may be empty)
    expected_severity: str  # Expected severity: CRITICAL, HIGH, MEDIUM, LOW
    category: str  # Domain: python, git, docker, network, system, package
    difficulty: str  # easy, medium, hard


# ---------------------------------------------------------------------------
# Built-in datasets
# ---------------------------------------------------------------------------

BUILTIN_DATASETS: dict[str, list[EvalSample]] = {}


def _register_builtin(name: str, samples: list[EvalSample]) -> None:
    BUILTIN_DATASETS[name] = samples


# ── Terminal Errors Benchmark (30 samples) ──────────────────────────────

_TERMINAL_ERRORS = [
    # ── Python ─────────────────────────────────────────────────────────
    EvalSample(
        id="terr_001",
        text="ModuleNotFoundError: No module named 'torch'",
        expected_code="MODULE_NOT_FOUND",
        expected_root_cause="The runtime could not import a required dependency.",
        expected_command="pip install torch",
        expected_severity="HIGH",
        category="python",
        difficulty="easy",
    ),
    EvalSample(
        id="terr_002",
        text="ModuleNotFoundError: No module named 'click'",
        expected_code="MODULE_NOT_FOUND",
        expected_root_cause="The runtime could not import a required dependency.",
        expected_command="pip install click",
        expected_severity="HIGH",
        category="python",
        difficulty="easy",
    ),
    EvalSample(
        id="terr_003",
        text="SyntaxError: invalid syntax\n  File \"app.py\", line 42\n    def foo():\n              ^",
        expected_code="SYNTAX_ERROR",
        expected_root_cause="The source file or command text contains a syntax problem.",
        expected_command="",
        expected_severity="HIGH",
        category="python",
        difficulty="easy",
    ),
    EvalSample(
        id="terr_004",
        text="TypeError: unsupported operand type(s) for +: 'int' and 'str'",
        expected_code="TYPE_ERROR",
        expected_root_cause="A runtime or compile-time type mismatch was detected.",
        expected_command="",
        expected_severity="MEDIUM",
        category="python",
        difficulty="easy",
    ),
    EvalSample(
        id="terr_005",
        text="AssertionError: assert 5 == 3\n\nExpected: 5\nActual: 3",
        expected_code="TEST_FAILURE",
        expected_root_cause="A test assertion or verification step failed.",
        expected_command="",
        expected_severity="MEDIUM",
        category="python",
        difficulty="easy",
    ),
    EvalSample(
        id="terr_006",
        text=(
            "ERROR: Cannot install torch==2.0.0 and torchvision==0.15.0 because "
            "these package versions have conflicting dependencies.\n\n"
            "The conflict is caused by:\n    torch 2.0.0 depends on typing-extensions\n"
            "    torchvision 0.15.0 depends on torch==2.0.0"
        ),
        expected_code="DEPENDENCY_CONFLICT",
        expected_root_cause="Installed dependencies have incompatible version requirements.",
        expected_command="pip install torch torchvision --no-deps",
        expected_severity="HIGH",
        category="python",
        difficulty="medium",
    ),
    EvalSample(
        id="terr_007",
        text="RecursionError: maximum recursion depth exceeded in comparison",
        expected_code="STACK_OVERFLOW",
        expected_root_cause="Infinite recursion or excessive call depth was detected.",
        expected_command="",
        expected_severity="HIGH",
        category="python",
        difficulty="easy",
    ),
    # ── Git ────────────────────────────────────────────────────────────
    EvalSample(
        id="terr_008",
        text="fatal: Authentication failed for 'https://github.com/user/repo.git/'",
        expected_code="GIT_AUTH_FAILED",
        expected_root_cause="Git authentication to the remote repository failed.",
        expected_command="",
        expected_severity="HIGH",
        category="git",
        difficulty="easy",
    ),
    EvalSample(
        id="terr_009",
        text="fatal: unable to access 'https://github.com/user/repo.git/': Could not resolve host: github.com",
        expected_code="NETWORK_FAILURE",
        expected_root_cause="A network-dependent operation could not reach its target.",
        expected_command="",
        expected_severity="HIGH",
        category="git",
        difficulty="medium",
    ),
    EvalSample(
        id="terr_010",
        text=(
            "Auto-merging src/app.py\n"
            "CONFLICT (content): Merge conflict in src/app.py\n"
            "Automatic merge failed; fix conflicts and then commit the result."
        ),
        expected_code="GIT_MERGE_CONFLICT",
        expected_root_cause="A git merge or rebase encountered conflicts.",
        expected_command="",
        expected_severity="MEDIUM",
        category="git",
        difficulty="easy",
    ),
    EvalSample(
        id="terr_011",
        text=(
            "To https://github.com/user/repo.git\n"
            " ! [rejected]        main -> main (non-fast-forward)\n"
            "error: failed to push some refs to 'https://github.com/user/repo.git'\n"
            "hint: Updates were rejected because the remote contains work that you do not have locally."
        ),
        expected_code="GIT_REMOTE_REJECTED",
        expected_root_cause="A git push was rejected by the remote.",
        expected_command="git pull --rebase origin main",
        expected_severity="MEDIUM",
        category="git",
        difficulty="easy",
    ),
    EvalSample(
        id="terr_012",
        text=(
            "git@github.com: Permission denied (publickey).\n"
            "fatal: Could not read from remote repository.\n"
            "Please make sure you have the correct access rights and the repository exists."
        ),
        expected_code="GIT_AUTH_FAILED",
        expected_root_cause="Git authentication to the remote repository failed.",
        expected_command="",
        expected_severity="HIGH",
        category="git",
        difficulty="easy",
    ),
    # ── Docker ─────────────────────────────────────────────────────────
    EvalSample(
        id="terr_013",
        text="Cannot connect to the Docker daemon at unix:///var/run/docker.sock. Is the docker daemon running?",
        expected_code="DOCKER_NOT_RUNNING",
        expected_root_cause="The Docker daemon is not reachable.",
        expected_command="",
        expected_severity="HIGH",
        category="docker",
        difficulty="easy",
    ),
    EvalSample(
        id="terr_014",
        text=(
            "Error response from daemon: pull access denied for private/image, "
            "repository does not exist or may require 'docker login': denied: "
            "requested access to the resource is denied"
        ),
        expected_code="DOCKER_IMAGE_NOT_FOUND",
        expected_root_cause="A Docker image or repository could not be located.",
        expected_command="docker login",
        expected_severity="HIGH",
        category="docker",
        difficulty="easy",
    ),
    # ── Network ────────────────────────────────────────────────────────
    EvalSample(
        id="terr_015",
        text="curl: (7) Failed to connect to api.example.com port 443: Connection refused",
        expected_code="NETWORK_FAILURE",
        expected_root_cause="A network-dependent operation could not reach its target.",
        expected_command="",
        expected_severity="HIGH",
        category="network",
        difficulty="easy",
    ),
    EvalSample(
        id="terr_016",
        text="ssh: connect to host 10.0.0.5 port 22: Connection timed out",
        expected_code="TIMEOUT_ERROR",
        expected_root_cause="An operation exceeded its time limit.",
        expected_command="",
        expected_severity="MEDIUM",
        category="network",
        difficulty="easy",
    ),
    EvalSample(
        id="terr_017",
        text="ping: google.com: Temporary failure in name resolution",
        expected_code="NETWORK_FAILURE",
        expected_root_cause="A network-dependent operation could not reach its target.",
        expected_command="",
        expected_severity="HIGH",
        category="network",
        difficulty="easy",
    ),
    EvalSample(
        id="terr_018",
        text="Error: connect ECONNREFUSED 127.0.0.1:5432",
        expected_code="NETWORK_FAILURE",
        expected_root_cause="A network-dependent operation could not reach its target.",
        expected_command="",
        expected_severity="HIGH",
        category="network",
        difficulty="easy",
    ),
    # ── System ─────────────────────────────────────────────────────────
    EvalSample(
        id="terr_019",
        text="bash: nvcc: command not found",
        expected_code="COMMAND_NOT_FOUND",
        expected_root_cause="The shell could not resolve the command name.",
        expected_command="",
        expected_severity="HIGH",
        category="system",
        difficulty="easy",
    ),
    EvalSample(
        id="terr_020",
        text="'python' is not recognized as an internal or external command, operable program or batch file.",
        expected_code="COMMAND_NOT_FOUND",
        expected_root_cause="The shell could not resolve the command name.",
        expected_command="",
        expected_severity="HIGH",
        category="system",
        difficulty="easy",
    ),
    EvalSample(
        id="terr_021",
        text="PermissionError: [Errno 13] Permission denied: '/var/log/app.log'",
        expected_code="PERMISSION_DENIED",
        expected_root_cause="The process hit a filesystem or OS permission boundary.",
        expected_command="sudo chmod 644 /var/log/app.log",
        expected_severity="HIGH",
        category="system",
        difficulty="easy",
    ),
    EvalSample(
        id="terr_022",
        text="OSError: [Errno 28] No space left on device",
        expected_code="DISK_FULL",
        expected_root_cause="The filesystem has run out of available space.",
        expected_command="",
        expected_severity="CRITICAL",
        category="system",
        difficulty="easy",
    ),
    EvalSample(
        id="terr_023",
        text="KeyError: 'DATABASE_URL'\n\nThe required environment variable DATABASE_URL is not set.",
        expected_code="ENV_VAR_MISSING",
        expected_root_cause="An expected environment variable is missing or empty.",
        expected_command="export DATABASE_URL=postgresql://localhost/mydb",
        expected_severity="MEDIUM",
        category="system",
        difficulty="easy",
    ),
    EvalSample(
        id="terr_024",
        text="MemoryError: Unable to allocate 10.0 GiB for an array with shape (1342177280,) and data type float64",
        expected_code="MEMORY_EXHAUSTED",
        expected_root_cause="The process exhausted available memory.",
        expected_command="",
        expected_severity="CRITICAL",
        category="system",
        difficulty="easy",
    ),
    # ── Package / Build ────────────────────────────────────────────────
    EvalSample(
        id="terr_025",
        text=(
            "npm ERR! code ENOENT\n"
            "npm ERR! syscall open\n"
            "npm ERR! path /home/user/package.json\n"
            "npm ERR! errno -2\n"
            "npm ERR! enoent Could not read package.json: Error: ENOENT: "
            "no such file or directory, open '/home/user/package.json'"
        ),
        expected_code="FILE_NOT_FOUND",
        expected_root_cause="A referenced file or path was missing.",
        expected_command="",
        expected_severity="MEDIUM",
        category="package",
        difficulty="medium",
    ),
    EvalSample(
        id="terr_026",
        text=(
            "error: command 'gcc' failed with exit status 1\n\n"
            "src/_cffi_backend.c:15:10: fatal error: Python.h: No such file or directory\n"
            " #include <Python.h>\n          ^~~~~~~~~~~\ncompilation terminated."
        ),
        expected_code="BUILD_FAILURE",
        expected_root_cause="A build or compilation step failed.",
        expected_command="apt-get install python3-dev",
        expected_severity="HIGH",
        category="package",
        difficulty="medium",
    ),
    EvalSample(
        id="terr_027",
        text="error: package was not found in the pypi registry: nonexistent-pkg-xyz",
        expected_code="PACKAGE_NOT_FOUND",
        expected_root_cause="A package manager could not locate the declared package.",
        expected_command="",
        expected_severity="HIGH",
        category="package",
        difficulty="easy",
    ),
    # ── SSL / TLS ──────────────────────────────────────────────────────
    EvalSample(
        id="terr_028",
        text="SSL certificate problem: self signed certificate in certificate chain",
        expected_code="SSL_CERT_ERROR",
        expected_root_cause="An SSL/TLS certificate verification failed.",
        expected_command="",
        expected_severity="HIGH",
        category="network",
        difficulty="easy",
    ),
    # ── Port ───────────────────────────────────────────────────────────
    EvalSample(
        id="terr_029",
        text="Error: listen EADDRINUSE: address already in use :::3000",
        expected_code="PORT_IN_USE",
        expected_root_cause="A local socket bind failed because the port is occupied.",
        expected_command="",
        expected_severity="MEDIUM",
        category="network",
        difficulty="easy",
    ),
    # ── Rate Limit ─────────────────────────────────────────────────────
    EvalSample(
        id="terr_030",
        text="Error: 429 Too Many Requests\n\nYou have exceeded the rate limit. Please try again in 60 seconds.",
        expected_code="RATE_LIMITED",
        expected_root_cause="An API or service call was rate-limited.",
        expected_command="",
        expected_severity="MEDIUM",
        category="network",
        difficulty="easy",
    ),
]

_register_builtin("terminal_errors", _TERMINAL_ERRORS)


# ---------------------------------------------------------------------------
# Dataset loading
# ---------------------------------------------------------------------------

def list_datasets() -> list[str]:
    """List available built-in datasets."""
    return sorted(BUILTIN_DATASETS.keys())


def load_dataset(name: str) -> list[EvalSample]:
    """Load a built-in dataset by name.

    Args:
        name: Dataset name (e.g., "terminal_errors").

    Returns:
        List of EvalSample instances.

    Raises:
        ValueError: If the dataset name is not found.
    """
    if name in BUILTIN_DATASETS:
        return BUILTIN_DATASETS[name]

    raise ValueError(
        f"Unknown dataset: {name!r}. Available: {list_datasets()}"
    )


def load_dataset_from_jsonl(path: str | Path) -> list[EvalSample]:
    """Load a custom dataset from a JSONL file.

    Expected format (one JSON object per line):
        {"id": "...", "text": "...", "expected_code": "...",
         "expected_root_cause": "...", "expected_command": "...",
         "expected_severity": "...", "category": "...", "difficulty": "..."}

    Following the SWE-bench file format convention (Jimenez et al., 2024).
    """
    path = Path(path)
    samples: list[EvalSample] = []
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            obj = json.loads(line)
            samples.append(
                EvalSample(
                    id=obj["id"],
                    text=obj["text"],
                    expected_code=obj["expected_code"],
                    expected_root_cause=obj["expected_root_cause"],
                    expected_command=obj.get("expected_command", ""),
                    expected_severity=obj.get("expected_severity", "MEDIUM"),
                    category=obj.get("category", "unknown"),
                    difficulty=obj.get("difficulty", "medium"),
                )
            )
    return samples


def export_dataset_to_jsonl(samples: list[EvalSample], path: str | Path) -> None:
    """Export a dataset to JSONL format for sharing."""
    path = Path(path)
    with open(path, "w", encoding="utf-8") as f:
        for s in samples:
            obj = {
                "id": s.id,
                "text": s.text,
                "expected_code": s.expected_code,
                "expected_root_cause": s.expected_root_cause,
                "expected_command": s.expected_command,
                "expected_severity": s.expected_severity,
                "category": s.category,
                "difficulty": s.difficulty,
            }
            f.write(json.dumps(obj, ensure_ascii=False) + "\n")
