# -*- coding: utf-8 -*-
"""
prompt_pipeline.cast_lock
=========================
Server-side Named Cast Lock enforcement — the contract `named-cast-vocabulary-lock` (P0) and
`named-cast-hal-exclusivity` (P0) registered against this module.

Both contracts previously carried a `gap`: the only enforcement anywhere was
`scripts/check_character_lock.py`, a CLI the agent had to remember to run before hand-off.
A lock that is off unless someone remembers it is off. This module runs the same gates at
compose time, on every beat, without anyone opting in.

Two deliberate design choices:

**The gates are not reimplemented here.** They live in the skill package's
`scripts/cast_lock_core.py` and are loaded from there. Writing a second copy of eight regex
gates in this file is how the registry/prose contradictions that both contract registries
exist to catch get created — one rule stated two ways with nothing comparing them.

**The core module is loaded from the vendored path only**, never from `skill_dir()`.
`skill_dir()` is user-configurable and auto-detected across `~/.codex`, `~/.claude` and
`~/.agents`; importing Python from it would mean the server executes code out of a directory
the operator can repoint. Reference *data* legitimately comes from `skill_dir()` (that is what
`skill_reference_path` is for, and it is what makes an operator's edited cast registry take
effect) — executable code does not.

Findings are advisory, not blocking. They are returned as plain strings into the same list
`validate_beat_prompts` already returns, which in 直出模式 is logged rather than enforced
(see composers/base.py). That is the intended first rollout stage: observe the false-positive
rate on real traffic before anything gets to fail a compose over a wardrobe adjective.
"""
import importlib.util
import json
import os
import sys

_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_CORE_PATH = os.path.join(_PROJECT_ROOT, 'skills', 'gemini-veo-restoration-composer',
                          'scripts', 'cast_lock_core.py')

NAMED_CAST_MODE = 'named_cast'

_core = None
_core_error = None


def cast_lock_core():
    """The shared gate implementation, or None when the vendored skill package is absent.

    Absence is a legitimate deployment (the operator points SKILL_DIR at a downloaded package
    and never vendored `skills/`), so it degrades to "no enforcement" rather than raising —
    but it says so once, because a silently disabled P0 gate is the failure this module was
    written to end. Same reasoning as the numpy note in requirements.txt.
    """
    global _core, _core_error
    if _core is not None or _core_error is not None:
        return _core
    if not os.path.exists(_CORE_PATH):
        _core_error = f"cast_lock_core.py not found at {_CORE_PATH}"
        if sys.stdout:
            print(f"[WARN] Named Cast Lock disabled: {_core_error}")
        return None
    try:
        spec = importlib.util.spec_from_file_location('_cast_lock_core', _CORE_PATH)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
    except Exception as e:  # pragma: no cover - a corrupt vendored file
        _core_error = f"cast_lock_core.py failed to load: {e}"
        if sys.stdout:
            print(f"[WARN] Named Cast Lock disabled: {_core_error}")
        return None
    _core = module
    return _core


def _registry_path(profile=None):
    """Cast registry path via the active skill dir, falling back to the vendored copy.

    Data, unlike code, is read from `skill_dir()` on purpose: an operator who edits their
    package's cast registry expects the server to honour it.
    """
    try:
        from server_common import skill_reference_path
        path = skill_reference_path('cast-registry.json', profile)
        if os.path.exists(path):
            return path
    except Exception:
        pass
    vendored = os.path.join(_PROJECT_ROOT, 'skills', 'gemini-veo-restoration-composer',
                            'references', 'cast-registry.json')
    return vendored if os.path.exists(vendored) else None


_registry_cache = {}


def load_cast_registry(profile=None):
    """Parsed cast registry, cached by (path, mtime) so an edit takes effect without a restart.

    Mirrors `server_common.skill_contract_registry`'s caching rule for the same reason: this
    is called once per beat per compose, and re-reading plus re-parsing the file every time is
    both wasteful and a way to flood the log with the same JSON error.
    """
    path = _registry_path(profile)
    if not path:
        return None
    try:
        stamp = os.path.getmtime(path)
    except OSError:
        return None
    cached = _registry_cache.get(path)
    if cached and cached[0] == stamp:
        return cached[1]
    try:
        with open(path, 'r', encoding='utf-8') as f:
            data = json.load(f)
        if not isinstance(data, dict):
            raise ValueError('cast registry root is not an object')
    except (OSError, ValueError) as e:
        if sys.stdout:
            print(f"[WARN] Named Cast Lock: unreadable cast registry {path}: {e}")
        data = None
    _registry_cache[path] = (stamp, data)
    return data


