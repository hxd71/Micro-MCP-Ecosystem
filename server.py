import argparse
import locale
import os
import subprocess
import sys
import webbrowser

from mcp.server import Server
from mcp.server.fastmcp import FastMCP
from mcp.server.sse import SseServerTransport
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import HTMLResponse, JSONResponse
from starlette.routing import Mount, Route
import uvicorn

# Initialize FastMCP server for DevOps tools.
mcp = FastMCP("mcp-server-devops")

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")


@mcp.tool()
async def run_shell_command(command: str) -> str:
        """Run a shell command with a human-in-the-loop confirmation step.

        Args:
                command: Shell command to execute.

        Returns:
            A multiline string containing stdout, stderr and returncode.
        """
        def decode_output(raw: bytes) -> str:
            """Decode command output with common Windows/UTF-8 fallbacks."""
            candidates = [
                "utf-8",
                locale.getpreferredencoding(False),
                "gbk",
                "cp936",
            ]
            for encoding in candidates:
                if not encoding:
                    continue
                try:
                    return raw.decode(encoding)
                except UnicodeDecodeError:
                    continue
            return raw.decode("utf-8", errors="replace")

        approval_mode = os.environ.get("MCP_APPROVAL_MODE", "prompt").strip().lower()

        print(f"\033[91m[⚠️ 拦截报警] Agent 试图执行敏感命令: {command}\033[0m")

        if approval_mode == "auto":
            approval = "y"
        elif approval_mode == "deny":
            approval = "n"
        else:
            approval = input("允许执行该命令吗?[y/n] ").strip().lower()

        if approval != "y":
            return (
                "approved: false\n"
                f"command: {command}\n"
                "returncode: null\n"
                "stdout:\n"
                "\n"
                "stderr:\n"
                "Command execution rejected by human.\n"
            )

        # Keep shell=True for generic command execution from MCP inputs.
        command_to_run = command
        if os.name == "nt" and command.strip() == "ls -la":
            command_to_run = "dir"

        process = subprocess.run(command_to_run, shell=True, capture_output=True)
        stdout_text = decode_output(process.stdout)
        stderr_text = decode_output(process.stderr)

        return (
            "approved: true\n"
            f"command: {command}\n"
            f"returncode: {process.returncode}\n"
            "stdout:\n"
            f"{stdout_text}\n"
            "stderr:\n"
            f"{stderr_text}"
        )


@mcp.tool()
async def read_local_file(filepath: str) -> str:
        """Read local file content.

        Args:
                filepath: Absolute or relative local path.

        Returns:
                File content, or a readable error message.
        """
        try:
                with open(filepath, "r", encoding="utf-8") as file:
                        return file.read()
        except Exception as exc:
                return f"Failed to read file: {exc}"


DEBUG_PAGE_HTML = """
<!doctype html>
<html lang="zh-CN">
<head>
    <meta charset="UTF-8" />
    <meta name="viewport" content="width=device-width, initial-scale=1.0" />
    <title>MCP DevOps Debug Page</title>
    <style>
        :root {
            --bg: #f5f7fb;
            --card: #ffffff;
            --text: #13203a;
            --muted: #5f6f8e;
            --accent: #0e8a6d;
            --danger: #c0392b;
            --border: #e2e8f3;
        }

        body {
            margin: 0;
            padding: 24px;
            font-family: "Segoe UI", "Microsoft YaHei", sans-serif;
            background: radial-gradient(circle at top right, #d8ffe5, var(--bg) 45%);
            color: var(--text);
        }

        .container {
            max-width: 900px;
            margin: 0 auto;
            display: grid;
            gap: 16px;
        }

        .card {
            background: var(--card);
            border: 1px solid var(--border);
            border-radius: 14px;
            padding: 16px;
            box-shadow: 0 6px 18px rgba(19, 32, 58, 0.06);
        }

        h1 { margin: 0 0 6px; }
        h2 { margin-top: 0; }
        p { color: var(--muted); }

        label { display: block; margin: 8px 0 6px; font-weight: 600; }

        input, textarea {
            width: 100%;
            padding: 10px;
            border-radius: 10px;
            border: 1px solid #cad4e6;
            font-family: Consolas, monospace;
            box-sizing: border-box;
        }

        button {
            margin-top: 10px;
            background: var(--accent);
            color: #fff;
            border: none;
            border-radius: 10px;
            padding: 10px 14px;
            font-weight: 700;
            cursor: pointer;
        }

        pre {
            margin-top: 12px;
            background: #0f1729;
            color: #f2f4f8;
            border-radius: 10px;
            padding: 12px;
            overflow: auto;
            min-height: 72px;
        }

        .warn {
            color: var(--danger);
            font-weight: 700;
        }
    </style>
</head>
<body>
    <div class="container">
        <div class="card">
            <h1>mcp-server-devops</h1>
            <p>当前暴露工具：<strong>run_shell_command</strong>、<strong>read_local_file</strong></p>
            <p class="warn">调用 run_shell_command 时，服务端终端会弹出 y/n 人工确认。</p>
        </div>

        <div class="card">
            <h2>Tool: run_shell_command(command: str)</h2>
            <label for="command">命令</label>
            <input id="command" value="ls -la" />
            <button onclick="runCommand()">执行命令</button>
            <pre id="commandResult"></pre>
        </div>

        <div class="card">
            <h2>Tool: read_local_file(filepath: str)</h2>
            <label for="filepath">文件路径</label>
            <input id="filepath" placeholder="例如: README.md 或 C:/logs/app.log" />
            <button onclick="readFile()">读取文件</button>
            <pre id="fileResult"></pre>
        </div>
    </div>

    <script>
        async function runCommand() {
            const command = document.getElementById('command').value;
            const resultBox = document.getElementById('commandResult');
            resultBox.textContent = '执行中...（请留意服务端终端是否在等待 y/n）';

            const response = await fetch('/debug/run_shell_command', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ command })
            });
            const data = await response.json();
            resultBox.textContent = data.result;
        }

        async function readFile() {
            const filepath = document.getElementById('filepath').value;
            const resultBox = document.getElementById('fileResult');
            resultBox.textContent = '读取中...';

            const response = await fetch('/debug/read_local_file', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ filepath })
            });
            const data = await response.json();
            resultBox.textContent = data.result;
        }
    </script>
</body>
</html>
"""


