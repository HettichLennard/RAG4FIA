#!/usr/bin/env python3
"""
Tree-sitter CST Analysis Script

Parses all source files in a project directory using tree-sitter and outputs
a hierarchical concrete syntax tree as JSON.

Communication protocol (JSON lines on stdout):
  {"type": "log", "message": "..."}     - progress log
  {"type": "result", "data": {...}, "sourceFiles": {...}}  - final CST tree
  {"type": "error", "message": "..."}   - error
"""

import importlib
import sys
import os
import json
import time
import uuid

# Map file extension -> (tree_sitter_module_name, language_function_name)
EXTENSION_TO_TS_LANG = {
    ".c": ("tree_sitter_c", "language"),
    ".h": ("tree_sitter_c", "language"),
    ".cpp": ("tree_sitter_cpp", "language"),
    ".cxx": ("tree_sitter_cpp", "language"),
    ".cc": ("tree_sitter_cpp", "language"),
    ".hpp": ("tree_sitter_cpp", "language"),
    ".hxx": ("tree_sitter_cpp", "language"),
    ".java": ("tree_sitter_java", "language"),
    ".py": ("tree_sitter_python", "language"),
    ".js": ("tree_sitter_javascript", "language"),
    ".jsx": ("tree_sitter_javascript", "language"),
    ".ts": ("tree_sitter_typescript", "language_typescript"),
    ".tsx": ("tree_sitter_typescript", "language_tsx"),
    ".go": ("tree_sitter_go", "language"),
    ".rb": ("tree_sitter_ruby", "language"),
    ".rs": ("tree_sitter_rust", "language"),
    ".cs": ("tree_sitter_c_sharp", "language"),
    # PHP
    ".php": ("tree_sitter_php", "language_php"),
    # Kotlin
    ".kt": ("tree_sitter_kotlin", "language"),
    ".kts": ("tree_sitter_kotlin", "language"),
    # Swift
    ".swift": ("tree_sitter_swift", "language"),
    # Scala
    ".scala": ("tree_sitter_scala", "language"),
    ".sc": ("tree_sitter_scala", "language"),
    # Lua
    ".lua": ("tree_sitter_lua", "language"),
}

EXTENSION_TO_LANGUAGE_NAME = {
    ".c": "C",
    ".h": "C/C++ Header",
    ".cpp": "C++",
    ".cxx": "C++",
    ".cc": "C++",
    ".hpp": "C++ Header",
    ".hxx": "C++ Header",
    ".java": "Java",
    ".py": "Python",
    ".js": "JavaScript",
    ".jsx": "JavaScript (JSX)",
    ".ts": "TypeScript",
    ".tsx": "TypeScript (TSX)",
    ".go": "Go",
    ".rb": "Ruby",
    ".rs": "Rust",
    ".cs": "C#",
    ".php": "PHP",
    ".kt": "Kotlin",
    ".kts": "Kotlin (Script)",
    ".swift": "Swift",
    ".scala": "Scala",
    ".sc": "Scala (Script)",
    ".lua": "Lua",
}

SKIP_DIRS = {".git", "node_modules", "__pycache__", "build", "dist", "target",
             ".vscode", ".idea", ".joern", "out", "bin", "obj"}


def emit(msg_type, **kwargs):
    print(json.dumps({"type": msg_type, **kwargs}), flush=True)


def log(message):
    emit("log", message=message)



# Tree-sitter node types that introduce a new scope (have a body/block child).
# Comprehensive across C, C++, Java, Python, JS, TS, Go, Ruby, Rust, C#.
SCOPING_TYPES = {
    # Presence Condition nodes (must not be merged during pruning)
    "PC", "if_pc", "elif_pc", "else_pc",
    # C
    "function_definition", "struct_specifier", "union_specifier", "enum_specifier",
    "if_statement", "else_clause", "for_statement", "while_statement", "do_statement",
    "switch_statement", "case_statement", "labeled_statement",
    "linkage_specification",
    "preproc_if", "preproc_ifdef", "preproc_else", "preproc_elif", "preproc_elifdef",
    # C++ (adds to C)
    "class_specifier", "namespace_definition",
    "template_declaration", "concept_definition", "requires_expression",
    "for_range_loop", "try_statement", "catch_clause",
    "lambda_expression", "export_declaration",
    # Java
    "class_declaration", "interface_declaration", "enum_declaration",
    "annotation_type_declaration", "record_declaration",
    "method_declaration", "constructor_declaration", "compact_constructor_declaration",
    "static_initializer",
    "enhanced_for_statement",
    "switch_expression", "switch_block_statement_group", "switch_rule",
    "try_with_resources_statement",
    "synchronized_statement",
    # Python
    "class_definition", "function_definition", "decorated_definition",
    "elif_clause",
    "except_clause", "finally_clause",
    "with_statement", "match_statement", "case_clause",
    "list_comprehension", "set_comprehension", "dictionary_comprehension",
    "generator_expression",
    # JavaScript
    "function_declaration", "function_expression",
    "generator_function_declaration", "generator_function",
    "arrow_function", "method_definition",
    "class_declaration", "class", "class_static_block",
    "for_in_statement",
    "switch_case", "switch_default",
    "with_statement",
    # TypeScript (adds to JS)
    "interface_declaration", "type_alias_declaration",
    "abstract_class_declaration",
    "internal_module", "module", "ambient_declaration",
    # Go
    "func_literal",
    "expression_switch_statement", "type_switch_statement",
    "expression_case", "type_case", "default_case",
    "select_statement", "communication_case",
    "go_statement", "defer_statement",
    "struct_type", "interface_type",
    # Ruby
    "method", "singleton_method",
    "module", "singleton_class",
    "if", "unless", "elsif", "else", "then",
    "while", "until", "for",
    "case", "when", "case_match", "in_clause",
    "begin", "rescue", "ensure",
    "do_block", "block", "lambda",
    "begin_block", "end_block",
    # Rust
    "function_item", "function_signature_item", "closure_expression",
    "struct_item", "enum_item", "enum_variant", "union_item", "type_item",
    "trait_item", "impl_item",
    "mod_item", "foreign_mod_item",
    "if_expression",
    "for_expression", "while_expression", "loop_expression",
    "match_expression", "match_arm",
    "unsafe_block", "async_block", "const_block", "try_block", "gen_block",
    "macro_definition",
    # C#
    "class_declaration", "struct_declaration", "interface_declaration",
    "enum_declaration", "record_declaration", "delegate_declaration",
    "namespace_declaration", "file_scoped_namespace_declaration",
    "constructor_declaration", "destructor_declaration",
    "operator_declaration", "conversion_operator_declaration",
    "local_function_statement",
    "property_declaration", "accessor_declaration", "indexer_declaration",
    "event_declaration",
    "foreach_statement",
    "switch_section", "switch_expression", "switch_expression_arm",
    "catch_filter_clause",
    "using_statement", "lock_statement",
    "checked_statement", "unsafe_statement", "fixed_statement",
    "anonymous_method_expression",
    "preproc_if", "preproc_else",
    # PHP
    "method_declaration", "anonymous_function", "arrow_function",
    "trait_declaration",
    "else_if_clause",
    "foreach_statement",
    "case_statement", "default_statement",
    "match_expression",
    "catch_clause", "finally_clause",
    "namespace_definition", "declare_statement",
    # Kotlin
    "function_declaration", "anonymous_function",
    "object_declaration", "companion_object",
    "secondary_constructor",
    "if_expression", "when_expression", "when_entry",
    "try_expression", "catch_block", "finally_block",
    "lambda_literal", "annotated_lambda",
    # Swift
    "init_declaration", "deinit_declaration", "subscript_declaration",
    "protocol_declaration",
    "guard_statement",
    "repeat_while_statement",
    "switch_entry",
    "computed_property", "computed_getter", "computed_setter",
    # Scala
    "function_definition", "class_definition",
    "object_definition", "trait_definition",
    "enum_definition", "extension_definition", "given_definition",
    "package_clause", "package_object",
    "if_expression", "for_expression", "while_expression",
    "do_while_expression", "match_expression",
    "case_clause", "lambda_expression",
    "try_expression", "catch_clause", "finally_clause",
    # Lua
    "function_declaration", "function_definition",
    "elseif_statement", "else_statement",
    "repeat_statement", "do_statement",
}

