#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
validate_contracts.py
======================
Proves references/skill-local-contracts.json's `enforced_by` pointers still resolve to real
code in this skill package, so a rename/removal in video_to_prompt_pipeline.py or
render_and_gate_anchor.py shows up here as a red exit code instead of silently drifting away
from the registry (the exact failure mode that produced the SPCP/Grid/word-count/beat-overload
contradictions this registry was introduced to stop -- the same rule stated one way in
SKILL.md prose and another way in code, with nothing ever comparing the two).

This is the skill-local counterpart to the project's ROOT-level contract-registry.json /
tests/test_skill_contract_registry.py, which validates a DIFFERENT registry against the
server-side prompt_pipeline package (prompt_pipeline/__init__.py, driving server.py). The two
registries are intentionally not merged: the root one is a live, pytest-covered contract for
the server's rendering pipeline; this one is scoped to the files that ship inside this skill
package (a standalone CLI used for Tier-4 video reverse-engineering, plus this skill's own
render-gate helper script). Run this one directly:

    python scripts/validate_contracts.py

Exit code 0: every contract resolves to real code, or has an explicitly documented gap.
Exit code 1: something drifted -- a broken pointer, an undocumented gap, or a malformed entry.
"""
import ast
import json
import sys
from pathlib import Path

SKILL_DIR = Path(__file__).resolve().parent.parent
REGISTRY_PATH = SKILL_DIR / "references" / "skill-local-contracts.json"
SKILL_COMMON_PATH = SKILL_DIR / "scripts" / "skill_common.py"

PY_FILES = {
    "video_to_prompt_pipeline.py": SKILL_DIR / "video_to_prompt_pipeline.py",
    "scripts/render_and_gate_anchor.py": SKILL_DIR / "scripts" / "render_and_gate_anchor.py",
    "scripts/check_character_lock.py": SKILL_DIR / "scripts" / "check_character_lock.py",
}

REQUIRED_FIELDS = ("id", "tier", "rule_zh", "blocking")


def _load_exit_constants(py_path: Path):
    """Module-level `EXIT_* = <int>` assignments in skill_common.py, so a call site written
    as `sys.exit(EXIT_SERVER_ERROR)` can be resolved to its numeric code without every
    helper script re-declaring the vocabulary as literals (see skill_common.py's own
    'one exit-code vocabulary' rationale)."""
    if not py_path.exists():
        return {}
    tree = ast.parse(py_path.read_text(encoding="utf-8"), filename=str(py_path))
    constants = {}
    for node in tree.body:
        if isinstance(node, ast.Assign) and len(node.targets) == 1 \
                and isinstance(node.targets[0], ast.Name) \
                and node.targets[0].id.startswith("EXIT_") \
                and isinstance(node.value, ast.Constant) and isinstance(node.value.value, int):
            constants[node.targets[0].id] = node.value.value
    return constants


def _load_module_facts(py_path: Path, exit_constants):
    """Static-analyze one Python file and collect everything an `enforced_by` pointer can
    resolve against: every def'd function/class name at any nesting level (gates are mostly
    inlined inside run_scup_audit(), not one-function-per-gate, so nested defs count too),
    every string literal (gate identity lives in `"name": "<Gate Display Name>"` dict
    literals, not in a function name), and every exit code passed to an exit()/sys.exit()
    call (render_and_gate_anchor.py's contract is its exit-code taxonomy, not named gates) —
    whether written as a literal int or as one of skill_common.py's named EXIT_* constants."""
    source = py_path.read_text(encoding="utf-8")
    tree = ast.parse(source, filename=str(py_path))
    function_names, class_names, string_literals, exit_codes = set(), set(), set(), set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            function_names.add(node.name)
        elif isinstance(node, ast.ClassDef):
            class_names.add(node.name)
        elif isinstance(node, ast.Constant) and isinstance(node.value, str):
            string_literals.add(node.value)
        elif isinstance(node, ast.Call):
            func = node.func
            is_exit_call = (isinstance(func, ast.Name) and func.id == "exit") or \
                          (isinstance(func, ast.Attribute) and func.attr == "exit")
            if is_exit_call and node.args:
                arg = node.args[0]
                if isinstance(arg, ast.Constant) and isinstance(arg.value, int):
                    exit_codes.add(arg.value)
                elif isinstance(arg, ast.Name) and arg.id in exit_constants:
                    exit_codes.add(exit_constants[arg.id])
    return {"functions": function_names, "classes": class_names,
            "strings": string_literals, "exit_codes": exit_codes}


def _resolve(enforced_by, facts_by_file):
    """'<file>::<kind>:<value>' -> True/False. kind is one of function | class | gate_name |
    exit_code. Returns False (not None) for anything unparseable so a malformed pointer reads
    as broken rather than silently skipped."""
    if not enforced_by:
        return None
    try:
        file_part, rest = enforced_by.split("::", 1)
        kind, value = rest.split(":", 1)
    except ValueError:
        return False
    facts = facts_by_file.get(file_part)
    if facts is None:
        return False
    if kind == "function":
        return value in facts["functions"]
    if kind == "class":
        return value in facts["classes"]
    if kind == "gate_name":
        return value in facts["strings"]
    if kind == "exit_code":
        try:
            return int(value) in facts["exit_codes"]
        except ValueError:
            return False
    return False


def main():
    if not REGISTRY_PATH.exists():
        print(f"[-] Registry not found: {REGISTRY_PATH}")
        return 1
    try:
        registry = json.loads(REGISTRY_PATH.read_text(encoding="utf-8"))
    except json.JSONDecodeError as e:
        print(f"[-] Registry is not valid JSON: {e}")
        return 1

    exit_constants = _load_exit_constants(SKILL_COMMON_PATH)
    facts_by_file = {}
    for name, path in PY_FILES.items():
        if not path.exists():
            print(f"[-] Expected source file missing: {path}")
            return 1
        facts_by_file[name] = _load_module_facts(path, exit_constants)

    contracts = registry.get("contracts", [])
    if not contracts:
        print("[-] Registry has no contracts.")
        return 1

    broken, undocumented_gaps, missing_fields, ok_count = [], [], [], 0
    seen_ids = set()
    for contract in contracts:
        cid = contract.get("id") or "(no id)"
        if cid in seen_ids:
            broken.append(f"{cid} -> duplicate contract id")
        seen_ids.add(cid)

        for field in REQUIRED_FIELDS:
            if field not in contract:
                missing_fields.append(f"{cid} missing required field '{field}'")

        enforced_by = contract.get("enforcer")
        gap = contract.get("gap")
        if not enforced_by:
            if not str(gap or "").strip():
                undocumented_gaps.append(cid)
            continue
        resolved = _resolve(enforced_by, facts_by_file)
        if resolved:
            ok_count += 1
        else:
            broken.append(f"{cid} -> {enforced_by} (does not resolve to real code)")

    print(f"[*] {len(contracts)} contract(s) in registry; {ok_count} resolved to real code; "
          f"{len(contracts) - ok_count - len(undocumented_gaps)} have a documented gap.")
    if broken:
        print(f"\n[-] {len(broken)} contract(s) point at code that no longer exists:")
        for b in broken:
            print(f"    {b}")
    if undocumented_gaps:
        print(f"\n[-] {len(undocumented_gaps)} contract(s) have no enforcer AND no documented gap:")
        for g in undocumented_gaps:
            print(f"    {g}")
    if missing_fields:
        print(f"\n[-] {len(missing_fields)} field(s) missing:")
        for m in missing_fields:
            print(f"    {m}")

    if broken or undocumented_gaps or missing_fields:
        print("\n[-] FAILED — registry has drifted from the code it describes.")
        return 1
    print("\n[+] PASSED — every contract resolves to real code or has a documented gap.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
