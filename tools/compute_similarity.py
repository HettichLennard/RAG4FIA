#!/usr/bin/env python3
"""
Edge Generator: Symbol Matching + Embeddings

Loads leaf nodes from chunk_tree.json, computes:
1. PROVIDES edges from symbol matching (consumes/produces)
2. EMBEDDING edges from cosine similarity (CodeSage)

Writes both to similarity_symbol_edges.csv.

Usage: compute_similarity.py <path_to_chunk_tree.json> [--top-percent N]

Communication protocol (JSON lines on stdout):
  {"type": "log", "message": "..."}
  {"type": "progress", "phase": "...", "current": N, "total": N}
  {"type": "done"}
  {"type": "error", "message": "..."}
"""

import sys
import os
import json
import time
import re
from collections import Counter

EMBEDDING_MODEL = "codesage/codesage-large-v2"
DEFAULT_TOP_PERCENT = 5


def emit(msg_type, **kwargs):
    print(json.dumps({"type": msg_type, **kwargs}), flush=True)


def log(message):
    emit("log", message=message)


def fix_codesage_compat():
    """Add Conv1D compatibility shim for newer transformers versions."""
    try:
        from transformers.modeling_utils import Conv1D  # noqa: F401
    except ImportError:
        try:
            from transformers.pytorch_utils import Conv1D
            import transformers.modeling_utils as mu
            mu.Conv1D = Conv1D
            log("Applied Conv1D compatibility shim for CodeSage.")
        except ImportError:
            pass


def build_node_index(node, index=None):
    """Build a flat index: node_id -> node dict."""
    if index is None:
        index = {}
    nid = node.get("id", "")
    if nid:
        index[nid] = node
    for child in node.get("children", []):
        build_node_index(child, index)
    return index


# ---------------------------------------------------------------------------
# Symbol matching → PROVIDES edges
# ---------------------------------------------------------------------------

def normalize_symbol(sym):
    """Normalize a symbol for matching."""
    s = sym.strip()
    # Remove trailing ()
    s = re.sub(r"\(\)$", "", s)
    # Remove leading struct/class/enum/typedef
    s = re.sub(r"^(struct|class|enum|typedef|const|static|extern|unsigned|signed)\s+", "", s)
    # Remove pointer/reference markers
    s = s.rstrip("*& ")
    return s.strip()


