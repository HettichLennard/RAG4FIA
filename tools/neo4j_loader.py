#!/usr/bin/env python3
"""
Neo4j Data Loader

Generates CSV files from chunk_tree.json and edge files, starts a Neo4j
Docker container, and loads the data via LOAD CSV + cypher-shell.

Usage:
  neo4j_loader.py start <vdg_output_dir>
  neo4j_loader.py stop

Communication protocol (JSON lines on stdout):
  {"type": "log", "message": "..."}
  {"type": "done", "action": "started|stopped"}
  {"type": "error", "message": "..."}
"""

import sys
import os
import json
import csv
import time
import subprocess

CONTAINER_NAME = "vdg-neo4j"
NEO4J_IMAGE = "neo4j:5-community"
NEO4J_HTTP_PORT = 7474
NEO4J_BOLT_PORT = 7687


def emit(msg_type, **kwargs):
    print(json.dumps({"type": msg_type, **kwargs}), flush=True)


def log(message):
    emit("log", message=message)


def run_cmd(args, timeout=120):
    """Run a command and return (returncode, stdout, stderr)."""
    try:
        result = subprocess.run(args, capture_output=True, text=True, timeout=timeout)
        return result.returncode, result.stdout.strip(), result.stderr.strip()
    except subprocess.TimeoutExpired:
        return -1, "", "Command timed out"
    except FileNotFoundError:
        return -1, "", f"Command not found: {args[0]}"


def container_exists():
    """Check if the Neo4j container exists (running or stopped)."""
    rc, out, _ = run_cmd(["docker", "ps", "-a", "--filter", f"name=^/{CONTAINER_NAME}$", "--format", "{{.Names}}"])
    return rc == 0 and CONTAINER_NAME in out


def container_running():
    """Check if the Neo4j container is currently running."""
    rc, out, _ = run_cmd(["docker", "ps", "--filter", f"name=^/{CONTAINER_NAME}$", "--format", "{{.Names}}"])
    return rc == 0 and CONTAINER_NAME in out


def stop_and_remove():
    """Stop and remove the Neo4j container."""
    if container_running():
        log("Stopping Neo4j container...")
        run_cmd(["docker", "stop", CONTAINER_NAME], timeout=30)
    if container_exists():
        log("Removing Neo4j container...")
        run_cmd(["docker", "rm", CONTAINER_NAME], timeout=10)
    log("Neo4j container removed.")


def build_node_index(node, index=None, parent_id=None):
    """Build flat node index with parent tracking."""
    if index is None:
        index = {}
    nid = node.get("id", "")
    if nid:
        index[nid] = node
        node["_parentId"] = parent_id
    for child in node.get("children", []):
        build_node_index(child, index, nid)
    return index


def determine_label(node):
    """Determine the Neo4j label for a node."""
    ntype = node.get("type", "")
    children = node.get("children", [])
    if ntype == "PROJECT":
        return "Project"
    elif ntype == "DIRECTORY":
        return "Directory"
    elif ntype == "FILE":
        return "File"
    elif ntype == "PC":
        return "PC"
    elif ntype in ("if_pc", "elif_pc", "else_pc"):
        return "PCAlternative"
    elif not children and node.get("content", ""):
        return "Code"
    else:
        return "Parent"


def sanitize_for_neo4j(value):
    """Sanitize a value for Neo4j LOAD CSV compatibility.

    Neo4j's LOAD CSV parser is stricter than Python's csv.writer.
    Remove characters that cause parse failures.
    """
    if value is None:
        return ""
    s = str(value)
    # Replace newlines with spaces (Neo4j LOAD CSV doesn't handle multiline well)
    s = s.replace("\r\n", " ").replace("\n", " ").replace("\r", " ").replace("\t", " ")
    # Remove backticks and double quotes — they conflict with CSV quoting
    s = s.replace("`", "'").replace('"', "'")
    # Collapse multiple spaces
    import re
    s = re.sub(r"  +", " ", s).strip()
    return s


