#!/usr/bin/env python3
"""Server-owned policy journal helpers.

The LLM is never trusted to edit ``memory.md``.  This module owns a small,
bounded journal block while leaving the scorecard lessons block and every
other byte of an existing diary untouched.
"""
from __future__ import annotations

import json
import pathlib
import re
from typing import Any, Mapping


JOURNAL_START = "<!-- AI_POLICY_JOURNAL_START -->"
JOURNAL_END = "<!-- AI_POLICY_JOURNAL_END -->"
JOURNAL_MAX_ENTRIES = 48
_ENTRY_RE = re.compile(
    r"^- activation=(?P<activation>[A-Za-z0-9._:-]{1,96}) "
    r"at=(?P<at>\S+) fp=(?P<fp>[0-9a-f]{8,64}) note=(?P<note>.*)$"
)


def _atomic_text(path: pathlib.Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.tmp")
    tmp.write_text(text, encoding="utf-8")
    tmp.replace(path)


def _one_line(value: Any, limit: int = 160) -> str:
    text = " ".join(str(value or "").replace("\x00", "").split())
    # Keep the server-owned line grammar intact even if a model note contains
    # markdown or marker-shaped content.
    text = text.replace("<!--", "").replace("-->", "")
    return text[:limit] or "(no note)"


def _policy_fingerprint(policy: Mapping[str, Any]) -> str:
    # Imported lazily to avoid an import cycle at module load time.
    from bots.ai_agents.experience_scorecard import _policy_fingerprint, policy_view

    return _policy_fingerprint(policy_view(policy))


def analyze_memory_text(text: str) -> dict[str, Any]:
    """Return a non-mutating integrity report for managed journal markers."""
    start_count = text.count(JOURNAL_START)
    end_count = text.count(JOURNAL_END)
    issues: list[str] = []
    if start_count != end_count:
        issues.append("unbalanced-policy-journal-markers")
    if start_count > 1 or end_count > 1:
        issues.append("duplicate-policy-journal-markers")
    entries: list[dict[str, str]] = []
    if start_count == end_count == 1:
        start = text.index(JOURNAL_START) + len(JOURNAL_START)
        end = text.index(JOURNAL_END, start)
        for line in text[start:end].splitlines():
            match = _ENTRY_RE.match(line.strip())
            if match:
                entries.append(match.groupdict())
            elif line.strip() and not line.lstrip().startswith("#"):
                issues.append("unrecognized-policy-journal-line")
    return {
        "status": "ready" if not issues else "quarantine-recommended",
        "issues": sorted(set(issues)),
        "journal_start_count": start_count,
        "journal_end_count": end_count,
        "entry_count": len(entries),
        "activation_ids": [entry["activation"] for entry in entries],
    }


def analyze_memory_file(path: pathlib.Path) -> dict[str, Any]:
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        text = ""
    except (OSError, UnicodeError) as exc:
        return {
            "status": "unreadable",
            "issues": [f"{type(exc).__name__}: {exc}"],
            "entry_count": 0,
            "activation_ids": [],
        }
    return analyze_memory_text(text)


def _render_journal(entries: list[dict[str, str]]) -> str:
    lines = [
        JOURNAL_START,
        "## 정책 활성 기록 (서버 자동 기록; 모델 편집 금지)",
    ]
    for entry in entries[-JOURNAL_MAX_ENTRIES:]:
        lines.append(
            "- activation={activation} at={at} fp={fp} note={note}".format(**entry)
        )
    lines.append(JOURNAL_END)
    return "\n".join(lines)


def record_policy_journal(
    memory_path: pathlib.Path,
    policy: Mapping[str, Any],
) -> dict[str, Any]:
    """Record one genuine activation without rewriting unmanaged memory.

    Duplicate activation IDs are no-ops.  If a pre-existing journal block is
    malformed, the function fails closed and returns a quarantine report
    instead of attempting an automatic repair.
    """
    activation_id = policy.get("activation_id")
    activated_at = policy.get("activated_at")
    if not isinstance(activation_id, str) or not activation_id:
        return {"status": "rejected", "reason": "activation-id-missing"}
    if not isinstance(activated_at, str) or not activated_at:
        return {"status": "rejected", "reason": "activated-at-missing"}
    try:
        old = memory_path.read_text(encoding="utf-8")
    except FileNotFoundError:
        old = ""
    except (OSError, UnicodeError) as exc:
        return {
            "status": "quarantined",
            "reason": f"memory-read-failed:{type(exc).__name__}",
        }

    report = analyze_memory_text(old)
    if report["issues"]:
        return {
            "status": "quarantined",
            "reason": "managed-journal-invalid",
            "report": report,
        }
    if activation_id in report["activation_ids"]:
        return {
            "status": "deduplicated",
            "activation_id": activation_id,
            "entry_count": report["entry_count"],
        }

    entries: list[dict[str, str]] = []
    if report["journal_start_count"] == 1:
        start = old.index(JOURNAL_START) + len(JOURNAL_START)
        end = old.index(JOURNAL_END, start)
        for line in old[start:end].splitlines():
            match = _ENTRY_RE.match(line.strip())
            if match:
                entries.append(match.groupdict())
    entries.append({
        "activation": activation_id[:96],
        "at": _one_line(activated_at, 40),
        "fp": _policy_fingerprint(policy),
        "note": _one_line(policy.get("note")),
    })
    block = _render_journal(entries)

    if report["journal_start_count"] == 1:
        start = old.index(JOURNAL_START)
        end = old.index(JOURNAL_END, start) + len(JOURNAL_END)
        new = old[:start] + block + old[end:]
    else:
        separator = "" if not old else ("\n" if old.endswith("\n") else "\n\n")
        new = old + separator + block + "\n"
    _atomic_text(memory_path, new)
    return {
        "status": "recorded",
        "activation_id": activation_id,
        "entry_count": min(len(entries), JOURNAL_MAX_ENTRIES),
        "trimmed": max(0, len(entries) - JOURNAL_MAX_ENTRIES),
    }