def compute_provides_edges(node_map, leaf_ids):
    """Compute PROVIDES edges by matching produces → consumes across nodes."""
    log("Computing PROVIDES edges from symbol matching...")
    match_start = time.time()

    # Build inverted index: normalized symbol → list of producer node IDs
    producers = {}  # symbol → [nodeId, ...]
    consumers = {}  # symbol → [nodeId, ...]

    for nid in leaf_ids:
        node = node_map[nid]
        for sym in node.get("produces", []):
            key = normalize_symbol(sym)
            if key:
                producers.setdefault(key, []).append(nid)
        for sym in node.get("consumes", []):
            key = normalize_symbol(sym)
            if key:
                consumers.setdefault(key, []).append(nid)

    # Match: for each consumed symbol, find all producers
    edges = []
    matched_symbols = set()
    for symbol, consumer_ids in consumers.items():
        producer_ids = producers.get(symbol, [])
        for prod_id in producer_ids:
            for cons_id in consumer_ids:
                if prod_id != cons_id:
                    edges.append({
                        "type": "PROVIDES",
                        "srcId": prod_id,
                        "dstId": cons_id,
                        "symbol": symbol,
                    })
                    matched_symbols.add(symbol)

    match_elapsed = time.time() - match_start

    # Stats
    unique_produced = set()
    unique_consumed = set()
    for nid in leaf_ids:
        node = node_map[nid]
        for s in node.get("produces", []):
            unique_produced.add(normalize_symbol(s))
        for s in node.get("consumes", []):
            unique_consumed.add(normalize_symbol(s))

    unmatched_consumed = unique_consumed - set(producers.keys())
    unmatched_produced = unique_produced - set(consumers.keys())

    log(f"PROVIDES edges: {len(edges)} edges from {len(matched_symbols)} matched symbols in {match_elapsed:.1f}s")
    log(f"Symbol stats: {len(unique_produced)} unique produced, {len(unique_consumed)} unique consumed, "
        f"{len(matched_symbols)} matched, {len(unmatched_consumed)} consumed-unmatched, "
        f"{len(unmatched_produced)} produced-unmatched")

    # Per-node edge count stats for PROVIDES
    if edges:
        edge_counts = Counter()
        for e in edges:
            edge_counts[e["srcId"]] += 1
            edge_counts[e["dstId"]] += 1
        for nid in leaf_ids:
            if nid not in edge_counts:
                edge_counts[nid] = 0
        counts = sorted(edge_counts.values())
        n = len(counts)
        avg = sum(counts) / n
        median = counts[n // 2] if n % 2 else (counts[n // 2 - 1] + counts[n // 2]) / 2
        stddev = (sum((c - avg) ** 2 for c in counts) / n) ** 0.5
        log(f"PROVIDES edge stats per node: min={counts[0]}, max={counts[-1]}, "
            f"median={median:.1f}, avg={avg:.1f}, stddev={stddev:.1f}")

    return edges


# ---------------------------------------------------------------------------
# Embedding similarity → EMBEDDING edges
# ---------------------------------------------------------------------------

def compute_embedding_edges(node_map, leaf_ids, embed_texts, top_percent):
    """Compute EMBEDDING edges via CodeSage cosine similarity."""
    fix_codesage_compat()

    log(f"Loading embedding model: {EMBEDDING_MODEL}")
    from sentence_transformers import SentenceTransformer
    import torch
    import numpy as np

    device = "cuda" if torch.cuda.is_available() else "cpu"
    embed_model = SentenceTransformer(EMBEDDING_MODEL, device=device, trust_remote_code=True)

    total = len(embed_texts)
    batch_size = 64
    emit("progress", phase="embedding", current=0, total=total)
    log(f"Generating embeddings for {total} leaf nodes on {device}...")
    embed_start = time.time()

    all_embeddings = []
    for i in range(0, total, batch_size):
        batch = embed_texts[i:i + batch_size]
        batch_emb = embed_model.encode(batch, batch_size=batch_size, show_progress_bar=False, convert_to_numpy=True)
        all_embeddings.append(batch_emb)
        done = min(i + batch_size, total)
        emit("progress", phase="embedding", current=done, total=total)
    embeddings = np.vstack(all_embeddings)

    embed_elapsed = time.time() - embed_start
    log(f"Embeddings generated in {embed_elapsed:.1f}s (dim={embeddings.shape[1]})")

    del embed_model
    if torch.cuda.is_available():
        torch.cuda.empty_cache()

    # Cosine similarity matrix
    n = len(leaf_ids)
    emit("progress", phase="similarity", current=0, total=3)
    log(f"Computing cosine similarity matrix ({n}x{n}) on {device}...")
    sim_start = time.time()

    emb_tensor = torch.tensor(embeddings, dtype=torch.float32, device=device)
    emb_norm = torch.nn.functional.normalize(emb_tensor, p=2, dim=1)
    sim_matrix = torch.mm(emb_norm, emb_norm.t())
    sim_matrix.fill_diagonal_(0.0)
    emit("progress", phase="similarity", current=1, total=3)

    triu_indices = torch.triu_indices(n, n, offset=1, device=device)
    triu_values = sim_matrix[triu_indices[0], triu_indices[1]]

    num_pairs = triu_values.shape[0]
    num_edges = max(1, int(num_pairs * top_percent / 100.0))
    threshold_val = torch.topk(triu_values, num_edges).values[-1].item()

    mask = triu_values >= threshold_val
    selected_i = triu_indices[0][mask].cpu().numpy()
    selected_j = triu_indices[1][mask].cpu().numpy()
    selected_sim = triu_values[mask].cpu().numpy()

    sim_elapsed = time.time() - sim_start
    emit("progress", phase="similarity", current=2, total=3)
    log(f"Cosine similarity computed in {sim_elapsed:.1f}s. Threshold (top {top_percent}%): "
        f"{threshold_val:.4f}, {len(selected_i)} edges from {num_pairs} pairs.")

    edges = []
    for idx in range(len(selected_i)):
        edges.append({
            "type": "EMBEDDING",
            "srcId": leaf_ids[int(selected_i[idx])],
            "dstId": leaf_ids[int(selected_j[idx])],
            "similarity": float(selected_sim[idx]),
        })

    # Per-node stats
    if edges:
        edge_counts = Counter()
        for e in edges:
            edge_counts[e["srcId"]] += 1
            edge_counts[e["dstId"]] += 1
        for nid in leaf_ids:
            if nid not in edge_counts:
                edge_counts[nid] = 0
        counts = sorted(edge_counts.values())
        n_c = len(counts)
        avg = sum(counts) / n_c
        median = counts[n_c // 2] if n_c % 2 else (counts[n_c // 2 - 1] + counts[n_c // 2]) / 2
        stddev = (sum((c - avg) ** 2 for c in counts) / n_c) ** 0.5
        log(f"EMBEDDING edge stats per node: min={counts[0]}, max={counts[-1]}, "
            f"median={median:.1f}, avg={avg:.1f}, stddev={stddev:.1f}")

    emit("progress", phase="similarity", current=3, total=3)
    return edges


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    if len(sys.argv) < 2:
        emit("error", message="Usage: compute_similarity.py <chunk_tree.json> [--top-percent N]")
        sys.exit(1)

    tree_path = os.path.abspath(sys.argv[1])
    if not os.path.exists(tree_path):
        emit("error", message=f"File not found: {tree_path}")
        sys.exit(1)

    top_percent = DEFAULT_TOP_PERCENT
    for i, arg in enumerate(sys.argv):
        if arg == "--top-percent" and i + 1 < len(sys.argv):
            try:
                top_percent = float(sys.argv[i + 1])
            except ValueError:
                pass

    log(f"Loading chunk tree from {tree_path}...")
    with open(tree_path, "r") as f:
        tree = json.load(f)

    node_map = build_node_index(tree)

    # Collect leaf nodes with summaries and symbols
    leaf_ids = []
    embed_texts = []
    has_symbols = False
    for nid, node in node_map.items():
        children = node.get("children", [])
        summary = node.get("summary", "")
        if not children and summary:
            leaf_ids.append(nid)
            embed_texts.append(summary)
            if node.get("consumes") or node.get("produces"):
                has_symbols = True

    if len(leaf_ids) < 2:
        emit("error", message="Not enough leaf nodes with summaries.")
        sys.exit(1)

    log(f"Found {len(leaf_ids)} leaf nodes with summaries.")

    all_edges = []

    # Phase 1: Symbol matching → PROVIDES edges
    if has_symbols:
        provides_edges = compute_provides_edges(node_map, leaf_ids)
        all_edges.extend(provides_edges)
    else:
        log("No symbol data found — skipping PROVIDES edge computation.")

    # Phase 2: Embedding similarity → EMBEDDING edges
    embedding_edges = compute_embedding_edges(node_map, leaf_ids, embed_texts, top_percent)
    all_edges.extend(embedding_edges)

    # Write combined edge file
    output_path = os.path.join(os.path.dirname(tree_path), "similarity_symbol_edges.csv")
    with open(output_path, "w") as f:
        f.write("edgeType,srcId,srcName,srcFile,srcLine,dstId,dstName,dstFile,dstLine,similarity,symbol\n")
        for e in all_edges:
            src_node = node_map.get(e["srcId"], {})
            dst_node = node_map.get(e["dstId"], {})
            sim_val = e.get("similarity", "")
            symbol = e.get("symbol", "")
            f.write(f"{e['type']},{e['srcId']},{src_node.get('name', '')},{src_node.get('file', '')},"
                    f"{src_node.get('lineStart', -1)},{e['dstId']},{dst_node.get('name', '')},{dst_node.get('file', '')},"
                    f"{dst_node.get('lineStart', -1)},{sim_val},{symbol}\n")

    log(f"Combined edges saved to {output_path}: "
        f"{sum(1 for e in all_edges if e['type'] == 'PROVIDES')} PROVIDES + "
        f"{sum(1 for e in all_edges if e['type'] == 'EMBEDDING')} EMBEDDING")
    emit("done")


if __name__ == "__main__":
    main()
