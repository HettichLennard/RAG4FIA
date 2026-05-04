#!/usr/bin/env python3
"""
Leaf Node Summarizer using vLLM

Generates summaries for leaf nodes (code chunks) in a chunk tree.
Each leaf receives its full hierarchical context from root to leaf,
enabling the model to understand where the code sits in the project.
All leaves are independent and processed in parallel.

After summarization, generates embeddings (CodeSage) and computes
cosine similarity edges between leaves.

Communication protocol (JSON lines on stdout):
  {"type": "log", "message": "..."}
  {"type": "done"}
  {"type": "error", "message": "..."}
"""

import sys
import os
import json
import time

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

MODEL_NAME = "/media/lennard/Volume/Models/Qwen3.5-9B"

VLLM_SETTINGS = {
    "tensor_parallel_size": 1,
    "gpu_memory_utilization": 0.92,
    "max_model_len": 16384,
    "max_num_seqs": 128,
    "dtype": "auto",
}

SAMPLING_PARAMS = {
    "temperature": 0.1,
    "max_tokens": 2048,
    "top_p": 0.95,
}

MAX_BATCH_SIZE = 256



# ---------------------------------------------------------------------------
# Protocol
# ---------------------------------------------------------------------------

def emit(msg_type, **kwargs):
    print(json.dumps({"type": msg_type, **kwargs}), flush=True)


def log(message):
    emit("log", message=message)


# ---------------------------------------------------------------------------
# Prompt construction
# ---------------------------------------------------------------------------

SYSTEM_PROMPT = (
    "You are a code analyst. You summarize code sections in the context of "
    "their surrounding project structure. Your summaries are used to generate "
    "embeddings for dependency analysis. You MUST name every specific symbol "
    "by its exact identifier — never say 'several functions' or 'various types', "
    "always say the actual names like `foo()`, `struct bar`, `g_count`. "
    "Always describe where the code lives: which function, class, file, and module "
    "it belongs to. Write approximately 200 words."
)

EXTRACT_SYSTEM_PROMPT = (
    "You are a precise symbol extractor. You extract exact symbol identifiers "
    "from code and summaries. Output only valid JSON matching the requested schema."
)

EXTRACT_USER_TEMPLATE = """You previously summarized this code section. Now extract the exact symbol names that cross the boundary of this code section.

**Code:**
```
{code}
```

**Your summary:**
{summary}

Extract every symbol that crosses this code section's boundary — ONLY symbols that flow in or out, NOT symbols defined and used purely internally.

- "consumes": symbols this code depends on that are NOT defined within it — functions it calls, types it uses, global variables it reads, macros it references, constants from headers, hardware registers it accesses, etc.
- "produces": symbols this code defines or exposes that other code could depend on — functions it defines, types it declares, global variables it writes, macros it defines, etc.

IMPORTANT: If a symbol is both read AND modified (e.g. `counter += 1`, `buffer[i] = x`), include it in BOTH consumes and produces.

Use exact identifiers as they appear in the code. Include every symbol — do not omit any.

Output valid JSON with two arrays: "consumes" and "produces"."""

EXTRACT_JSON_SCHEMA = {
    "type": "object",
    "properties": {
        "consumes": {
            "type": "array",
            "items": {"type": "string"}
        },
        "produces": {
            "type": "array",
            "items": {"type": "string"}
        }
    },
    "required": ["consumes", "produces"]
}