def generate_csvs(vdg_dir, tree):
    """Generate Neo4j-compatible CSV files from tree and edge data."""
    node_map = build_node_index(tree)
    import tempfile
    csv_dir = tempfile.mkdtemp(prefix="vdg_neo4j_csv_")

    # --- Nodes CSV ---
    nodes_path = os.path.join(csv_dir, "nodes.csv")
    with open(nodes_path, "w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow(["nodeId", "label", "name", "type", "file", "lineStart", "lineEnd", "summary", "content", "pcColor", "pcCondition", "pcDepth"])
        for nid, node in node_map.items():
            writer.writerow([
                nid,
                determine_label(node),
                sanitize_for_neo4j(node.get("name", "")),
                sanitize_for_neo4j(node.get("type", "")),
                sanitize_for_neo4j(node.get("file", "")),
                node.get("lineStart", -1),
                node.get("lineEnd", -1),
                sanitize_for_neo4j(node.get("summary", "")),
                sanitize_for_neo4j(node.get("content", "")),
                node.get("pcColor", ""),
                sanitize_for_neo4j(node.get("pcCondition", "")),
                node.get("pcDepth", -1),
            ])

    # --- CONTAINS edges (hierarchical parent -> child) ---
    contains_path = os.path.join(csv_dir, "contains.csv")
    with open(contains_path, "w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow(["startId", "endId"])
        for nid, node in node_map.items():
            parent_id = node.get("_parentId", "")
            if parent_id:
                writer.writerow([parent_id, nid])

    # --- Structural edges (successor/predecessor from edges.csv) ---
    structural_path = os.path.join(csv_dir, "structural_edges.csv")
    edges_csv = os.path.join(vdg_dir, "edges.csv")
    with open(structural_path, "w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow(["startId", "endId", "edgeType", "srcName", "dstName"])
        if os.path.exists(edges_csv):
            with open(edges_csv, "r") as ef:
                reader = csv.reader(ef)
                next(reader, None)  # skip header
                for row in reader:
                    if len(row) >= 9:
                        writer.writerow([row[1], row[5], row[0], row[2], row[6]])

    # --- Embedding + PROVIDES edges (from similarity_symbol_edges.csv) ---
    embedding_path = os.path.join(csv_dir, "embedding_edges.csv")
    provides_path = os.path.join(csv_dir, "provides_edges.csv")
    sim_csv = os.path.join(vdg_dir, "similarity_symbol_edges.csv")

    with open(embedding_path, "w", newline="", encoding="utf-8") as ef, \
         open(provides_path, "w", newline="", encoding="utf-8") as pf:
        emb_writer = csv.writer(ef)
        prov_writer = csv.writer(pf)
        emb_writer.writerow(["startId", "endId", "similarity"])
        prov_writer.writerow(["startId", "endId", "symbol"])
        if os.path.exists(sim_csv):
            with open(sim_csv, "r") as sf:
                reader = csv.reader(sf)
                next(reader, None)  # skip header
                for row in reader:
                    if len(row) >= 9:
                        edge_type = row[0]
                        if edge_type == "EMBEDDING" and len(row) >= 10:
                            emb_writer.writerow([row[1], row[5], row[9]])
                        elif edge_type == "PROVIDES" and len(row) >= 11:
                            prov_writer.writerow([row[1], row[5], row[10]])

    # Clean up internal fields
    for nid, node in node_map.items():
        node.pop("_parentId", None)

    log(f"CSV files written to {csv_dir}")
    return csv_dir


def wait_for_neo4j(timeout=60):
    """Wait for Neo4j to be ready."""
    log("Waiting for Neo4j to start...")
    start = time.time()
    while time.time() - start < timeout:
        rc, out, _ = run_cmd([
            "docker", "exec", CONTAINER_NAME,
            "cypher-shell", "RETURN 1;"
        ], timeout=5)
        if rc == 0:
            return True
        time.sleep(2)
    return False


def run_cypher(query, timeout=60):
    """Execute a Cypher query via cypher-shell."""
    rc, out, err = run_cmd([
        "docker", "exec", CONTAINER_NAME,
        "cypher-shell", query
    ], timeout=timeout)
    if rc != 0:
        log(f"Cypher error: {err}")
    return rc == 0


def load_data_into_neo4j():
    """Load CSV data into Neo4j using LOAD CSV."""
    log("Loading nodes...")
    ok = run_cypher("""
LOAD CSV WITH HEADERS FROM 'file:///nodes.csv' AS row
CALL {
  WITH row
  WITH row, CASE row.label
    WHEN 'Project' THEN ['Project']
    WHEN 'Directory' THEN ['Directory']
    WHEN 'File' THEN ['File']
    WHEN 'Code' THEN ['Code']
    ELSE ['Parent']
  END AS labels
  CREATE (n:Node {
    nodeId: row.nodeId,
    name: row.name,
    type: row.type,
    file: row.file,
    lineStart: toInteger(row.lineStart),
    lineEnd: toInteger(row.lineEnd),
    summary: row.summary,
    content: row.content,
    label: row.label,
    pcColor: row.pcColor,
    pcCondition: row.pcCondition,
    pcDepth: toInteger(row.pcDepth)
  })
} IN TRANSACTIONS OF 500 ROWS;
""")
    if not ok:
        return False

    # Add label-specific secondary labels
    for label in ["Project", "Directory", "File", "Code", "Parent", "PC", "PCAlternative"]:
        run_cypher(f"MATCH (n:Node {{label: '{label}'}}) SET n:{label};")

    log("Creating index on nodeId...")
    run_cypher("CREATE INDEX node_id_idx IF NOT EXISTS FOR (n:Node) ON (n.nodeId);")
    # Wait a moment for index to be built
    time.sleep(1)

    log("Loading CONTAINS edges...")
    ok = run_cypher("""
LOAD CSV WITH HEADERS FROM 'file:///contains.csv' AS row
CALL {
  WITH row
  MATCH (parent:Node {nodeId: row.startId})
  MATCH (child:Node {nodeId: row.endId})
  CREATE (parent)-[:CONTAINS]->(child)
} IN TRANSACTIONS OF 500 ROWS;
""")
    if not ok:
        return False

    log("Loading structural edges...")
    run_cypher("""
LOAD CSV WITH HEADERS FROM 'file:///structural_edges.csv' AS row
CALL {
  WITH row
  MATCH (src:Node {nodeId: row.startId})
  MATCH (dst:Node {nodeId: row.endId})
  FOREACH (_ IN CASE WHEN row.edgeType = 'SUCCESSOR' THEN [1] ELSE [] END |
    CREATE (src)-[:SUCCESSOR]->(dst)
  )
  FOREACH (_ IN CASE WHEN row.edgeType = 'PREDECESSOR' THEN [1] ELSE [] END |
    CREATE (src)-[:PREDECESSOR]->(dst)
  )
  FOREACH (_ IN CASE WHEN row.edgeType <> 'SUCCESSOR' AND row.edgeType <> 'PREDECESSOR' THEN [1] ELSE [] END |
    CREATE (src)-[:STRUCTURAL {type: row.edgeType}]->(dst)
  )
} IN TRANSACTIONS OF 500 ROWS;
""")

    log("Loading EMBEDDING edges...")
    run_cypher("""
LOAD CSV WITH HEADERS FROM 'file:///embedding_edges.csv' AS row
CALL {
  WITH row
  MATCH (src:Node {nodeId: row.startId})
  MATCH (dst:Node {nodeId: row.endId})
  CREATE (src)-[:EMBEDDING {similarity: toFloat(row.similarity)}]->(dst)
} IN TRANSACTIONS OF 500 ROWS;
""")

    log("Loading PROVIDES edges...")
    run_cypher("""
LOAD CSV WITH HEADERS FROM 'file:///provides_edges.csv' AS row
CALL {
  WITH row
  MATCH (src:Node {nodeId: row.startId})
  MATCH (dst:Node {nodeId: row.endId})
  CREATE (src)-[:PROVIDES {symbol: row.symbol}]->(dst)
} IN TRANSACTIONS OF 500 ROWS;
""")

    # Count results
    rc, out, _ = run_cmd([
        "docker", "exec", CONTAINER_NAME,
        "cypher-shell", "MATCH (n) RETURN count(n) AS nodes;"
    ])
    log(f"Neo4j loaded: {out}")
    rc, out, _ = run_cmd([
        "docker", "exec", CONTAINER_NAME,
        "cypher-shell", "MATCH ()-[r]->() RETURN type(r) AS type, count(r) AS count;"
    ])
    log(f"Relationships: {out}")

    return True


def start_neo4j(vdg_dir):
    """Generate CSVs, start Neo4j container, and load data."""
    tree_path = os.path.join(vdg_dir, "chunk_tree.json")
    if not os.path.exists(tree_path):
        emit("error", message=f"chunk_tree.json not found in {vdg_dir}")
        return

    log("Loading chunk tree...")
    with open(tree_path, "r") as f:
        tree = json.load(f)

    csv_dir = generate_csvs(vdg_dir, tree)

    # Stop existing container if any
    if container_exists():
        stop_and_remove()

    # Pull image if needed
    log(f"Ensuring Neo4j image ({NEO4J_IMAGE}) is available...")
    rc, _, err = run_cmd(["docker", "pull", NEO4J_IMAGE], timeout=300)
    if rc != 0:
        # Image might already exist locally
        log(f"Pull warning (may already exist): {err}")

    # Start container with CSV directory mounted
    log("Starting Neo4j container...")
    rc, _, err = run_cmd([
        "docker", "run", "-d",
        "--name", CONTAINER_NAME,
        "-e", "NEO4J_AUTH=none",
        "-p", f"{NEO4J_HTTP_PORT}:7474",
        "-p", f"{NEO4J_BOLT_PORT}:7687",
        "-v", f"{csv_dir}:/var/lib/neo4j/import",
        NEO4J_IMAGE,
    ])
    if rc != 0:
        emit("error", message=f"Failed to start container: {err}")
        return

    if not wait_for_neo4j(timeout=60):
        # Show container logs on failure
        _, logs, _ = run_cmd(["docker", "logs", "--tail", "20", CONTAINER_NAME])
        if logs:
            log(f"Container logs:\n{logs}")
        emit("error", message="Neo4j failed to start within 60 seconds.")
        stop_and_remove()
        return

    # Show startup logs
    _, logs, _ = run_cmd(["docker", "logs", "--tail", "5", CONTAINER_NAME])
    if logs:
        for line in logs.split("\n"):
            if line.strip():
                log(f"[neo4j] {line.strip()}")

    log("Neo4j is ready. Loading data...")
    if load_data_into_neo4j():
        log(f"Neo4j is running. Browser: http://localhost:{NEO4J_HTTP_PORT}  Bolt: bolt://localhost:{NEO4J_BOLT_PORT}")
        log("--- Example Cypher queries ---")
        log("Entire graph:  MATCH (n)-[r]->(m) RETURN n, r, m LIMIT 500")
        log("Leaf nodes only:  MATCH (a:Code)-[r:SUCCESSOR|PREDECESSOR|EMBEDDING|PROVIDES]->(b:Code) RETURN a, r, b")
        log("Embedding edges:  MATCH (a:Code)-[r:EMBEDDING]->(b:Code) RETURN a, r, b")
        log("PROVIDES edges:  MATCH (a:Code)-[r:PROVIDES]->(b:Code) RETURN a.name, r.symbol, b.name, b.file")
        log("What depends on a symbol:  MATCH (a:Code)-[r:PROVIDES]->(b:Code) WHERE r.symbol CONTAINS 'malloc' RETURN a.name, b.name, r.symbol")
        log("Impact of a node:  MATCH (a:Code {nodeId: 'YOUR_ID'})-[r:PROVIDES]->(b:Code) RETURN b.name, b.file, r.symbol")
        log("What would break:  MATCH (a:Code {nodeId: 'YOUR_ID'})-[r:PROVIDES]->(b:Code) WHERE NOT EXISTS { MATCH (other:Code)-[:PROVIDES {symbol: r.symbol}]->(b) WHERE other.nodeId <> a.nodeId } RETURN b.name, r.symbol AS exclusiveDep")
        log("Successor chain:  MATCH p=(a:Code)-[:SUCCESSOR*]->(b:Code) WHERE NOT ()-[:SUCCESSOR]->(a) RETURN p LIMIT 10")
        log("Most connected (all edges):  MATCH (n:Code)-[r]-() RETURN n.name, n.file, count(r) AS edges ORDER BY edges DESC LIMIT 20")
        log("Hierarchy of a file:  MATCH p=(f:File)-[:CONTAINS*]->(c:Code) WHERE f.name CONTAINS 'AStar' RETURN p")
        emit("done", action="started")
    else:
        emit("error", message="Failed to load data into Neo4j.")


def main():
    if len(sys.argv) < 2:
        emit("error", message="Usage: neo4j_loader.py start <vdg_output_dir> | stop")
        sys.exit(1)

    action = sys.argv[1]

    if action == "stop":
        if container_exists():
            stop_and_remove()
            emit("done", action="stopped")
        else:
            log("No Neo4j container found.")
            emit("done", action="stopped")
    elif action == "start":
        if len(sys.argv) < 3:
            emit("error", message="Usage: neo4j_loader.py start <vdg_output_dir>")
            sys.exit(1)
        vdg_dir = os.path.abspath(sys.argv[2])
        start_neo4j(vdg_dir)
    else:
        emit("error", message=f"Unknown action: {action}")
        sys.exit(1)


if __name__ == "__main__":
    main()