def named_cast_settings(packet, config=None):
    """(agent_lock_mode, cast_id) for this compose, or (None, None) when the lock is off.

    Read off the Drift Lock packet first because that is where the protocol says the choice is
    recorded (`agent_lock_mode: named_cast`), with the compose config as the source that puts
    it there. Hero Agent Lock stays the package default: absent an explicit `named_cast`, this
    module does nothing at all.
    """
    packet = packet if isinstance(packet, dict) else {}
    config = config if isinstance(config, dict) else {}
    mode = (packet.get('agent_lock_mode')
            or config.get('agentLockMode') or config.get('agent_lock_mode') or '')
    mode = str(mode).strip().lower().replace('-', '_')
    if mode != NAMED_CAST_MODE:
        return None, None
    cast_id = (packet.get('cast_id') or config.get('castId') or config.get('cast_id') or '')
    return NAMED_CAST_MODE, str(cast_id).strip() or None


def apply_named_cast_settings(packet, config):
    """Stamp the compose-level lock choice onto the Drift Lock packet.

    The packet is what every downstream check receives; the config is not. Without this the
    lock would be unreachable from `validate_beat_prompts` no matter what the operator
    configured.
    """
    if not isinstance(packet, dict) or not isinstance(config, dict):
        return packet
    mode = str(config.get('agentLockMode') or config.get('agent_lock_mode') or '')
    mode = mode.strip().lower().replace('-', '_')
    if mode == NAMED_CAST_MODE:
        packet['agent_lock_mode'] = NAMED_CAST_MODE
        cast_id = config.get('castId') or config.get('cast_id')
        if cast_id:
            packet['cast_id'] = str(cast_id).strip()
    return packet


def named_cast_lock_violations(prompt, label='VIDEO', cast_id=None, profile=None,
                               include_warnings=True):
    """Named Cast Lock findings for one composed prompt, as flat strings.

    Returns [] whenever the lock cannot or should not run — no cast id, no registry, unknown
    cast, empty prompt. Callers merge the result into the existing per-beat error list; in
    直出模式 that list is logged, not enforced.
    """
    if not prompt or not cast_id:
        return []
    core = cast_lock_core()
    if core is None:
        return []
    registry = load_cast_registry(profile)
    if not registry:
        return []
    cast = core.find_cast(registry, cast_id)
    if cast is None:
        if sys.stdout:
            print(f"[WARN] Named Cast Lock: unknown cast id {cast_id!r}; lock not applied")
        return []
    if not str(label).startswith(core.judged_kinds_for(cast)):
        return []
    globals_ = core.registry_globals(registry, cast)
    fnd = core.Findings()
    core.audit_prompt(label, prompt, cast, globals_, fnd)
    rows = fnd.rows if include_warnings else fnd.errors
    return [f"Named Cast Lock [{gate}] {slot}: {detail}" for gate, slot, detail, _ in rows]


def named_cast_beat_violations(image_prompt, video_prompt, packet, config=None, profile=None):
    """Both prompts of one beat, gated on the packet's `agent_lock_mode`.

    IMAGE is judged only in pipeline mode B: in mode A every IMAGE anchor is person-free by
    contract and already owned by Clean Frame Boundary, so judging it here would just report
    the same frame twice under two names. `named_cast_lock_violations` applies that rule via
    `judged_kinds_for`.
    """
    mode, cast_id = named_cast_settings(packet, config)
    if mode != NAMED_CAST_MODE or not cast_id:
        return []
    out = []
    out.extend(named_cast_lock_violations(video_prompt, label='VIDEO', cast_id=cast_id,
                                          profile=profile))
    out.extend(named_cast_lock_violations(image_prompt, label='IMAGE', cast_id=cast_id,
                                          profile=profile))
    return out