# Node types that represent the body/block inside a scoping node
BODY_TYPES = {
    # C / C++
    "compound_statement", "field_declaration_list", "enumerator_list",
    "declaration_list", "initializer_list",
    # Java
    "class_body", "interface_body", "enum_body", "annotation_type_body",
    "constructor_body", "switch_block",
    # Python
    "block",
    # JS / TS
    "statement_block",
    # Go
    "literal_value", "method_spec_list",
    # Ruby
    "body_statement", "parenthesized_statements",
    # Rust
    "match_block", "enum_variant_list", "use_list",
    # C#
    "enum_member_declaration_list", "accessor_list", "switch_body",
    # PHP
    "colon_block", "switch_block", "match_block",
    # Kotlin
    "class_body", "enum_class_body", "control_structure_body",
    "function_body", "statements",
    # Swift
    "class_body", "enum_class_body", "protocol_body", "function_body",
    # Scala
    "template_body", "with_template_body", "enum_body",
    "indented_block", "indented_cases", "case_block",
    # Lua
    "block",
    # Shared
    "body",
}

# Tokens that mark the opening of a scope body
SCOPE_OPEN_TOKENS = {"{", ":", "do", "then", "begin", "=>", "function"}

# Tokens that mark the closing of a scope body
SCOPE_CLOSE_TOKENS = {"}", "end", "endif", "endwhile", "endfor",
                      "endforeach", "endswitch", "enddeclare", "until"}

# Non-body children that are structurally part of the scope definition
SCOPE_PART_TOKENS = {
    # Inheritance / implementation
    "base_class_clause", "superclass", "super_interfaces", "superinterfaces",
    # Generics / templates
    "type_parameters", "type_parameter_list", "template_parameter_list",
    # Decorators / attributes / annotations
    "decorator", "attribute", "attribute_declaration",
    "annotation", "marker_annotation",
    # Access
    "access_specifier", "access_modifier",
    # Constraints
    "type_constraint", "requires_clause", "trait_bounds", "where_clause",
    # Throws
    "throws",
    # PHP
    "base_clause", "class_interface_clause",
    "formal_parameters", "attribute_list",
    "anonymous_function_use_clause",
    "visibility_modifier", "abstract_modifier", "final_modifier",
    "readonly_modifier", "static_modifier",
    # Kotlin
    "modifiers", "delegation_specifier",
    "primary_constructor", "function_value_parameters",
    "receiver_type", "constructor_delegation_call",
    # Swift
    "inheritance_specifier", "capture_list",
    "availability_condition",
    # Scala
    "extends_clause", "derives_clause",
    "class_parameters", "parameters",
    "self_type", "enumerators", "guard",
}


def get_language(ext):
    """Load the tree-sitter Language for a file extension. Returns None on failure."""
    from tree_sitter import Language

    lang_info = EXTENSION_TO_TS_LANG.get(ext.lower())
    if not lang_info:
        return None

    module_name, func_name = lang_info
    try:
        mod = importlib.import_module(module_name)
        lang_func = getattr(mod, func_name)
        return Language(lang_func())
    except (ImportError, AttributeError) as e:
        log(f"  Warning: Could not load tree-sitter grammar for {ext}: {e}")
        return None


def node_to_dict(ts_node, file_rel_path):
    """Convert a tree-sitter Node to a dict recursively."""
    node_type = ts_node.type

    # Build display name
    if ts_node.child_count == 0 and ts_node.text:
        text = ts_node.text.decode("utf-8", errors="replace")
        text_preview = text[:80] + "..." if len(text) > 80 else text
        text_preview = text_preview.replace("\n", " ").replace("\r", "")
        name = f"[{node_type}] {text_preview}" if text_preview else f"[{node_type}]"
    else:
        name = f"[{node_type}]"

    return {
        "name": name,
        "type": node_type,
        "file": file_rel_path,
        "lineStart": ts_node.start_point[0] + 1,  # 1-indexed
        "lineEnd": ts_node.end_point[0] + 1,       # 1-indexed, inclusive
        "colStart": ts_node.start_point[1],         # 0-indexed column
        "colEnd": ts_node.end_point[1],             # 0-indexed column
        "children": [node_to_dict(child, file_rel_path) for child in ts_node.children],
    }


def build_file_tree(cst_by_file, project_name):
    """Build hierarchical directory/file tree from per-file CST roots."""
    root = {"name": project_name, "type": "PROJECT", "children": []}
    dir_nodes = {}

    for filepath in sorted(cst_by_file.keys()):
        cst_roots = cst_by_file[filepath]
        parts = filepath.strip("/").split("/")

        current = root
        for i, part in enumerate(parts[:-1]):
            path_key = "/".join(parts[:i + 1])
            if path_key not in dir_nodes:
                dir_node = {"name": part, "type": "DIRECTORY", "children": []}
                current["children"].append(dir_node)
                dir_nodes[path_key] = dir_node
            current = dir_nodes[path_key]

        file_node = {"name": parts[-1], "type": "FILE", "children": cst_roots}
        current["children"].append(file_node)

    return root


def count_nodes(node):
    total = 1
    for child in node.get("children", []):
        total += count_nodes(child)
    return total


def node_range(node):
    """Line range of a node (number of lines)."""
    ls = node.get("lineStart", 0)
    le = node.get("lineEnd", 0)
    if ls > 0 and le > 0:
        return le - ls + 1
    return 0


def _get_scope_category(node):
    """Return the scope category of a node: 'open', 'close', 'pc', or 'regular'.

    PC nodes and nodes containing PCs get their own unique category
    so they are never merged with other nodes.
    """
    ntype = node.get("type", "")
    if ntype in ("PC", "if_pc", "elif_pc", "else_pc"):
        return f"pc_{node.get('pcId', id(node))}"
    if _contains_pc(node):
        return f"pc_container_{id(node)}"
    return node.get("_scopeCat", "regular")


def _build_sig_text(nodes):
    """Build a display text from a list of nodes for merged signatures."""
    parts = []
    for s in nodes:
        n = s.get("name", "")
        if n.startswith("[") and "] " in n:
            n = n.split("] ", 1)[1]
        elif n.startswith("[") and n.endswith("]"):
            n = s.get("type", "")
        if n:
            parts.append(n)
    text = " ".join(parts)
    return text[:120] + "..." if len(text) > 120 else text


def _make_scope_node(nodes, cat, file_ref):
    """Merge a list of nodes into a single scope_open or scope_close node."""
    if len(nodes) == 1:
        node = nodes[0]
        # Ensure the type is set correctly for scope detection in enrichment
        node["type"] = "scope_open" if cat == "open" else "scope_close"
        node["_scopeCat"] = cat
        return node
    sig_text = _build_sig_text(nodes)
    prefix = "scope-open" if cat == "open" else "scope-close"
    return {
        "name": f"[{prefix}] {sig_text}",
        "type": "scope_open" if cat == "open" else "scope_close",
        "file": file_ref,
        "lineStart": nodes[0].get("lineStart", 0),
        "lineEnd": nodes[-1].get("lineEnd", 0),
        "colStart": nodes[0].get("colStart", 0),
        "colEnd": nodes[-1].get("colEnd", 0),
        "_scopeCat": cat,
        "children": [],
    }


