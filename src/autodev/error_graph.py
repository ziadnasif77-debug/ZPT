"""Error graph — tracks errors as a connected graph with escalation policy.

Instead of a flat list of error hashes, this builds a graph where each error
node records its signature, file, root cause, and fix attempts. Edges connect
errors by causation (fix A caused error B, or error B reintroduced error A).

Escalation policy:
  1st occurrence → fix normally
  2nd occurrence → change approach
  3rd occurrence → isolate the failing module
  4th occurrence → rollback to last stable commit
  5th occurrence → stop and escalate to user
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal


@dataclass
class ErrorNode:
    signature: str
    error_type: str
    file: str
    root_cause: str
    occurrences: int = 0
    fix_attempts: list[str] = field(default_factory=list)
    iteration_first_seen: int = 0
    iteration_last_seen: int = 0


@dataclass
class ErrorEdge:
    source: str  # error signature
    target: str  # error signature
    relation: Literal["caused_by", "fixed_by", "reintroduced_by"]
    iteration: int = 0


EscalationAction = Literal["fix", "change_approach", "isolate", "rollback", "escalate"]


class ErrorGraph:
    def __init__(self, persist_path: str | None = None):
        self._nodes: dict[str, ErrorNode] = {}
        self._edges: list[ErrorEdge] = []
        self._persist_path = persist_path
        self._previous_error: str | None = None
        if persist_path:
            self._load()

    def record_error(
        self,
        error_type: str,
        file: str,
        root_cause: str,
        iteration: int,
        stderr: str = "",
    ) -> ErrorNode:
        """Record a new error occurrence. Returns the ErrorNode."""
        sig = _error_signature(error_type, file, root_cause)

        if sig in self._nodes:
            node = self._nodes[sig]
            node.occurrences += 1
            node.iteration_last_seen = iteration
        else:
            node = ErrorNode(
                signature=sig,
                error_type=error_type,
                file=file,
                root_cause=root_cause[:500],
                occurrences=1,
                iteration_first_seen=iteration,
                iteration_last_seen=iteration,
            )
            self._nodes[sig] = node

        if self._previous_error and self._previous_error != sig:
            prev_node = self._nodes.get(self._previous_error)
            if prev_node:
                self._edges.append(ErrorEdge(
                    source=self._previous_error,
                    target=sig,
                    relation="caused_by",
                    iteration=iteration,
                ))

        self._previous_error = sig
        self._save()
        return node

    def record_fix(self, error_sig: str, fix_description: str, iteration: int) -> None:
        """Record that an error was fixed."""
        if error_sig in self._nodes:
            self._nodes[error_sig].fix_attempts.append(fix_description)
        self._save()

    def get_escalation(self, error_sig: str) -> EscalationAction:
        """Determine what action to take based on error occurrence count."""
        node = self._nodes.get(error_sig)
        if not node:
            return "fix"

        count = node.occurrences
        if count <= 1:
            return "fix"
        elif count == 2:
            return "change_approach"
        elif count == 3:
            return "isolate"
        elif count == 4:
            return "rollback"
        else:
            return "escalate"

    def get_error_context(self, error_sig: str) -> str:
        """Build context string about an error for the developer prompt."""
        node = self._nodes.get(error_sig)
        if not node:
            return ""

        action = self.get_escalation(error_sig)
        parts = [
            f"ERROR HISTORY: This error has occurred {node.occurrences} time(s).",
            f"Error type: {node.error_type} in {node.file}",
            f"Root cause: {node.root_cause}",
        ]

        if node.fix_attempts:
            parts.append(f"Previous fix attempts: {'; '.join(node.fix_attempts[-3:])}")

        if action == "change_approach":
            parts.append(
                "ESCALATION: This error has occurred TWICE. You MUST use a "
                "completely DIFFERENT approach to fix it. Do NOT repeat the same fix."
            )
        elif action == "isolate":
            parts.append(
                "ESCALATION: This error has occurred 3 TIMES. Isolate the failing "
                "module — simplify or rewrite the problematic section entirely."
            )
        elif action == "rollback":
            parts.append(
                "ESCALATION: This error has occurred 4 TIMES. The system will "
                "ROLLBACK to the last stable version."
            )
        elif action == "escalate":
            parts.append(
                "ESCALATION: This error has occurred 5+ TIMES. Stopping for "
                "human intervention."
            )

        return "\n".join(parts)

    def get_stats(self) -> dict:
        """Return summary statistics for the UI."""
        return {
            "total_errors": len(self._nodes),
            "total_occurrences": sum(n.occurrences for n in self._nodes.values()),
            "most_common": sorted(
                [{"sig": n.signature[:16], "type": n.error_type, "file": n.file,
                  "count": n.occurrences}
                 for n in self._nodes.values()],
                key=lambda x: x["count"],
                reverse=True,
            )[:5],
        }

    def reset(self) -> None:
        """Clear all error history."""
        self._nodes.clear()
        self._edges.clear()
        self._previous_error = None
        self._save()

    def _save(self) -> None:
        if not self._persist_path:
            return
        try:
            path = Path(self._persist_path)
            path.parent.mkdir(parents=True, exist_ok=True)
            data = {
                "nodes": {
                    sig: {
                        "signature": n.signature,
                        "error_type": n.error_type,
                        "file": n.file,
                        "root_cause": n.root_cause,
                        "occurrences": n.occurrences,
                        "fix_attempts": n.fix_attempts,
                        "iteration_first_seen": n.iteration_first_seen,
                        "iteration_last_seen": n.iteration_last_seen,
                    }
                    for sig, n in self._nodes.items()
                },
                "edges": [
                    {"source": e.source, "target": e.target,
                     "relation": e.relation, "iteration": e.iteration}
                    for e in self._edges
                ],
            }
            path.write_text(json.dumps(data, indent=2), encoding="utf-8")
        except Exception as e:
            print(f"[ERROR-GRAPH] Failed to save: {e}", flush=True)

    def _load(self) -> None:
        if not self._persist_path:
            return
        path = Path(self._persist_path)
        if not path.exists():
            return
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            for sig, nd in data.get("nodes", {}).items():
                self._nodes[sig] = ErrorNode(**nd)
            for ed in data.get("edges", []):
                self._edges.append(ErrorEdge(**ed))
        except Exception as e:
            print(f"[ERROR-GRAPH] Failed to load: {e}", flush=True)


def _error_signature(error_type: str, file: str, root_cause: str) -> str:
    """Create a stable signature for an error."""
    key = f"{error_type}:{file}:{root_cause[:200]}"
    return hashlib.sha256(key.encode()).hexdigest()[:16]