LEAF_USER_TEMPLATE = """You are given a JSON object describing a code leaf node and its full hierarchical context from project root down to the leaf.

**JSON structure explained:**
- The outermost object is the project root. Each level has "id", "type", "name", and "content".
- "content" contains the structural code at that level. Other children at the same level appear as <placeholder> tokens (e.g. <function_definition_00008>), showing you what sibling code sections exist alongside the current path.
- The placeholder for the child leading to the current leaf has been replaced with ">>> CHILD <<<". Follow the "child" field to see its contents.
- The innermost node has "code" instead of "content" — this is the actual source code to summarize.

**Your task:**
Write a ~200-word summary of the leaf node's code. You MUST name every symbol by its exact identifier — never use vague language like "several helpers", "various types", or "standard library functions". Always write the actual names.

IMPORTANT: Start your summary by describing WHERE this code lives — which function(s), class, file, and directory it belongs to, using the hierarchy provided.

Then emphasize the external symbols that CROSS the code section's boundary — only symbols flowing IN from outside or flowing OUT to outside, NOT internal-only symbols:

- **Symbols flowing IN**: functions called, types used, global variables read, resources accessed (files, sockets, hardware registers, database tables), callbacks invoked — anything this code depends on that is NOT defined within it.
- **Symbols flowing OUT**: functions defined, types declared, global variables written, resources produced, side effects — anything this code creates that other code could depend on.

Weave these naturally into a coherent prose summary. Do not use bullet points or separate headings.

**Examples of good summaries:**

Example 1 (embedded system code):
"This section defines `adc_read_channel(uint8_t ch)` which reads a raw 12-bit sample from the ADC peripheral by writing to the `ADC_CR` control register and polling `ADC_SR` for the conversion-complete flag. It depends on the hardware register base address `ADC_BASE` defined in `hal/adc.h` and the `gpio_set_analog_mode()` function from the GPIO driver. It returns the raw ADC value as `uint16_t`, which callers like `sensor_get_temperature()` in `sensor.c` consume to convert to physical units."

Example 2 (application code):
"This merged section includes `<pthread.h>`, `<sqlite3.h>`, and the project header `db_pool.h`. It defines `db_worker_thread()`, the main loop for the database writer thread, which dequeues `write_request_t` items from the shared `g_write_queue` (protected by `g_queue_mutex` and signaled via `g_queue_cond`). Each request is executed via `sqlite3_exec()` on a connection obtained from `db_pool_acquire()`. On failure it logs via `syslog()` and increments the global `g_error_count`. The function is registered as a thread entry point via `pthread_create()` in `main.c`."

Hierarchical context:
```json
{hierarchy_json}
```"""


def build_hierarchy_json(leaf_node, node_map):
    """Build nested JSON from root down to the leaf node.

    At each parent level, the placeholder for the child-on-path-to-leaf
    is replaced with '>>> CHILD <<<'. Other sibling placeholders remain,
    giving the model structural context about what else exists at that level.
    """
    import re

    # Walk up to collect the parent chain
    chain = []
    current_id = leaf_node.get("id", "")
    while current_id:
        node = node_map.get(current_id)
        if not node:
            break
        chain.append(node)
        current_id = node.get("_parentId", "")

    # chain is [leaf, parent, ..., root] — reverse to root-first
    chain.reverse()

    # Build nested dict from root to leaf
    result = None
    innermost = None
    for i, node in enumerate(chain):
        is_leaf = (i == len(chain) - 1)
        entry = {
            "id": node.get("id", ""),
            "type": node.get("type", ""),
            "name": node.get("name", ""),
        }
        if is_leaf:
            entry["code"] = node.get("content", "")
        else:
            content = node.get("content", "")
            if content and i + 1 < len(chain):
                # Replace the placeholder for the next node in the chain
                # with >>> CHILD <<< so the model knows where it leads
                next_id = chain[i + 1].get("id", "")
                if next_id:
                    # Match any placeholder ending with the child's ID
                    content = re.sub(
                        r"<([^<>]*" + re.escape(next_id) + r")>",
                        ">>> CHILD <<<",
                        content
                    )
            if content:
                entry["content"] = content

        if result is None:
            result = entry
            innermost = entry
        else:
            innermost["child"] = entry
            innermost = entry

    return result or {}


def build_leaf_messages(leaf_node, node_map):
    """Build chat messages for a leaf node with hierarchical context."""
    hierarchy = build_hierarchy_json(leaf_node, node_map)
    hierarchy_str = json.dumps(hierarchy, indent=2, ensure_ascii=False)

    user = LEAF_USER_TEMPLATE.format(hierarchy_json=hierarchy_str)

    return [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content": user},
    ]


# ---------------------------------------------------------------------------
# Output processing
# ---------------------------------------------------------------------------

def strip_thinking(text):
    """Extract the final answer from model output, stripping thinking traces.

    Handles multiple formats:
      - <think>...</think> answer        (standard thinking tags)
      - analysis...assistantfinal answer  (gpt-oss style)
    Falls back to returning the full text if no pattern matches.
    """
    import re

    # gpt-oss style: extract everything after the last "assistantfinal"
    if "assistantfinal" in text:
        return text.rsplit("assistantfinal", 1)[-1].strip()

    # Standard <think>...</think> tags
    cleaned = re.sub(r"<think>.*?</think>\s*", "", text, flags=re.DOTALL).strip()
    if cleaned:
        return cleaned

    return text.strip()


def truncate_summary(text, max_words=300):
    """Truncate to at most max_words words."""
    words = text.strip().split()
    if len(words) > max_words:
        truncated = ' '.join(words[:max_words])
        last_period = truncated.rfind('.')
        if last_period > len(truncated) // 2:
            return truncated[:last_period + 1]
        return truncated
    return text.strip()