def _has_body_child(children):
    """Check if any child is a body/block type."""
    return any(c.get("type", "") in BODY_TYPES for c in children)


def _has_scope_token_in_body(children):
    """Check if any body child contains a scope-opening token ({ or :)."""
    for c in children:
        if c.get("type", "") in BODY_TYPES:
            for bc in c.get("children", []):
                if bc.get("type", "") in SCOPE_OPEN_TOKENS:
                    return True
    return False


def flatten_scoping_node(node, max_size):
    """For a scoping node, flatten its body inline and categorize children.

    Detection is TOKEN-DRIVEN: a node is only treated as scoping if it has
    a body child that contains an actual scope-opening token ({, :, do, etc.).
    This prevents false positives like Python's `module` which has no opener.

    Children become one of three categories:
    - 'open': scope-opening signature parts (keyword, condition, {, :, inherits, etc.)
    - 'close': scope-closing parts (}, end, etc.)
    - 'regular': body contents

    If the entire scoping node's range <= max_size, everything stays 'regular'.

    Processes bottom-up so nested scoping nodes are handled first.
    """
    # Process children first (bottom-up)
    for child in node.get("children", []):
        flatten_scoping_node(child, max_size)

    ntype = node.get("type", "")
    if ntype not in SCOPING_TYPES:
        return
    children = node.get("children", [])
    if not children:
        return

    # Only treat as scoping if there's an actual body with scope tokens
    if not _has_body_child(children) and not _has_scope_token_in_body(children):
        return

    # For Python: the body is `block` which has `:` as a sibling (not inside block).
    # Check if `:` appears as a direct child — that's also a scope opener.
    has_direct_opener = any(
        c.get("type", "") in SCOPE_OPEN_TOKENS for c in children
    )
    has_body_opener = _has_scope_token_in_body(children)

    if not has_direct_opener and not has_body_opener and not _has_body_child(children):
        return

    # If the entire scoping node fits in max_size, treat everything as regular
    # Exception: nodes containing PCs must preserve their signatures
    nr = node_range(node)
    is_small = nr <= max_size and not _contains_pc(node)

    file_ref = node.get("file", "")

    new_children = []
    open_group = []  # accumulates opening signature tokens
    found_body = False  # tracks whether we've seen the body yet

    def flush_open():
        """Merge accumulated open-signature tokens into one scope_open node."""
        nonlocal open_group
        if not open_group:
            return
        if is_small:
            for o in open_group:
                o["_scopeCat"] = "regular"
                new_children.append(o)
        else:
            merged = _make_scope_node(open_group, "open", file_ref)
            new_children.append(merged)
        open_group = []

    for child in children:
        ctype = child.get("type", "")

        if ctype in BODY_TYPES:
            found_body = True
            # Body wrapper — inline its children
            for body_child in child.get("children", []):
                btype = body_child.get("type", "")
                if btype in SCOPE_OPEN_TOKENS:
                    body_child["_scopeCat"] = "open" if not is_small else "regular"
                    open_group.append(body_child)
                elif btype in SCOPE_CLOSE_TOKENS:
                    flush_open()
                    body_child["_scopeCat"] = "close" if not is_small else "regular"
                    body_child["type"] = "scope_close" if not is_small else btype
                    new_children.append(body_child)
                else:
                    flush_open()
                    body_child["_scopeCat"] = "regular"
                    new_children.append(body_child)
        elif ctype in SCOPING_TYPES and child.get("children"):
            # Nested scoping with children (else_clause, catch, etc.)
            flush_open()
            child["_scopeCat"] = "regular"
            new_children.append(child)
        elif ctype in SCOPE_OPEN_TOKENS and not found_body:
            # Direct scope opener (Python's `:` is a sibling of `block`, not inside it)
            child["_scopeCat"] = "open" if not is_small else "regular"
            open_group.append(child)
        elif ctype in SCOPE_PART_TOKENS and not found_body:
            child["_scopeCat"] = "open" if not is_small else "regular"
            open_group.append(child)
        elif not found_body:
            # Pre-body child: part of the opening signature
            child["_scopeCat"] = "open" if not is_small else "regular"
            open_group.append(child)
        else:
            # Post-body content (shouldn't normally happen, but be safe)
            flush_open()
            child["_scopeCat"] = "regular"
            new_children.append(child)

    flush_open()
    node["children"] = new_children


# ---- Pruning functions ----

def _contains_pc(node):
    """Check if any descendant is a PC node."""
    for child in node.get("children", []):
        if child.get("type") in ("PC", "if_pc", "elif_pc", "else_pc"):
            return True
        if _contains_pc(child):
            return True
    return False


def prune_step1(node, max_size):
    """Vertical pruning: if a node's range <= max_size, make it a leaf.

    Works top-down. No scoping constraints — purely based on max_size.
    """
    ntype = node.get("type", "")
    if ntype in ("PROJECT", "DIRECTORY", "FILE"):
        for child in node.get("children", []):
            prune_step1(child, max_size)
        return

    # Never collapse PC structure — preserve alternatives
    if ntype in ("PC", "if_pc", "elif_pc", "else_pc"):
        for child in node.get("children", []):
            prune_step1(child, max_size)
        return

    nr = node_range(node)
    if nr <= max_size and node.get("children"):
        # Don't collapse if any descendant is a PC node
        if _contains_pc(node):
            for child in node.get("children", []):
                prune_step1(child, max_size)
            return
        node["children"] = []
        return

    for child in node.get("children", []):
        prune_step1(child, max_size)


def _merge_group(group):
    """Create a merged leaf node from a list of sibling nodes."""
    if len(group) == 1:
        return group[0]
    file_ref = group[0].get("file", "")
    gs = group[0].get("lineStart", 0)
    ge = group[-1].get("lineEnd", 0)

    # Determine merged type based on category
    cat = _get_scope_category(group[0])
    if cat == "open":
        return _make_scope_node(group, "open", file_ref)
    elif cat == "close":
        return _make_scope_node(group, "close", file_ref)
    else:
        return {
            "name": f"[merged: {len(group)} nodes] lines {gs}-{ge}",
            "type": "merged",
            "file": file_ref,
            "lineStart": gs,
            "lineEnd": ge,
            "colStart": group[0].get("colStart", 0),
            "colEnd": group[-1].get("colEnd", 0),
            "children": [],
        }