def create_starlette_app(mcp_server: Server, *, debug: bool = False) -> Starlette:
    """Create a Starlette application that can serve the provided MCP server with SSE.
    
    Sets up a Starlette web application with routes for SSE (Server-Sent Events)
    communication with the MCP server.
    
    Args:
        mcp_server: The MCP server instance to connect
        debug: Whether to enable debug mode for the Starlette app
        
    Returns:
        A configured Starlette application
    """
    # Create an SSE transport with a base path for messages
    sse = SseServerTransport("/messages/")

    async def handle_sse(request: Request) -> None:
        """Handler for SSE connections.
        
        Establishes an SSE connection and connects it to the MCP server.
        
        Args:
            request: The incoming HTTP request
        """
        # Connect the SSE transport to the request
        async with sse.connect_sse(
                request.scope,
                request.receive,
                request._send,  # noqa: SLF001
        ) as (read_stream, write_stream):
            # Run the MCP server with the SSE streams
            await mcp_server.run(
                read_stream,
                write_stream,
                mcp_server.create_initialization_options(),
            )

    async def debug_page(_: Request) -> HTMLResponse:
        return HTMLResponse(DEBUG_PAGE_HTML)

    async def debug_run_shell_command(request: Request) -> JSONResponse:
        payload = await request.json()
        command = str(payload.get("command", "")).strip()
        result = await run_shell_command(command)
        return JSONResponse({"result": result})

    async def debug_read_local_file(request: Request) -> JSONResponse:
        payload = await request.json()
        filepath = str(payload.get("filepath", "")).strip()
        result = await read_local_file(filepath)
        return JSONResponse({"result": result})

    # Create and return the Starlette application with routes.
    return Starlette(
        debug=debug,
        routes=[
            Route("/", endpoint=debug_page, methods=["GET"]),
            Route("/debug/run_shell_command", endpoint=debug_run_shell_command, methods=["POST"]),
            Route("/debug/read_local_file", endpoint=debug_read_local_file, methods=["POST"]),
            Route("/sse", endpoint=handle_sse),
            Mount("/messages/", app=sse.handle_post_message),
        ],
    )


if __name__ == "__main__":
    # Get the underlying MCP server from the FastMCP instance
    mcp_server = mcp._mcp_server  # noqa: WPS437

    # Set up command-line argument parsing
    parser = argparse.ArgumentParser(description="Run MCP server with configurable transport")
    # Allow choosing between stdio and SSE transport modes
    parser.add_argument(
        "--transport",
        choices=["stdio", "sse"],
        default="stdio",
        help="Transport mode (stdio or sse)",
    )
    # Host configuration for SSE mode
    parser.add_argument("--host", default="0.0.0.0", help="Host to bind to (for SSE mode)")
    # Port configuration for SSE mode
    parser.add_argument("--port", type=int, default=8080, help="Port to listen on (for SSE mode)")
    parser.add_argument(
        "--open-browser",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="Automatically open debug page in browser for SSE mode",
    )
    args = parser.parse_args()

    # Launch the server with the selected transport mode
    if args.transport == "stdio":
        # Run with stdio transport (default)
        # This mode communicates through standard input/output
        mcp.run(transport="stdio")
    else:
        # Run with SSE transport (web-based)
        # Create a Starlette app to serve the MCP server
        starlette_app = create_starlette_app(mcp_server, debug=True)
        if args.open_browser:
            webbrowser.open(f"http://{args.host}:{args.port}/")
        # Start the web server with the configured host and port
        uvicorn.run(starlette_app, host=args.host, port=args.port)