# ---------------------------------------------------------------------------
# Tree traversal
# ---------------------------------------------------------------------------

def build_node_index(node, index=None, parent_id=None):
    """Build a flat index: node_id -> node dict, with parent references."""
    if index is None:
        index = {}
    nid = node.get("id", "")
    if nid:
        index[nid] = node
        node["_parentId"] = parent_id
    for child in node.get("children", []):
        build_node_index(child, index, nid)
    return index


def collect_leaf_nodes(node_map):
    """Return list of (node_id, node) for all leaf nodes (no children, with content)."""
    leaves = []
    for nid, node in node_map.items():
        children = node.get("children", [])
        content = node.get("content", "")
        if not children and content:
            leaves.append((nid, node))
    return leaves


# ---------------------------------------------------------------------------
# Similarity computation
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Main summarization engine
# ---------------------------------------------------------------------------

def run_summarization(tree_path):
    """Run leaf-only summarization on the chunk tree."""
    log("Loading chunk tree...")
    with open(tree_path, "r") as f:
        tree = json.load(f)

    node_map = build_node_index(tree)
    leaves = collect_leaf_nodes(node_map)
    total_nodes = len(node_map)
    log(f"Chunk tree: {total_nodes} nodes, {len(leaves)} leaf nodes to summarize.")

    # --- Phase 1: Summarization ---
    log(f"Initializing vLLM with model: {MODEL_NAME}")

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
        temperature=SAMPLING_PARAMS["temperature"],
        max_tokens=SAMPLING_PARAMS["max_tokens"],
        top_p=SAMPLING_PARAMS["top_p"],
    )
    log("vLLM engine ready.")

    tokenizer = llm.get_tokenizer()
    CONTEXT_WARN_THRESHOLD = 16384

    def estimate_tokens(messages):
        text = "".join(m["content"] for m in messages)
        return len(tokenizer.encode(text))

    # Build all conversations (all leaves, no dependencies)
    conversations = []
    conv_leaf_ids = []

    for nid, node in leaves:
        messages = build_leaf_messages(node, node_map)
        tok_count = estimate_tokens(messages)
        if tok_count > CONTEXT_WARN_THRESHOLD:
            name = node.get("name", nid)
            emit("error", message=f"Prompt for leaf '{name}' ({nid}) exceeds {CONTEXT_WARN_THRESHOLD} tokens: {tok_count} tokens")
        conversations.append(messages)
        conv_leaf_ids.append(nid)

    # Debug log
    debug_path = os.path.join(os.path.dirname(tree_path), "prompt_debug.txt")
    debug_file = open(debug_path, "w", encoding="utf-8")
    log(f"Writing prompt debug log to {debug_path}")

    # Process all leaves in batches (all independent — max parallelism)
    summarized = 0
    total_leaves = len(conversations)
    start_time = time.time()
    emit("progress", phase="summaries", current=0, total=total_leaves)
    log(f"Generating {total_leaves} leaf summaries...")

    for batch_start in range(0, len(conversations), MAX_BATCH_SIZE):
        batch_end = min(batch_start + MAX_BATCH_SIZE, len(conversations))
        batch_convs = conversations[batch_start:batch_end]
        batch_ids = conv_leaf_ids[batch_start:batch_end]

        batch_num = batch_start // MAX_BATCH_SIZE + 1
        total_batches = (len(conversations) + MAX_BATCH_SIZE - 1) // MAX_BATCH_SIZE
        log(f"Batch {batch_num}/{total_batches}: {len(batch_convs)} prompts...")
        batch_start_time = time.time()

        outputs = llm.chat(batch_convs, sampling_params,
                            chat_template_kwargs={"enable_thinking": False})

        for output, conv, nid in zip(outputs, batch_convs, batch_ids):
            raw_text = output.outputs[0].text
            summary = strip_thinking(raw_text)
            summary = truncate_summary(summary)
            node_map[nid]["summary"] = summary

            # Debug entry
            node = node_map[nid]
            debug_file.write(f"{'=' * 80}\n")
            debug_file.write(f"NODE: {nid}  TYPE: {node.get('type', '')}  NAME: {node.get('name', '')}\n")
            debug_file.write(f"{'=' * 80}\n")
            debug_file.write(f"\n--- SYSTEM ---\n{conv[0]['content']}\n")
            debug_file.write(f"\n--- USER ---\n{conv[1]['content']}\n")
            debug_file.write(f"\n--- RAW RESPONSE ---\n{raw_text}\n")
            debug_file.write(f"\n--- FINAL SUMMARY ---\n{summary}\n\n")
            debug_file.flush()

        summarized += len(batch_ids)
        emit("progress", phase="summaries", current=summarized, total=total_leaves)
        batch_elapsed = time.time() - batch_start_time
        log(f"Batch {batch_num}/{total_batches}: done in {batch_elapsed:.1f}s")

    total_elapsed = time.time() - start_time
    log(f"Summarization complete: {summarized} leaf nodes in {total_elapsed:.1f}s")

    debug_file.close()
    log(f"Prompt debug log written to {debug_path}")

    # --- Pass 2: Symbol extraction with constrained JSON decoding ---
    log("Starting Pass 2: Symbol extraction with constrained decoding...")
    from vllm.sampling_params import StructuredOutputsParams

    extract_params = SamplingParams(
        temperature=0.1,
        max_tokens=2048,
        top_p=0.95,
        structured_outputs=StructuredOutputsParams(json=EXTRACT_JSON_SCHEMA),
    )

    extract_conversations = []
    extract_ids = []

    for nid, node in leaves:
        summary = node.get("summary", "")
        content = node.get("content", "")
        if not summary or not content:
            continue
        messages = [
            {"role": "system", "content": EXTRACT_SYSTEM_PROMPT},
            {"role": "user", "content": EXTRACT_USER_TEMPLATE.format(code=content, summary=summary)},
        ]
        extract_conversations.append(messages)
        extract_ids.append(nid)

    extracted = 0
    total_extract = len(extract_conversations)
    extract_start = time.time()
    emit("progress", phase="symbols", current=0, total=total_extract)
    log(f"Extracting symbols for {total_extract} leaf nodes...")

    for batch_start in range(0, len(extract_conversations), MAX_BATCH_SIZE):
        batch_end = min(batch_start + MAX_BATCH_SIZE, len(extract_conversations))
        batch_convs = extract_conversations[batch_start:batch_end]
        batch_ids = extract_ids[batch_start:batch_end]

        batch_num = batch_start // MAX_BATCH_SIZE + 1
        total_batches = (len(extract_conversations) + MAX_BATCH_SIZE - 1) // MAX_BATCH_SIZE
        log(f"Extract batch {batch_num}/{total_batches}: {len(batch_convs)} prompts...")

        outputs = llm.chat(batch_convs, extract_params,
                            chat_template_kwargs={"enable_thinking": False})

        for output, nid in zip(outputs, batch_ids):
            raw_text = output.outputs[0].text.strip()
            try:
                symbols = json.loads(raw_text)
                node_map[nid]["consumes"] = symbols.get("consumes", [])
                node_map[nid]["produces"] = symbols.get("produces", [])
            except json.JSONDecodeError:
                log(f"Warning: failed to parse symbols for node {nid}")
                node_map[nid]["consumes"] = []
                node_map[nid]["produces"] = []

        extracted += len(batch_ids)
        emit("progress", phase="symbols", current=extracted, total=total_extract)

    extract_elapsed = time.time() - extract_start
    log(f"Symbol extraction complete: {extracted} nodes in {extract_elapsed:.1f}s")

    # Log symbol stats
    total_consumes = sum(len(node_map[nid].get("consumes", [])) for nid, _ in leaves)
    total_produces = sum(len(node_map[nid].get("produces", [])) for nid, _ in leaves)
    log(f"Total symbols: {total_consumes} consumed, {total_produces} produced across {extracted} leaves")

    # Clean up internal fields before saving
    for nid, node in node_map.items():
        node.pop("_parentId", None)

    # Save summaries and symbols to JSON
    log("Saving updated chunk tree...")
    with open(tree_path, "w") as f:
        json.dump(tree, f, indent=2)
    log("Chunk tree saved with summaries and symbols.")


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main():
    if len(sys.argv) < 2:
        emit("error", message="Usage: summarize.py <path_to_chunk_tree.json>")
        sys.exit(1)

    tree_path = os.path.abspath(sys.argv[1])
    if not os.path.exists(tree_path):
        emit("error", message=f"File not found: {tree_path}")
        sys.exit(1)

    try:
        run_summarization(tree_path)
        emit("done")
    except Exception as e:
        emit("error", message=f"Summarization failed: {e}")
        import traceback
        log(traceback.format_exc())
        sys.exit(1)


if __name__ == "__main__":
    main()