def _split_run_balanced(run, max_size):
    """Split a run of nodes into balanced groups, each fitting within max_size.

    Strategy: compute the total line range. Determine the minimum number of
    groups needed (ceil(total_range / max_size)). Then distribute nodes across
    that many groups as evenly as possible, respecting the max_size constraint.

    This produces 1,2,3; 4,5,6; 7,8,9 instead of 1; 2,3,4; 5,6,7; 8,9.
    """
    if len(run) <= 1:
        return [run]

    total_start = run[0].get("lineStart", 0)
    total_end = run[-1].get("lineEnd", 0)
    total_range = total_end - total_start + 1

    if total_range <= max_size:
        return [run]

    # Minimum groups needed
    import math
    min_groups = math.ceil(total_range / max_size)
    # Target nodes per group (distribute evenly)
    n = len(run)
    target_per_group = max(1, n // min_groups)

    groups = []
    i = 0
    while i < n:
        # Try target_per_group nodes, then adjust if it exceeds max_size
        end_idx = min(i + target_per_group, n)

        # Expand if we can fit more
        while end_idx < n:
            candidate_range = run[end_idx].get("lineEnd", 0) - run[i].get("lineStart", 0) + 1
            if candidate_range <= max_size:
                end_idx += 1
            else:
                break

        # Contract if we exceed max_size
        while end_idx > i + 1:
            grp_range = run[end_idx - 1].get("lineEnd", 0) - run[i].get("lineStart", 0) + 1
            if grp_range <= max_size:
                break
            end_idx -= 1

        groups.append(run[i:end_idx])
        i = end_idx

    # Rebalance: if the last group is much smaller, try to redistribute
    if len(groups) > 1 and len(groups[-1]) == 1 and len(groups[-2]) > 2:
        # Try moving the last node of the second-to-last group to make groups more even
        pass  # The above algorithm is already reasonably balanced

    return groups


def prune_step2(node, max_size, preserve_scoping=False):
    """Horizontal pruning: merge consecutive siblings within max_size.

    Uses balanced splitting: runs of same-category nodes are divided into
    evenly-sized groups rather than greedy left-to-right packing.

    When preserve_scoping=True, three categories cannot be mixed:
    - 'open' (scope openers) only merge with consecutive 'open'
    - 'close' (scope closers) only merge with consecutive 'close'
    - 'regular' only merge with consecutive 'regular'
    """
    for child in node.get("children", []):
        prune_step2(child, max_size, preserve_scoping)

    children = node.get("children", [])
    if len(children) <= 1:
        return

    if node.get("type") in ("PROJECT", "DIRECTORY", "PC"):
        return

    # Split children into runs of the same category
    runs = []  # list of (category, [nodes])
    current_cat = None
    current_run = []

    for child in children:
        cat = _get_scope_category(child) if preserve_scoping else "regular"
        if cat != current_cat and current_run:
            runs.append((current_cat, current_run))
            current_run = []
        current_cat = cat
        current_run.append(child)
    if current_run:
        runs.append((current_cat, current_run))

    # For each run, split into balanced groups and merge
    merged = []
    for cat, run in runs:
        groups = _split_run_balanced(run, max_size)
        for group in groups:
            merged.append(_merge_group(group))

    node["children"] = merged


def prune_tree(tree, max_size, preserve_scoping=False):
    """Apply pruning to the CST tree.

    When preserve_scoping=True:
      1. Flatten scoping nodes — categorize children as open/close/regular
         (scoping nodes with range <= max_size become all-regular)
      2. Vertical pruning (collapse small subtrees)
      3. Horizontal pruning (merge consecutive same-category siblings)
    """
    if max_size <= 0:
        return

    if preserve_scoping:
        flatten_scoping_node(tree, max_size)

    prune_step1(tree, max_size)
    prune_step2(tree, max_size, preserve_scoping)


_id_counter = 0


def _generate_id():
    """Generate a unique short ID (project-wide unique via counter)."""
    global _id_counter
    _id_counter += 1
    return f"{_id_counter:05x}"


def _is_passthrough(node):
    """Check if a node is a pass-through: single child spanning the same lines."""
    children = node.get("children", [])
    if len(children) != 1:
        return False
    child = children[0]
    return (node.get("lineStart") == child.get("lineStart") and
            node.get("lineEnd") == child.get("lineEnd"))


def _get_source_lines(source_files, file_path):
    """Get source lines for a file (cached split)."""
    content = source_files.get(file_path, "")
    if isinstance(content, str):
        return content.split("\n")
    return []


def _extract_source(source_files, file_path, line_start, line_end, col_start=None, col_end=None):
    """Extract verbatim source code for a node's range."""
    lines = _get_source_lines(source_files, file_path)
    if not lines or line_start <= 0:
        return ""
    start_idx = line_start - 1
    end_idx = min(len(lines), line_end)
    selected = lines[start_idx:end_idx]
    if not selected:
        return ""
    # Apply column trimming on first/last lines if available
    if col_start is not None and col_end is not None:
        if len(selected) == 1:
            selected[0] = selected[0][col_start:col_end]
        else:
            selected[0] = selected[0][col_start:]
            selected[-1] = selected[-1][:col_end]
    return "\n".join(selected)


def assign_ids(node):
    """Assign a unique ID to every node in the tree, recursively."""
    node["id"] = _generate_id()
    for child in node.get("children", []):
        assign_ids(child)


def _inline_signatures_and_build_content(node, source_files):
    """Process tree bottom-up: inline scope signatures into parent content,
    remove signature nodes, and compute content for every node.

    - Leaf nodes: content = verbatim source code
    - PROJECT/DIRECTORY: content = placeholder listing of direct children
    - Pass-through nodes (single child, same range): content = ""
    - Other non-leaf nodes: content = source code frame with child placeholders
      Scope open/close children are inlined (their text appears directly),
      and they are removed from children list.
    """
    # Recurse into children first (bottom-up)
    for child in node.get("children", []):
        _inline_signatures_and_build_content(child, source_files)

    children = node.get("children", [])
    ntype = node.get("type", "")

    # --- PROJECT / DIRECTORY / FILE nodes ---
    if ntype in ("PROJECT", "DIRECTORY", "FILE"):
        if children:
            parts = []
            for child in children:
                child_name = child.get("name", "unknown")
                parts.append(f"<{child_name}_{child['id']}>")
            node["content"] = "\n".join(parts)
        else:
            node["content"] = ""
        return

    # --- Leaf nodes (no children) ---
    if not children:
        # PC alternative leaves without content get their content from source or JSON
        if ntype in ("if_pc", "elif_pc", "else_pc") and not node.get("content"):
            file_path = node.get("file", "")
            ls = node.get("lineStart", 0)
            le = node.get("lineEnd", 0)
            node["content"] = _extract_source(source_files, file_path, ls, le)
        elif not node.get("content"):
            file_path = node.get("file", "")
            ls = node.get("lineStart", 0)
            le = node.get("lineEnd", 0)
            cs = node.get("colStart")
            ce = node.get("colEnd")
            node["content"] = _extract_source(source_files, file_path, ls, le, cs, ce)
        return

    # --- PC nodes ---
    if ntype == "PC":
        # If only one alternative (empty else was skipped), collapse:
        # move alternative's children up to the PC node directly
        if len(children) == 1 and children[0].get("type") in ("if_pc", "elif_pc"):
            only_alt = children[0]
            alt_children = only_alt.get("children", [])
            alt_cond = node.get("pcCondition", "")
            if len(alt_children) == 1:
                # Single child — collapse: promote child directly
                node["children"] = alt_children
                ctype_c = alt_children[0].get("type", "")
                if ctype_c == "PC":
                    ph = f"<PC: {alt_children[0].get('pcCondition', '')}>"
                else:
                    ph = f"<{ctype_c}_{alt_children[0].get('id', '')}>"
                node["content"] = f"if_pc ({alt_cond}) {{\n  {ph}\n}}"
            else:
                # Multiple children — keep the if_pc wrapper as the single placeholder
                alt_id = only_alt.get("id", "")
                node["content"] = f"if_pc ({alt_cond}) {{\n  <if_pc_{alt_id}>\n}}"
            return
        else:
            # Multiple alternatives — build if_pc/elif_pc/else_pc signature
            parts = []
            for child in children:
                # After collapsing, _alt_type holds the original alternative type
                alt_type = child.get("_alt_type", child.get("type", ""))
                child_cond = child.get("pcCondition", "")
                child_id = child.get("id", "")
                if alt_type == "if_pc":
                    parts.append(f"if_pc ({child_cond}) {{")
                    parts.append(f"  <{child_id}>")
                    parts.append("}")
                elif alt_type == "elif_pc":
                    parts.append(f"elif_pc ({child_cond}) {{")
                    parts.append(f"  <{child_id}>")
                    parts.append("}")
                elif alt_type == "else_pc":
                    parts.append("else_pc {")
                    parts.append(f"  <{child_id}>")
                    parts.append("}")
                else:
                    # Non-alternative children (shouldn't happen normally)
                    parts.append(f"<{child.get('type', '')}_{child_id}>")
            node["content"] = "\n".join(parts)
            return

    # --- Nodes containing PCs: inline signatures + child placeholders (no code in frame) ---
    if _contains_pc(node) and ntype not in ("PC", "if_pc", "elif_pc", "else_pc"):
        parts = []
        remaining_children = []
        sig_line_start = None
        sig_line_end = None

        for child in children:
            cls = child.get("lineStart", 0)
            cle = child.get("lineEnd", 0)
            ctype_c = child.get("type", "")

            if ctype_c in ("scope_open", "scope_close"):
                # Inline signature text
                if cls > 0:
                    sig_line_start = cls if sig_line_start is None else min(sig_line_start, cls)
                if cle > 0:
                    sig_line_end = cle if sig_line_end is None else max(sig_line_end, cle)
                sig_text = child.get("content", "")
                if sig_text:
                    parts.append(sig_text)
                continue

            if ctype_c == "PC":
                parts.append(f"<PC: {child.get('pcCondition', '')}>")
            else:
                parts.append(f"<{ctype_c}_{child.get('id', '')}>")
            remaining_children.append(child)

        if sig_line_start is not None:
            node["sigLineStart"] = sig_line_start
            node["sigLineEnd"] = sig_line_end

        node["content"] = "\n".join(parts)
        node["children"] = remaining_children
        return

    # --- Pass-through nodes (never for PC types) ---
    if _is_passthrough(node):
        node["content"] = ""
        return

    # --- Non-leaf nodes with children: list placeholders, inline signatures ---
    parts = []
    remaining_children = []
    sig_line_start = None
    sig_line_end = None

    for child in children:
        ctype = child.get("type", "")
        if ctype in ("scope_open", "scope_close"):
            # Track signature range before inlining
            cls = child.get("lineStart", 0)
            cle = child.get("lineEnd", 0)
            if cls > 0:
                sig_line_start = cls if sig_line_start is None else min(sig_line_start, cls)
            if cle > 0:
                sig_line_end = cle if sig_line_end is None else max(sig_line_end, cle)
            # Inline signature text directly, remove node from children
            sig_text = child.get("content", "")
            if sig_text:
                parts.append(sig_text)
        else:
            # Use descriptive name for PC nodes so condition is visible in parent
            if ctype == "PC":
                pc_cond = child.get("pcCondition", "")
                parts.append(f"<PC: {pc_cond}>")
            else:
                parts.append(f"<{ctype}_{child['id']}>")
            remaining_children.append(child)

    # Preserve signature range on abstract node for edge matching
    if sig_line_start is not None:
        node["sigLineStart"] = sig_line_start
        node["sigLineEnd"] = sig_line_end

    node["content"] = "\n".join(parts)
    node["children"] = remaining_children


def compute_sibling_edges(node):
    """Compute predecessor/successor edges between consecutive leaf nodes.

    Only connects leaf nodes (no children) within the same file.
    Collects all leaves under each FILE node in order, then creates
    edges between consecutive leaves: A→B (SUCCESSOR) and B→A (PREDECESSOR).
    Returns a list of edge dicts.
    """
    edges = []

    def collect_leaves(n):
        """Collect leaf nodes in tree order."""
        children = n.get("children", [])
        if not children:
            return [n]
        leaves = []
        for child in children:
            leaves.extend(collect_leaves(child))
        return leaves

    def process_file(file_node):
        """Create edges between consecutive leaves within a file."""
        leaves = collect_leaves(file_node)
        for i in range(len(leaves) - 1):
            a = leaves[i]
            b = leaves[i + 1]
            edges.append({
                "edgeType": "SUCCESSOR",
                "srcId": a["id"],
                "srcName": a.get("name", ""),
                "srcFile": a.get("file", ""),
                "srcLine": a.get("lineStart", -1),
                "dstId": b["id"],
                "dstName": b.get("name", ""),
                "dstFile": b.get("file", ""),
                "dstLine": b.get("lineStart", -1),
            })
            edges.append({
                "edgeType": "PREDECESSOR",
                "srcId": b["id"],
                "srcName": b.get("name", ""),
                "srcFile": b.get("file", ""),
                "srcLine": b.get("lineStart", -1),
                "dstId": a["id"],
                "dstName": a.get("name", ""),
                "dstFile": a.get("file", ""),
                "dstLine": a.get("lineStart", -1),
            })

    def walk(n):
        ntype = n.get("type", "")
        if ntype == "FILE":
            process_file(n)
        else:
            for child in n.get("children", []):
                walk(child)

    walk(node)
    return edges


def write_edges_csv(edges, csv_path):
    """Write edges to a CSV file."""
    import csv
    os.makedirs(os.path.dirname(csv_path), exist_ok=True)
    with open(csv_path, "w", newline="") as f:
        writer = csv.writer(f)
        writer.writerow([
            "edge_type", "src_id", "src_name", "src_file", "src_line",
            "dst_id", "dst_name", "dst_file", "dst_line",
        ])
        for e in edges:
            writer.writerow([
                e["edgeType"], e["srcId"], e["srcName"], e["srcFile"],
                e["srcLine"], e["dstId"], e["dstName"], e["dstFile"],
                e["dstLine"],
            ])


def enrich_tree(tree, source_files):
    """Post-process the tree: assign IDs, compute content, inline signatures."""
    assign_ids(tree)
    _inline_signatures_and_build_content(tree, source_files)


# ---------------------------------------------------------------------------
# Presence Condition (PC) injection
# ---------------------------------------------------------------------------

def get_spl_section_start_line(file_content):
    """Return the 1-indexed line number where SPL_PRESENCE_CONDITIONS begins, or None."""
    for i, line in enumerate(file_content.split("\n")):
        stripped = line.strip()
        if stripped.startswith("//"):
            stripped = stripped[2:].strip()
        if "SPL_PRESENCE_CONDITIONS" in stripped and "END" not in stripped:
            return i + 1  # 1-indexed
    return None


def strip_spl_section(content, spl_start_line_1indexed):
    """Strip content from SPL section start onward."""
    if spl_start_line_1indexed is None:
        return content
    lines = content.split("\n")
    # spl_start_line_1indexed is 1-indexed
    return "\n".join(lines[:spl_start_line_1indexed - 1]).rstrip()


def _remove_spl_nodes(tree, spl_start_lines):
    """Remove nodes whose lineStart is at or beyond the SPL section start.

    Also removes nodes with empty content (whitespace only).
    """
    def clean(node):
        children = node.get("children", [])
        if not children:
            return
        file_path = node.get("file", "")
        # Try to get file path from children if not on this node
        if not file_path:
            for c in children:
                file_path = c.get("file", "")
                if file_path:
                    break

        spl_start = spl_start_lines.get(file_path)

        new_children = []
        for child in children:
            child_start = child.get("lineStart", 0)
            child_content = child.get("content", "")

            # Remove nodes entirely in SPL section
            if spl_start and child_start >= spl_start:
                continue

            clean(child)
            new_children.append(child)

        node["children"] = new_children

    clean(tree)


def parse_pc_json_from_file(file_content):
    """Extract and parse the SPL presence conditions JSON from a source file.

    Returns a list of PresenceCondition dicts, or [] if none found.
    """
    marker_start = "SPL_PRESENCE_CONDITIONS"
    marker_end = "SPL_PRESENCE_CONDITIONS_END"

    lines = file_content.split("\n")
    in_section = False
    json_lines = []

    for line in lines:
        stripped = line.strip()
        # Strip comment prefixes: //, #, --, etc.
        for prefix in ["//", "#", "--", "*", "/*", "<!--"]:
            if stripped.startswith(prefix):
                stripped = stripped[len(prefix):].strip()
                break

        if marker_end in stripped:
            break
        if in_section:
            json_lines.append(stripped)
        if marker_start in stripped and marker_end not in stripped:
            in_section = True
            # Skip the "DO NOT EDIT" warning line
            continue

    if not json_lines:
        return []

    # Remove "DO NOT EDIT" warning if present
    json_text = "\n".join(json_lines)
    if "DO NOT EDIT" in json_text.split("\n")[0]:
        json_text = "\n".join(json_lines[1:])

    try:
        data = json.loads(json_text)
        return data.get("presenceConditions", [])
    except json.JSONDecodeError:
        return []


def build_pc_tree(pcs):
    """Build a nested tree of PCs sorted by position.

    Returns a list of top-level PCs, each with nested children
    stored in a '_children' field (PCs contained within their range).
    """
    # Sort by start line, then by range size (largest first for nesting)
    sorted_pcs = sorted(pcs, key=lambda p: (
        p["start"]["line"], p["start"]["character"],
        -(p["end"]["line"] - p["start"]["line"])
    ))

    # Build nesting: a PC is a child of another if fully contained
    def contains(outer, inner):
        os, oe = outer["start"]["line"], outer["end"]["line"]
        is_, ie = inner["start"]["line"], inner["end"]["line"]
        if os < is_ and ie < oe:
            return True
        if os == is_ and ie < oe and inner["start"]["character"] >= outer["start"]["character"]:
            return True
        if os < is_ and ie == oe and inner["end"]["character"] <= outer["end"]["character"]:
            return True
        return False

    for pc in sorted_pcs:
        pc["_children"] = []

    roots = []
    for pc in sorted_pcs:
        placed = False
        # Try to find the smallest enclosing PC
        for candidate in reversed(sorted_pcs):
            if candidate is pc:
                continue
            if contains(candidate, pc):
                candidate["_children"].append(pc)
                placed = True
                break
        if not placed:
            roots.append(pc)

    return roots


def pc_overlaps_range(pc, line_start, line_end):
    """Check if a PC's range overlaps with a node's line range.

    Both PC positions and node positions are 1-indexed at this point.
    """
    pc_start = pc["start"]["line"]
    pc_end = pc["end"]["line"]
    return pc_start <= line_end and pc_end >= line_start


def split_node_content_at_pc(node_content_lines, node_line_start_1indexed, pc):
    """Split content lines into before-PC, inside-PC, after-PC segments.

    All line numbers are 1-indexed.
    Returns (before_lines, inside_lines, after_lines) as lists of (line_1indexed, text).
    """
    pc_start = pc["start"]["line"]  # 1-indexed
    pc_end = pc["end"]["line"]      # 1-indexed

    before = []
    inside = []
    after = []

    for i, line_text in enumerate(node_content_lines):
        abs_line = node_line_start_1indexed + i
        if abs_line < pc_start:
            before.append((abs_line, line_text))
        elif abs_line <= pc_end:
            inside.append((abs_line, line_text))
        else:
            after.append((abs_line, line_text))

    return before, inside, after


def make_leaf_from_lines(lines_with_nums, file_path):
    """Create a leaf node dict from a list of (line_1indexed, text) tuples.

    line numbers are 1-indexed throughout.
    """
    if not lines_with_nums:
        return None
    content = "\n".join(text for _, text in lines_with_nums)
    if not content.strip():
        return None
    return {
        "type": "pc_code",
        "name": f"[code] lines {lines_with_nums[0][0]}-{lines_with_nums[-1][0]}",
        "lineStart": lines_with_nums[0][0],
        "lineEnd": lines_with_nums[-1][0],
        "colStart": 0,
        "colEnd": len(lines_with_nums[-1][1]),
        "file": file_path,
        "content": content,
        "children": [],
    }


def make_pc_node(pc, file_path, source_lines, nested_pcs):
    """Create a PC abstract node with alternative children.

    The active alternative's code is in the source file.
    Inactive alternatives' code comes from the PC JSON.
    """
    # PC positions are 1-indexed at this point
    pc_start = pc["start"]["line"]  # 1-indexed
    pc_end = pc["end"]["line"]      # 1-indexed
    alternatives = pc.get("alternatives", [])
    active_idx = pc.get("activeAlternativeIndex", 0)
    condition = pc.get("condition", "")
    color = pc.get("color", "#888888")
    pc_id = pc.get("id", "")

    # Create PC abstract node
    pc_node = {
        "type": "PC",
        "name": f"[PC: {condition}]",
        "lineStart": pc_start,
        "lineEnd": pc_end,
        "colStart": pc["start"].get("character", 0),
        "colEnd": pc["end"].get("character", 0),
        "file": file_path,
        "pcCondition": condition,
        "pcColor": color,
        "pcId": pc_id,
        "children": [],
    }

    # Create children for each alternative
    for i, alt in enumerate(alternatives):
        alt_type = alt.get("type", "if")
        alt_cond = alt.get("condition", "")
        alt_content = alt.get("content", "")

        if alt_type == "if":
            alt_name = f"if_pc ({alt_cond})"
            node_type = "if_pc"
        elif alt_type == "elif":
            alt_name = f"elif_pc ({alt_cond})"
            node_type = "elif_pc"
        else:
            alt_name = "else_pc"
            node_type = "else_pc"

        alt_node = {
            "type": node_type,
            "name": alt_name,
            "lineStart": pc_start,
            "lineEnd": pc_end,
            "file": file_path,
            "pcCondition": alt_cond if alt_type != "else" else "",
            "pcColor": color,
            "children": [],
        }

        if i == active_idx:
            # Active alternative: code is in the source file
            # Get lines within this PC's range (convert 1-indexed to 0-indexed for array access)
            active_lines = []
            for ln_1 in range(pc_start, pc_end + 1):
                ln_0 = ln_1 - 1  # 0-indexed for source_lines array
                if ln_0 < len(source_lines):
                    active_lines.append((ln_1, source_lines[ln_0]))

            # Handle nested PCs: split active lines at nested PC boundaries
            remaining_lines = active_lines
            for child_pc in nested_pcs:
                if not remaining_lines:
                    break
                before, inside, after = split_node_content_at_pc(
                    [text for _, text in remaining_lines],
                    remaining_lines[0][0],
                    child_pc
                )

                before_node = make_leaf_from_lines(before, file_path)
                if before_node:
                    alt_node["children"].append(before_node)

                nested_pc_node = make_pc_node(
                    child_pc, file_path, source_lines,
                    child_pc.get("_children", [])
                )
                alt_node["children"].append(nested_pc_node)

                remaining_lines = after

            if remaining_lines:
                remaining_node = make_leaf_from_lines(remaining_lines, file_path)
                if remaining_node:
                    alt_node["children"].append(remaining_node)

            # If no children were created (single code block, no nested PCs)
            if not alt_node["children"]:
                code = "\n".join(
                    source_lines[ln - 1] for ln in range(pc_start, pc_end + 1)
                    if ln - 1 < len(source_lines)
                )
                if code.strip():
                    alt_node["content"] = code
        else:
            # Inactive alternative: code from JSON
            if alt_content and alt_content.strip():
                alt_node["content"] = alt_content
            else:
                # Empty alternative (auto-generated else) — skip
                continue

        pc_node["children"].append(alt_node)

    # Simplify: if an alternative has exactly one code child, absorb it
    for alt in pc_node["children"]:
        alt_children = alt.get("children", [])
        if len(alt_children) == 1 and alt_children[0].get("type") == "pc_code":
            # Merge the single code child into the alternative node
            child = alt_children[0]
            alt["content"] = child.get("content", "")
            alt["lineStart"] = child.get("lineStart", alt.get("lineStart"))
            alt["lineEnd"] = child.get("lineEnd", alt.get("lineEnd"))
            alt["children"] = []

    # Build signature content showing the if/elif/else structure
    content_parts = []
    for child in pc_node["children"]:
        placeholder = f"<{child['name']}>"
        content_parts.append(placeholder)
    pc_node["content"] = "\n".join(content_parts)

    return pc_node


def inject_pcs_into_file_node(file_node, source_files):
    """Inject presence conditions into a FILE node's CST subtree.

    Works on the raw CST before pruning, so function boundaries are preserved.
    For each PC, finds the deepest AST node that fully contains it, then wraps
    the overlapping children in a PC/alternative structure.
    """
    # FILE nodes don't have a 'file' field — get it from children
    file_path = file_node.get("file", "")
    if not file_path:
        for child in file_node.get("children", []):
            file_path = child.get("file", "")
            if file_path:
                break
    if not file_path:
        return

    file_content = source_files.get(file_path, "")
    if not file_content:
        return

    pcs = parse_pc_json_from_file(file_content)
    if not pcs:
        return

    # Convert PC positions from 0-indexed to 1-indexed (tree uses 1-indexed)
    for pc in pcs:
        pc["start"]["line"] += 1
        pc["end"]["line"] += 1

    source_lines = file_content.split("\n")
    pc_roots = build_pc_tree(pcs)

    if not pc_roots:
        return

    # Inject each top-level PC into the CST
    # Start from children of FILE node (which have proper line ranges)
    for pc in pc_roots:
        for child in file_node.get("children", []):
            cs = child.get("lineStart", 0)
            ce = child.get("lineEnd", 0)
            if cs and ce and cs <= pc["start"]["line"] and ce >= pc["end"]["line"]:
                _inject_single_pc(child, pc, source_lines, file_path)
                break


def _inject_single_pc(node, pc, source_lines, file_path):
    """Inject a single PC into the CST by finding the deepest containing node
    and wrapping the overlapping children.

    Handles nested PCs recursively.
    """
    pc_start = pc["start"]["line"]
    pc_end = pc["end"]["line"]

    # Find the deepest node that fully contains this PC
    target = _find_deepest_container(node, pc_start, pc_end)
    if not target:
        return

    children = target.get("children", [])
    if not children:
        # Target is a leaf — wrap it entirely
        _wrap_leaf_in_pc(target, pc, source_lines, file_path)
        return

    # Find which children overlap with this PC
    before = []
    overlapping = []
    after = []

    for child in children:
        cs = child.get("lineStart", 0)
        ce = child.get("lineEnd", 0)
        if ce < pc_start:
            before.append(child)
        elif cs > pc_end:
            after.append(child)
        else:
            overlapping.append(child)

    if not overlapping:
        return

    # Build the PC node wrapping the overlapping children
    pc_node = _build_pc_from_children(pc, overlapping, source_lines, file_path)

    # Inject nested PCs into the active alternative's children
    for child_pc in pc.get("_children", []):
        # Find the if_pc child (active alternative) and inject into it
        for alt in pc_node.get("children", []):
            if alt.get("type") == "if_pc" and alt.get("children"):
                _inject_single_pc(alt, child_pc, source_lines, file_path)
                break

    # Rebuild target's children: before + PC node + after
    target["children"] = before + [pc_node] + after


def _find_deepest_container(node, pc_start, pc_end):
    """Find the deepest node in the tree that fully contains the PC range."""
    ns = node.get("lineStart", 0)
    ne = node.get("lineEnd", 0)

    # This node must fully contain the PC
    if ns > pc_start or ne < pc_end:
        return None

    # Try to find a deeper child that also fully contains it
    for child in node.get("children", []):
        cs = child.get("lineStart", 0)
        ce = child.get("lineEnd", 0)
        if cs <= pc_start and ce >= pc_end:
            deeper = _find_deepest_container(child, pc_start, pc_end)
            if deeper:
                return deeper

    return node


def _build_pc_from_children(pc, overlapping_children, source_lines, file_path):
    """Build a PC node that wraps the overlapping CST children."""
    pc_start = pc["start"]["line"]
    pc_end = pc["end"]["line"]
    condition = pc.get("condition", "")
    color = pc.get("color", "#888888")
    pc_id = pc.get("id", "")
    alternatives = pc.get("alternatives", [])
    active_idx = pc.get("activeAlternativeIndex", 0)

    pc_node = {
        "type": "PC",
        "name": f"[PC: {condition}]",
        "file": file_path,
        "lineStart": pc_start,
        "lineEnd": pc_end,
        "colStart": pc["start"].get("character", 0),
        "colEnd": pc["end"].get("character", 0),
        "pcCondition": condition,
        "pcColor": color,
        "pcId": pc_id,
        "children": [],
    }

    for i, alt in enumerate(alternatives):
        alt_type = alt.get("type", "if")
        alt_cond = alt.get("condition", "")
        alt_content = alt.get("content", "")

        if alt_type == "if":
            alt_name = f"if_pc ({alt_cond})"
            node_type = "if_pc"
        elif alt_type == "elif":
            alt_name = f"elif_pc ({alt_cond})"
            node_type = "elif_pc"
        else:
            alt_name = "else_pc"
            node_type = "else_pc"

        alt_node = {
            "type": node_type,
            "name": alt_name,
            "file": file_path,
            "lineStart": pc_start,
            "lineEnd": pc_end,
            "pcCondition": alt_cond if alt_type != "else" else "",
            "pcColor": color,
            "children": [],
        }

        if i == active_idx:
            # Active alternative: its children are the overlapping CST nodes
            alt_node["children"] = list(overlapping_children)
        else:
            # Inactive alternative: code from JSON
            if alt_content and alt_content.strip():
                alt_node["content"] = alt_content
                alt_node["children"] = []
            else:
                # Empty alternative — skip
                continue

        pc_node["children"].append(alt_node)

    return pc_node


def _wrap_leaf_in_pc(leaf_parent_unused, pc, source_lines, file_path):
    """Handle the case where a PC exactly covers a leaf node.
    This shouldn't happen in practice since PCs cover code regions
    that are typically inside compound statements with children.
    """
    pass


def _collapse_single_child_alternatives(node):
    """For multi-alternative PCs, if an alternative has exactly 1 child,
    replace the alternative node with the child directly, transferring
    the alternative's name/condition onto the child.
    """
    ntype = node.get("type", "")

    if ntype == "PC":
        new_children = []
        for alt in node.get("children", []):
            alt_type = alt.get("type", "")
            alt_children = alt.get("children", [])

            if alt_type in ("if_pc", "elif_pc", "else_pc") and len(alt_children) == 1:
                # Collapse: promote the single child, keep the alt's PC metadata
                child = alt_children[0]
                child["pcCondition"] = alt.get("pcCondition", child.get("pcCondition", ""))
                child["pcColor"] = alt.get("pcColor", child.get("pcColor", ""))
                # Prefix the child's name with the alternative type
                alt_name = alt.get("name", alt_type)
                child["name"] = f"{alt_name}: {child.get('name', '')}"
                child["_alt_type"] = alt_type
                new_children.append(child)
            else:
                new_children.append(alt)
        node["children"] = new_children

    for child in node.get("children", []):
        _collapse_single_child_alternatives(child)


def _darken_color(hex_color, factor=0.7):
    """Darken a hex color by the given factor (0=black, 1=unchanged)."""
    if not hex_color or not hex_color.startswith("#") or len(hex_color) != 7:
        return hex_color
    try:
        r = int(hex_color[1:3], 16)
        g = int(hex_color[3:5], 16)
        b = int(hex_color[5:7], 16)
        r = int(r * factor)
        g = int(g * factor)
        b = int(b * factor)
        return f"#{r:02x}{g:02x}{b:02x}"
    except ValueError:
        return hex_color


def _propagate_pc_colors(node, inherited_color="", depth=0):
    """Propagate pcColor and pcDepth to all descendants of PC nodes.

    Nested PCs get a darker version of their own color.
    pcDepth indicates nesting level (0=outermost PC, 1=nested, etc.)
    """
    ntype = node.get("type", "")

    if ntype == "PC":
        color = node.get("pcColor", inherited_color)
        if depth > 0:
            color = _darken_color(color, 0.7)
            node["pcColor"] = color
        node["pcDepth"] = depth
        for child in node.get("children", []):
            child["pcColor"] = child.get("pcColor") or color
            child["pcDepth"] = depth
            _propagate_pc_colors(child, color, depth + 1)
    elif ntype in ("if_pc", "elif_pc", "else_pc"):
        color = node.get("pcColor", inherited_color)
        node["pcColor"] = color
        node["pcDepth"] = depth
        for child in node.get("children", []):
            child["pcColor"] = child.get("pcColor") or color
            child["pcDepth"] = depth
            _propagate_pc_colors(child, color, depth)
    else:
        if inherited_color:
            node["pcColor"] = node.get("pcColor") or inherited_color
            node["pcDepth"] = depth
        color = node.get("pcColor", inherited_color)
        for child in node.get("children", []):
            _propagate_pc_colors(child, color, depth)


def inject_presence_conditions(tree, source_files_original):
    """Top-level function: inject presence conditions into all FILE nodes.

    Uses original (unstripped) source files for PC JSON parsing.
    """
    global _injected_pc_ids
    _injected_pc_ids = set()

    def walk(node):
        if node.get("type") == "FILE":
            inject_pcs_into_file_node(node, source_files_original)
        for child in node.get("children", []):
            walk(child)
    walk(tree)

    # Note: collapsing and color propagation run after pruning/enrichment in main()


def main():
    if len(sys.argv) < 2:
        emit("error", message="Usage: treesitter_ast.py <project_path> [--max-node-size N]")
        sys.exit(1)

    project_path = os.path.abspath(sys.argv[1])
    project_name = os.path.basename(project_path)

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

    # Check tree-sitter is available
    try:
        from tree_sitter import Parser
    except ImportError:
        emit("error", message="tree-sitter not installed. Run: pip install -r requirements.txt")
        sys.exit(1)

    from tree_sitter import Parser

    log("=== Phase 1: Tree-sitter CST Parsing ===")

    # Find all source files
    log("Scanning for source files...")
    source_paths = []
    for root, dirs, filenames in os.walk(project_path):
        dirs[:] = [d for d in dirs if d not in SKIP_DIRS and not d.startswith(".")]
        for fname in filenames:
            _, ext = os.path.splitext(fname)
            if ext.lower() in EXTENSION_TO_TS_LANG:
                full_path = os.path.join(root, fname)
                rel_path = os.path.relpath(full_path, project_path)
                source_paths.append((rel_path, full_path, ext.lower()))

    log(f"Found {len(source_paths)} source files.")

    if not source_paths:
        emit("error", message="No parseable source files found in project.")
        sys.exit(1)

    # Parse each file — read content and parse in one pass
    cst_by_file = {}
    source_files = {}  # collected during parsing, no separate snapshot needed
    lang_counts = {}
    parse_errors = 0
    start_time = time.time()

    total_files = len(source_paths)
    last_pct = -1
    for file_idx, (rel_path, full_path, ext) in enumerate(sorted(source_paths)):
        lang = get_language(ext)
        if not lang:
            continue

        lang_name = EXTENSION_TO_LANGUAGE_NAME.get(ext, ext)
        lang_counts[lang_name] = lang_counts.get(lang_name, 0) + 1

        try:
            with open(full_path, "r", errors="replace") as f:
                content = f.read()

            source_files[rel_path] = content
            parser = Parser(lang)
            tree = parser.parse(content.encode("utf-8"))
            cst_root = node_to_dict(tree.root_node, rel_path)
            cst_by_file[rel_path] = [cst_root]
        except Exception as e:
            log(f"  Error parsing {rel_path}: {e}")
            parse_errors += 1

        # Log progress every 5%
        pct = ((file_idx + 1) * 100) // total_files
        if pct >= last_pct + 5 or file_idx == total_files - 1:
            log(f"  Parsing: {pct}% ({file_idx + 1}/{total_files} files)")
            last_pct = pct

    elapsed = time.time() - start_time
    total_loc = sum(content.count("\n") + 1 for content in source_files.values())
    log(f"Tree-sitter parsing completed in {elapsed:.1f}s.")
    log(f"Total lines of code: {total_loc}")

    # Log language stats
    if lang_counts:
        lang_summary = ", ".join(
            f"{lang}: {count} file{'s' if count != 1 else ''}"
            for lang, count in sorted(lang_counts.items(), key=lambda x: -x[1])
        )
        log(f"Languages detected: {lang_summary}")

    if parse_errors:
        log(f"  {parse_errors} files had parse errors.")

    # Keep original content for PC JSON parsing, strip SPL sections for content extraction
    source_files_original = dict(source_files)
    spl_start_lines = {}  # rel_path -> 1-indexed line where SPL section starts
    for rel_path, content in list(source_files.items()):
        spl_line = get_spl_section_start_line(content)
        if spl_line:
            spl_start_lines[rel_path] = spl_line
            source_files[rel_path] = strip_spl_section(content, spl_line)

    # Build tree
    tree = build_file_tree(cst_by_file, project_name)
    total_nodes_before = count_nodes(tree)
    log(f"CST tree built: {len(cst_by_file)} files, {total_nodes_before} total nodes.")

    # Remove nodes that fall entirely within SPL JSON sections (before pruning)
    if spl_start_lines:
        _remove_spl_nodes(tree, spl_start_lines)

    # Inject presence conditions into CST (before pruning so function structure is preserved)
    log("Scanning for presence conditions...")
    inject_presence_conditions(tree, source_files_original)
    pc_nodes = count_nodes(tree) - count_nodes(tree)  # just for logging
    log(f"Presence conditions injected. Tree: {count_nodes(tree)} nodes.")

    # Apply pruning if requested
    if max_node_size > 0:
        scoping_msg = " (preserving scoping structure)" if preserve_scoping else ""
        log(f"Pruning tree with max node size = {max_node_size} lines{scoping_msg}...")
        prune_tree(tree, max_node_size, preserve_scoping)
        total_nodes_pruned = count_nodes(tree)
        log(f"Pruned: {total_nodes_before} -> {total_nodes_pruned} nodes.")

    # Enrich: assign IDs, compute content frames, inline signatures
    log("Enriching tree with IDs and content...")
    enrich_tree(tree, source_files)

    # Collapse single-child alternatives AFTER pruning (children may have been merged)
    _collapse_single_child_alternatives(tree)

    # Propagate PC colors AFTER pruning/enrichment so merged nodes get colored
    _propagate_pc_colors(tree)

    final_nodes = count_nodes(tree)
    log(f"Final chunk tree: {final_nodes} nodes.")

    # Compute sibling edges and write CSV
    edges = compute_sibling_edges(tree)
    csv_path = os.path.join(project_path, "vdg-output", "edges.csv")
    write_edges_csv(edges, csv_path)
    log(f"Computed {len(edges)} sibling edges, saved to vdg-output/edges.csv")

    # Write chunk tree JSON (full node properties, nested structure)
    chunk_tree_path = os.path.join(project_path, "vdg-output", "chunk_tree.json")
    os.makedirs(os.path.dirname(chunk_tree_path), exist_ok=True)
    with open(chunk_tree_path, "w") as f:
        json.dump(tree, f, indent=2)
    log(f"Chunk tree saved to vdg-output/chunk_tree.json")

    emit("result", data=tree, sourceFiles=source_files, edges=edges, totalLoc=total_loc)


if __name__ == "__main__":
    main()
