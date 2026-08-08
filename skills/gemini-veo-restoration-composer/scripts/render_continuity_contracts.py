#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
render_continuity_contracts.py
================================
Regenerates references/continuity-contracts.md from references/skill-local-contracts.json.

The registry is the single source of truth (see the _comment block at the top of
skill-local-contracts.json for why). This script is the only thing that should ever write
continuity-contracts.md — edit the registry, then run this, not the other way around.

Usage:
    python scripts/render_continuity_contracts.py
    python scripts/validate_contracts.py   # confirm the registry itself still resolves
"""
import json
import sys
from pathlib import Path

SKILL_DIR = Path(__file__).resolve().parent.parent
REGISTRY_PATH = SKILL_DIR / "references" / "skill-local-contracts.json"
OUTPUT_PATH = SKILL_DIR / "references" / "continuity-contracts.md"

TIER_LABELS = {
    "P0": "P0 — Kill Gates (block delivery)",
    "P1": "P1 — Rewrite Gates (score deduction, fix before delivery)",
    "P2": "P2 — Polish / Advisory",
}
TIER_ORDER = {"P0": 0, "P1": 1, "P2": 2}


def render(registry: dict) -> str:
    contracts = sorted(registry["contracts"], key=lambda c: (TIER_ORDER.get(c["tier"], 9), c["id"]))
    lines = [
        "# Continuity Contracts",
        "",
        "Rendered from [`references/skill-local-contracts.json`](skill-local-contracts.json) — "
        "**edit the registry, not this file.** Regenerate with "
        "`python scripts/render_continuity_contracts.py` after any registry change, then run "
        "`python scripts/validate_contracts.py` to confirm every `enforcer` still resolves to real code.",
        "",
        "This is the skill-local counterpart to the project root's "
        "`references/contract-registry.json` (which governs the server-side rendering "
        "pipeline in `prompt_pipeline/__init__.py`). This file governs only what ships "
        "inside this skill package: `video_to_prompt_pipeline.py` (the standalone Tier-4 "
        "video reverse-engineering CLI) and `scripts/render_and_gate_anchor.py`.",
        "",
    ]
    for tier in ("P0", "P1", "P2"):
        group = [c for c in contracts if c["tier"] == tier]
        if not group:
            continue
        lines.append(f"## {TIER_LABELS[tier]}")
        lines.append("")
        for c in group:
            enforcer = c.get("enforcer")
            enforced_str = f"`{enforcer}`" if enforcer else "*(no programmatic enforcer — see gap note)*"
            lines.append(f"### `{c['id']}`")
            lines.append("")
            lines.append(c["rule_zh"])
            lines.append("")
            lines.append(f"- **Source**: {c.get('source', '(unspecified)')}")
            lines.append(f"- **Enforced by**: {enforced_str}")
            if c.get("gap"):
                lines.append(f"- **Gap**: {c['gap']}")
            if c.get("note_zh"):
                lines.append(f"- **Note**: {c['note_zh']}")
            lines.append("")
    return "\n".join(lines) + "\n"


def main():
    if not REGISTRY_PATH.exists():
        print(f"[-] Registry not found: {REGISTRY_PATH}")
        return 1
    registry = json.loads(REGISTRY_PATH.read_text(encoding="utf-8"))
    OUTPUT_PATH.write_text(render(registry), encoding="utf-8")
    print(f"[+] Wrote {OUTPUT_PATH} from {len(registry['contracts'])} contract(s).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
