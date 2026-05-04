#!/usr/bin/env python3
"""
Code Chunking Orchestrator

Parses all source files via tree-sitter, builds a hierarchical chunk tree
for visualization.

Communication protocol (JSON lines on stdout):
  {"type": "log", "message": "..."}     - progress log
  {"type": "result", "data": {...}, "sourceFiles": {...}}  - chunk tree
  {"type": "error", "message": "..."}   - error
"""

import subprocess
import sys
import os
import json
import time


def emit(msg_type, **kwargs):
    print(json.dumps({"type": msg_type, **kwargs}), flush=True)


def log(message):
    emit("log", message=message)


def run_treesitter_phase(project_path, max_node_size=0, preserve_scoping=False):
    """Run tree-sitter CST parsing via the treesitter_ast.py script.

    Forwards all JSON lines to stdout. Returns result_emitted.
    """
    script_path = os.path.join(os.path.dirname(__file__), "treesitter_ast.py")
    python_cmd = sys.executable

    args = [python_cmd, script_path, project_path]
    if max_node_size > 0:
        args.extend(["--max-node-size", str(max_node_size)])
    if preserve_scoping:
        args.append("--preserve-scoping")

    process = subprocess.Popen(
        args,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )

    result_emitted = False
    for line in process.stdout:
        line = line.rstrip("\n")
        if not line:
            continue
        # Forward JSON lines directly to our stdout
        print(line, flush=True)
        try:
            msg = json.loads(line)
            if msg.get("type") == "result":
                result_emitted = True
        except json.JSONDecodeError:
            pass

    # Forward any stderr as log
    stderr_out = process.stderr.read()
    if stderr_out.strip():
        log(f"[tree-sitter stderr] {stderr_out.strip()}")

    process.wait()
    if process.returncode != 0 and not result_emitted:
        log(f"Tree-sitter script exited with code {process.returncode}")

    return result_emitted


def main():
    if len(sys.argv) < 2:
        emit("error", message="Usage: analyze.py <project_path>")
        sys.exit(1)

    project_path = os.path.abspath(sys.argv[1])

    # Parse arguments
    max_node_size = 0
    preserve_scoping = False
    for i, arg in enumerate(sys.argv):
        if arg == "--max-node-size" and i + 1 < len(sys.argv):
            try:
                max_node_size = int(sys.argv[i + 1])
            except ValueError:
                pass
        elif arg == "--preserve-scoping":
            preserve_scoping = True

    if not os.path.isdir(project_path):
        emit("error", message=f"Not a directory: {project_path}")
        sys.exit(1)

    result_emitted = run_treesitter_phase(project_path, max_node_size, preserve_scoping)

    if not result_emitted:
        emit("error", message="Tree-sitter parsing failed to produce results.")


if __name__ == "__main__":
    main()
