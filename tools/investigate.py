#!/usr/bin/env python3
"""
Code Impact Investigator

Agentic loop using vLLM (offline) with tool calling against Neo4j.
The LLM iteratively queries the code graph to build a system-wide
impact analysis for a specific leaf node.

Usage: investigate.py <chunk_tree.json> <node_id>

Communication protocol (JSON lines on stdout):
  {"type": "log", "message": "..."}
  {"type": "tool", "name": "...", "args": "...", "resultCount": N}
  {"type": "result", "analysis": "..."}
  {"type": "error", "message": "..."}
"""

import sys
import os
import json
import time
import re

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

MODEL_NAME = "/media/lennard/Volume/Models/Qwen3.5-9B"

VLLM_SETTINGS = {
    "tensor_parallel_size": 1,
    "gpu_memory_utilization": 0.92,
    "max_model_len": 32768,
    "max_num_seqs": 1,
    "dtype": "auto",
}

SAMPLING_PARAMS_SETTINGS = {
    "temperature": 0.3,
    "max_tokens": 4096,
    "top_p": 0.95,
}

MAX_ITERATIONS_PER_PHASE = 15
MAX_PHASES = 2  # 2 phases × 15 iterations = 30 total
MAX_TOOL_RESULT_CHARS = 4000
NEO4J_URI = "bolt://localhost:7687"


# ---------------------------------------------------------------------------
# Protocol
# ---------------------------------------------------------------------------

def emit(msg_type, **kwargs):
    print(json.dumps({"type": msg_type, **kwargs}), flush=True)


def log(message):
    emit("log", message=message)


# ---------------------------------------------------------------------------
# Tool definitions
# ---------------------------------------------------------------------------

TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "get_similar_code",
            "description": "Find code sections semantically similar to a given node via embedding edges. Returns summaries and metadata ordered by similarity.",
            "parameters": {
                "type": "object",
                "properties": {
                    "node_id": {"type": "string", "description": "The nodeId to find similar code for"},
                    "limit": {"type": "integer", "description": "Max results (default 10)"}
                },
                "required": ["node_id"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "get_neighbors",
            "description": "Get code sections immediately before and/or after a given node within the same file (via SUCCESSOR/PREDECESSOR edges).",
            "parameters": {
                "type": "object",
                "properties": {
                    "node_id": {"type": "string", "description": "The nodeId to find neighbors for"},
                    "direction": {"type": "string", "enum": ["successor", "predecessor", "both"], "description": "Direction (default: both)"}
                },
                "required": ["node_id"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "get_node_details",
            "description": "Get full details of a specific node: code/content, summary, file location, and the complete parent hierarchy from root with each parent's content and summary.",
            "parameters": {
                "type": "object",
                "properties": {
                    "node_id": {"type": "string", "description": "The nodeId to retrieve"}
                },
                "required": ["node_id"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "get_file_structure",
            "description": "Get all leaf nodes within a specific file with their summaries. Useful to understand the full context of a file.",
            "parameters": {
                "type": "object",
                "properties": {
                    "file_name": {"type": "string", "description": "File name or partial path to search for"}
                },
                "required": ["file_name"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "search_by_symbol",
            "description": "Search for code nodes whose summary OR source code contains a specific symbol, function name, type, or variable.",
            "parameters": {
                "type": "object",
                "properties": {
                    "symbol": {"type": "string", "description": "The symbol name to search for"},
                    "limit": {"type": "integer", "description": "Max results (default 15)"}
                },
                "required": ["symbol"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "run_cypher",
            "description": "Execute an arbitrary Cypher query against the Neo4j graph. Node properties: nodeId, name, type, file, lineStart, lineEnd, summary, content, label. Edge types: CONTAINS, SUCCESSOR, PREDECESSOR, EMBEDDING (has similarity property). Node labels: Project, Directory, File, Code, Parent.",
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "The Cypher query to execute"},
                    "limit": {"type": "integer", "description": "Max rows (default 20)"}
                },
                "required": ["query"]
            }
        }
    }
]

SYSTEM_PROMPT = """You are a code impact analyst. You are investigating the system-wide impact of a specific code section within a larger software project.

You have access to a Neo4j graph database containing:
- All code sections (Code nodes) with their source code, summaries, and file locations
- EMBEDDING edges connecting semantically similar code sections (with similarity scores)
- SUCCESSOR/PREDECESSOR edges connecting sequential code within files
- CONTAINS edges representing the project hierarchy (Project -> Directory -> File -> Code)

Your goal: Explain how the target code section affects the rest of the system. Specifically:
1. What other code depends on symbols this section defines or modifies?
2. What would break or change if this code were modified?
3. What is the data/control flow path from this code through the system?
4. Are there similar code patterns elsewhere that might need synchronized changes?

Use the available tools to explore the codebase. Start with the target node's direct connections, then follow dependency chains outward. When you find a relevant connection, explain WHY it matters.

IMPORTANT: When writing ANY summary or analysis — whether preliminary or final — you MUST include:
- Every impacted node ID (e.g. "node 00205")
- Every impacted function name (fully qualified, e.g. RSA_decrypt())
- Every impacted class/struct/type name
- Every impacted file path
Never use vague language like "several nodes" or "various functions" — list them ALL by exact ID and name.

Write your final analysis as a structured report with: Impact Summary, Direct Dependencies, Transitive Impact, Similar Code Patterns, and Risk Assessment."""

COMPACTION_PROMPT = """You have used all available tool calls for this exploration phase. Write a PRELIMINARY summary of your findings so far. This summary will be your ONLY context for the next exploration phase — include every detail that matters:

- All impacted node IDs with their names and files (e.g. "node 00205, RSA_decrypt in crypto/rsa.c")
- All impacted functions, classes, and types by exact fully qualified name
- All dependency chains you discovered (A -> B -> C with node IDs)
- What you have NOT yet investigated and should explore next

This is NOT your final report — you will get more tool calls after this. Be thorough and precise so nothing is lost when context resets."""


# ---------------------------------------------------------------------------
# Neo4j tool execution
# ---------------------------------------------------------------------------

def filter_visited(results, visited_nodes):
    """Replace summaries of already-visited nodes with a short marker.

    Tracks new nodes in visited_nodes. Returns filtered results.
    """
    filtered = []
    for r in results:
        nid = r.get("nodeId", "")
        if not nid:
            filtered.append(r)
            continue
        if nid in visited_nodes:
            filtered.append({
                "nodeId": nid,
                "name": r.get("name", ""),
                "file": r.get("file", ""),
                "note": "(already explored — summary omitted)"
            })
        else:
            visited_nodes[nid] = {
                "name": r.get("name", ""),
                "file": r.get("file", ""),
            }
            filtered.append(r)
    return filtered


def execute_tool(neo4j_session, tool_name, args, visited_nodes):
    """Execute a tool call against Neo4j and return the result."""
    try:
        if tool_name == "get_similar_code":
            result = _get_similar_code(neo4j_session, args)
            return filter_visited(result, visited_nodes)
        elif tool_name == "get_neighbors":
            result = _get_neighbors(neo4j_session, args)
            return filter_visited(result, visited_nodes)
        elif tool_name == "get_node_details":
            # Always return full details including summary
            result = _get_node_details(neo4j_session, args)
            nid = result.get("nodeId", "")
            if nid and nid not in visited_nodes:
                visited_nodes[nid] = {
                    "name": result.get("name", ""),
                    "file": result.get("file", ""),
                }
            return result
        elif tool_name == "get_file_structure":
            result = _get_file_structure(neo4j_session, args)
            return filter_visited(result, visited_nodes)
        elif tool_name == "search_by_symbol":
            result = _search_by_symbol(neo4j_session, args)
            return filter_visited(result, visited_nodes)
        elif tool_name == "run_cypher":
            result = _run_cypher(neo4j_session, args)
            if isinstance(result, list):
                return filter_visited(result, visited_nodes)
            return result
        else:
            return {"error": f"Unknown tool: {tool_name}"}
    except Exception as e:
        return {"error": str(e)}


def _get_similar_code(session, args):
    node_id = args["node_id"]
    limit = args.get("limit", 10)
    result = session.run(
        "MATCH (a:Code {nodeId: $nid})-[r:EMBEDDING]-(b:Code) "
        "RETURN b.nodeId AS nodeId, b.name AS name, b.file AS file, "
        "b.lineStart AS lineStart, b.summary AS summary, r.similarity AS similarity "
        "ORDER BY r.similarity DESC LIMIT $limit",
        nid=node_id, limit=limit
    )
    return [dict(r) for r in result]


def _get_neighbors(session, args):
    node_id = args["node_id"]
    direction = args.get("direction", "both")
    results = []
    if direction in ("successor", "both"):
        result = session.run(
            "MATCH (a:Code {nodeId: $nid})-[:SUCCESSOR]->(b:Code) "
            "RETURN b.nodeId AS nodeId, b.name AS name, b.file AS file, "
            "b.lineStart AS lineStart, b.summary AS summary",
            nid=node_id
        )
        for r in result:
            d = dict(r)
            d["direction"] = "successor"
            results.append(d)
    if direction in ("predecessor", "both"):
        result = session.run(
            "MATCH (a:Code {nodeId: $nid})-[:PREDECESSOR]->(b:Code) "
            "RETURN b.nodeId AS nodeId, b.name AS name, b.file AS file, "
            "b.lineStart AS lineStart, b.summary AS summary",
            nid=node_id
        )
        for r in result:
            d = dict(r)
            d["direction"] = "predecessor"
            results.append(d)
    return results


def _get_node_details(session, args):
    node_id = args["node_id"]
    # Get the node itself
    result = session.run(
        "MATCH (n:Node {nodeId: $nid}) "
        "RETURN n.nodeId AS nodeId, n.name AS name, n.type AS type, "
        "n.file AS file, n.lineStart AS lineStart, n.lineEnd AS lineEnd, "
        "n.summary AS summary, n.content AS content, n.label AS label",
        nid=node_id
    )
    records = [dict(r) for r in result]
    if not records:
        return {"error": "Node not found"}
    node_info = records[0]

    # Walk up the CONTAINS hierarchy to root, collecting parent context
    hierarchy = []
    current_id = node_id
    for _ in range(20):  # safety limit
        parent_result = session.run(
            "MATCH (parent:Node)-[:CONTAINS]->(child:Node {nodeId: $nid}) "
            "RETURN parent.nodeId AS nodeId, parent.name AS name, parent.type AS type, "
            "parent.content AS content, parent.summary AS summary, parent.label AS label",
            nid=current_id
        )
        parent_records = [dict(r) for r in parent_result]
        if not parent_records:
            break
        parent = parent_records[0]
        hierarchy.append({
            "nodeId": parent["nodeId"],
            "name": parent["name"],
            "type": parent["type"],
            "content": (parent.get("content") or "")[:500],  # truncate large content
            "summary": parent.get("summary") or "",
        })
        current_id = parent["nodeId"]

    hierarchy.reverse()  # root first
    node_info["hierarchy"] = hierarchy
    return node_info


def _get_file_structure(session, args):
    file_name = args["file_name"]
    limit = args.get("limit", 20)
    result = session.run(
        "MATCH (f:File)-[:CONTAINS*]->(c:Code) "
        "WHERE f.name CONTAINS $fname "
        "RETURN c.nodeId AS nodeId, c.name AS name, f.name AS file, "
        "c.lineStart AS lineStart, c.lineEnd AS lineEnd, c.summary AS summary "
        "ORDER BY c.lineStart LIMIT $limit",
        fname=file_name, limit=limit
    )
    return [dict(r) for r in result]


def _search_by_symbol(session, args):
    symbol = args["symbol"]
    limit = args.get("limit", 15)
    result = session.run(
        "MATCH (n:Code) WHERE n.summary CONTAINS $sym OR n.content CONTAINS $sym "
        "RETURN n.nodeId AS nodeId, n.name AS name, n.file AS file, "
        "n.lineStart AS lineStart, n.summary AS summary "
        "LIMIT $limit",
        sym=symbol, limit=limit
    )
    return [dict(r) for r in result]


def _run_cypher(session, args):
    query = args["query"]
    limit = args.get("limit", 20)
    # Safety: append LIMIT if not present
    if "LIMIT" not in query.upper():
        query += f" LIMIT {limit}"
    result = session.run(query)
    return [dict(r) for r in result]


# ---------------------------------------------------------------------------
# Tool call parsing
# ---------------------------------------------------------------------------

def parse_tool_calls(text):
    """Parse tool calls from model output.

    Handles multiple formats:

    1. Hermes/JSON format:
       <tool_call>
       {"name": "func_name", "arguments": {"arg": "val"}}
       </tool_call>

    2. Qwen XML format:
       <tool_call>
       <function=func_name>
       <parameter=arg>val</parameter>
       </function>
       </tool_call>
    """
    tool_calls = []

    # Extract all <tool_call>...</tool_call> blocks
    blocks = re.findall(r"<tool_call>(.*?)</tool_call>", text, re.DOTALL)

    for block in blocks:
        block = block.strip()

        # Try JSON format first
        try:
            parsed = json.loads(block)
            name = parsed.get("name", "")
            arguments = parsed.get("arguments", {})
            if name:
                tool_calls.append({"name": name, "arguments": arguments})
            continue
        except (json.JSONDecodeError, ValueError):
            pass

        # Try Qwen XML format: <function=name><parameter=key>value</parameter></function>
        func_match = re.search(r"<function=(\w+)>(.*?)</function>", block, re.DOTALL)
        if func_match:
            name = func_match.group(1)
            params_text = func_match.group(2)
            arguments = {}
            for param_match in re.finditer(
                r"<parameter=(\w+)>\s*(.*?)\s*</parameter>", params_text, re.DOTALL
            ):
                key = param_match.group(1)
                val = param_match.group(2).strip()
                # Try to parse as int/float/bool
                if val.isdigit():
                    val = int(val)
                elif val.replace(".", "", 1).isdigit():
                    val = float(val)
                elif val.lower() in ("true", "false"):
                    val = val.lower() == "true"
                arguments[key] = val
            if name:
                tool_calls.append({"name": name, "arguments": arguments})

    return tool_calls


def truncate_result(result, max_chars=MAX_TOOL_RESULT_CHARS):
    """Truncate tool result to fit within context limits."""
    text = json.dumps(result, default=str)
    if len(text) <= max_chars:
        return text
    # For lists, truncate items
    if isinstance(result, list) and len(result) > 1:
        while len(json.dumps(result, default=str)) > max_chars and len(result) > 1:
            result = result[:-1]
        return json.dumps(result, default=str) + "\n... (truncated)"
    return text[:max_chars] + "... (truncated)"


# ---------------------------------------------------------------------------
# Context building
# ---------------------------------------------------------------------------

def build_node_index(node, index=None, parent_id=None):
    if index is None:
        index = {}
    nid = node.get("id", "")
    if nid:
        index[nid] = node
        node["_parentId"] = parent_id
    for child in node.get("children", []):
        build_node_index(child, index, nid)
    return index


def build_hierarchy_path(node_id, node_map):
    """Build a human-readable hierarchy path: Project > Dir > File > ..."""
    parts = []
    current = node_id
    while current:
        node = node_map.get(current)
        if not node:
            break
        parts.append(f"{node.get('type', '')}:{node.get('name', '')}")
        current = node.get("_parentId", "")
    parts.reverse()
    return " > ".join(parts)


def build_initial_user_message(node, hierarchy_path):
    """Build the initial user message with full node context."""
    node_id = node.get("id", "")
    name = node.get("name", "")
    file_path = node.get("file", "")
    line_start = node.get("lineStart", -1)
    line_end = node.get("lineEnd", -1)
    summary = node.get("summary", "(no summary)")
    content = node.get("content", "(no code)")

    return (
        f"Investigate the system-wide impact of this code section:\n\n"
        f"**Node:** {node_id} ({name})\n"
        f"**File:** {file_path}:{line_start}-{line_end}\n"
        f"**Hierarchy:** {hierarchy_path}\n\n"
        f"**Summary:**\n{summary}\n\n"
        f"**Code:**\n```\n{content}\n```\n\n"
        f"Use the available tools to explore the codebase graph and build a "
        f"comprehensive impact analysis. Start by finding what other code "
        f"references symbols defined here, then follow the chains outward."
    )


# ---------------------------------------------------------------------------
# Main investigation loop
# ---------------------------------------------------------------------------

def build_visited_summary(visited_nodes):
    """Build a list of visited nodes for the result."""
    return [
        {"nodeId": nid, "name": info["name"], "file": info["file"]}
        for nid, info in visited_nodes.items()
    ]


def build_compacted_messages(node, hierarchy_path, preliminary_summary):
    """Build messages for a new phase after context compaction."""
    node_id = node.get("id", "")
    content = node.get("content", "")

    user_msg = (
        f"Continue investigating the impact of this code section:\n\n"
        f"**Node:** {node_id} ({node.get('name', '')})\n"
        f"**File:** {node.get('file', '')}:{node.get('lineStart', -1)}-{node.get('lineEnd', -1)}\n"
        f"**Hierarchy:** {hierarchy_path}\n\n"
        f"**Code:**\n```\n{content}\n```\n\n"
        f"**Your findings from the previous exploration phase:**\n{preliminary_summary}\n\n"
        f"Continue using the available tools to explore areas you identified as "
        f"needing further investigation. When you have enough information, write "
        f"your final structured analysis."
    )

    return [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content": user_msg},
    ]


def run_investigation(tree_path, node_id):
    """Run the agentic investigation loop."""
    # Load tree and find node
    log("Loading chunk tree...")
    with open(tree_path, "r") as f:
        tree = json.load(f)

    node_map = build_node_index(tree)
    node = node_map.get(node_id)
    if not node:
        emit("error", message=f"Node {node_id} not found in chunk tree.")
        return

    hierarchy_path = build_hierarchy_path(node_id, node_map)
    log(f"Investigating: {node.get('name', '')} in {node.get('file', '')}")

    # Clean up parent refs
    for nid, n in node_map.items():
        n.pop("_parentId", None)

    # Connect to Neo4j
    log("Connecting to Neo4j...")
    from neo4j import GraphDatabase
    driver = GraphDatabase.driver(NEO4J_URI)
    session = driver.session()

    # Verify connection
    try:
        session.run("RETURN 1").single()
        log("Neo4j connected.")
    except Exception as e:
        emit("error", message=f"Cannot connect to Neo4j: {e}")
        return

    # Initialize vLLM
    log(f"Initializing vLLM with {MODEL_NAME}...")
    from vllm import LLM, SamplingParams

    llm = LLM(
        model=MODEL_NAME,
        tensor_parallel_size=VLLM_SETTINGS["tensor_parallel_size"],
        gpu_memory_utilization=VLLM_SETTINGS["gpu_memory_utilization"],
        max_model_len=VLLM_SETTINGS["max_model_len"],
        max_num_seqs=VLLM_SETTINGS["max_num_seqs"],
        dtype=VLLM_SETTINGS["dtype"],
    )
    sampling_params = SamplingParams(
        temperature=SAMPLING_PARAMS_SETTINGS["temperature"],
        max_tokens=SAMPLING_PARAMS_SETTINGS["max_tokens"],
        top_p=SAMPLING_PARAMS_SETTINGS["top_p"],
    )
    log("vLLM ready.")

    # Build conversation
    messages = [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content": build_initial_user_message(node, hierarchy_path)},
    ]

    # Phased agentic loop with context compaction
    visited_nodes = {}  # nodeId -> {name, file}
    visited_nodes[node_id] = {
        "name": node.get("name", ""),
        "file": node.get("file", ""),
    }
    total_tool_calls = 0
    total_iterations = 0
    start_time = time.time()
    preliminary_summary = None
    investigation_done = False

    for phase in range(MAX_PHASES):
        # Build messages for this phase
        if phase == 0:
            messages = [
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": build_initial_user_message(node, hierarchy_path)},
            ]
        else:
            log(f"Phase {phase + 1}: Compacted context with {len(visited_nodes)} visited nodes.")
            messages = build_compacted_messages(node, hierarchy_path, preliminary_summary)

        for iteration in range(MAX_ITERATIONS_PER_PHASE):
            total_iterations += 1
            log(f"Phase {phase + 1}, iteration {iteration + 1}/{MAX_ITERATIONS_PER_PHASE} "
                f"(total: {total_iterations}, visited: {len(visited_nodes)} nodes)...")

            outputs = llm.chat(
                messages=[messages],
                sampling_params=sampling_params,
                tools=TOOLS,
                chat_template_kwargs={"enable_thinking": False},
            )

            response_text = outputs[0].outputs[0].text
            tool_calls = parse_tool_calls(response_text)

            if tool_calls:
                messages.append({"role": "assistant", "content": response_text})

                for tc in tool_calls:
                    tool_name = tc["name"]
                    tool_args = tc["arguments"]
                    args_short = json.dumps(tool_args)[:80]
                    log(f"Tool: {tool_name}({args_short})")

                    result = execute_tool(session, tool_name, tool_args, visited_nodes)
                    result_count = len(result) if isinstance(result, list) else 1
                    total_tool_calls += 1
                    emit("tool", name=tool_name, args=args_short, resultCount=result_count)

                    result_str = truncate_result(result)
                    messages.append({
                        "role": "tool",
                        "name": tool_name,
                        "content": result_str,
                    })
            else:
                # No tool calls — final analysis
                analysis = response_text.strip()
                elapsed = time.time() - start_time
                log(f"Investigation complete in {elapsed:.1f}s "
                    f"({total_iterations} iterations, {phase + 1} phases, "
                    f"{total_tool_calls} tool calls, {len(visited_nodes)} nodes visited)")
                visited_summary = build_visited_summary(visited_nodes)
                emit("result", analysis=analysis, visitedNodes=visited_summary,
                     visitedCount=len(visited_nodes), toolCalls=total_tool_calls)
                investigation_done = True
                break

        if investigation_done:
            break

        # Phase exhausted — force preliminary summary for compaction
        if phase < MAX_PHASES - 1:
            log(f"Phase {phase + 1} exhausted. Requesting preliminary summary for compaction...")
            messages.append({"role": "user", "content": COMPACTION_PROMPT})
            outputs = llm.chat(
                messages=[messages],
                sampling_params=sampling_params,
                chat_template_kwargs={"enable_thinking": False},
            )
            preliminary_summary = outputs[0].outputs[0].text.strip()
            log(f"Preliminary summary: {len(preliminary_summary)} chars. Starting next phase...")
        else:
            # Final phase exhausted — force final analysis
            log(f"All phases exhausted. Forcing final analysis...")
            messages.append({
                "role": "user",
                "content": "You have used all available exploration phases. "
                           "Write your FINAL impact analysis now based on everything "
                           "you have learned. Include every impacted node ID, function, "
                           "class, and file."
            })
            outputs = llm.chat(
                messages=[messages],
                sampling_params=sampling_params,
                chat_template_kwargs={"enable_thinking": False},
            )
            analysis = outputs[0].outputs[0].text.strip()
            elapsed = time.time() - start_time
            log(f"Investigation complete in {elapsed:.1f}s "
                f"({total_iterations} iterations, {MAX_PHASES} phases, "
                f"{total_tool_calls} tool calls, {len(visited_nodes)} nodes visited)")
            visited_summary = build_visited_summary(visited_nodes)
            emit("result", analysis=analysis, visitedNodes=visited_summary,
                 visitedCount=len(visited_nodes), toolCalls=total_tool_calls)

    # Cleanup
    session.close()
    driver.close()
    del llm
    import torch
    if torch.cuda.is_available():
        torch.cuda.empty_cache()


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main():
    if len(sys.argv) < 3:
        emit("error", message="Usage: investigate.py <chunk_tree.json> <node_id>")
        sys.exit(1)

    tree_path = os.path.abspath(sys.argv[1])
    node_id = sys.argv[2]

    if not os.path.exists(tree_path):
        emit("error", message=f"File not found: {tree_path}")
        sys.exit(1)

    try:
        run_investigation(tree_path, node_id)
    except Exception as e:
        emit("error", message=f"Investigation failed: {e}")
        import traceback
        log(traceback.format_exc())
        sys.exit(1)


if __name__ == "__main__":
    main()
