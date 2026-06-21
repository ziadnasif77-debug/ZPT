"""Persistent memory — remembers successful solutions and lessons across sessions.

Uses ChromaDB as a local vector store with sentence-transformers embeddings.
No internet needed — everything runs locally.

Two collections:
1. solutions — complete successful pipeline runs (task, plan, code, iterations)
2. lessons — error patterns and their fixes (what went wrong, what fixed it)
"""

from __future__ import annotations

import hashlib
import json
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any


@dataclass
class MemoryConfig:
    enabled: bool = True
    db_path: str = "./memory_db"
    embedding_model: str = "all-MiniLM-L6-v2"
    top_k: int = 3
    min_similarity: float = 0.7
    save_lessons: bool = True


@dataclass
class SolutionRecord:
    task: str
    product_spec: dict | None
    plan: dict | None
    files: list[dict]
    iterations_needed: int
    healer_fixes: list[str]
    timestamp: str
    model_used: str = ""


@dataclass
class LessonRecord:
    error_type: str
    what_went_wrong: str
    what_fixed_it: str
    avoid_this: str
    task_context: str = ""
    timestamp: str = ""


@dataclass
class MemorySearchResult:
    task: str
    plan: dict | None
    files: list[dict]
    iterations_needed: int
    similarity: float
    timestamp: str


@dataclass
class LessonSearchResult:
    error_type: str
    what_went_wrong: str
    what_fixed_it: str
    avoid_this: str
    similarity: float


class Memory:
    """Persistent memory using ChromaDB for vector search."""

    def __init__(self, config: MemoryConfig | None = None):
        self._config = config or MemoryConfig()
        self._client = None
        self._solutions = None
        self._lessons = None
        self._available = False

        if not self._config.enabled:
            return

        try:
            import chromadb
            from chromadb.config import Settings

            db_path = Path(self._config.db_path)
            db_path.mkdir(parents=True, exist_ok=True)

            self._client = chromadb.PersistentClient(
                path=str(db_path),
                settings=Settings(anonymized_telemetry=False),
            )

            ef = self._get_embedding_function()

            self._solutions = self._client.get_or_create_collection(
                name="solutions",
                embedding_function=ef,
                metadata={"hnsw:space": "cosine"},
            )
            self._lessons = self._client.get_or_create_collection(
                name="lessons",
                embedding_function=ef,
                metadata={"hnsw:space": "cosine"},
            )

            self._available = True
            print(f"[MEMORY] Initialized with {self._solutions.count()} solutions, "
                  f"{self._lessons.count()} lessons", flush=True)

        except ImportError:
            print("[MEMORY] ChromaDB not installed — memory disabled. "
                  "Install with: pip install chromadb sentence-transformers", flush=True)
        except Exception as e:
            print(f"[MEMORY] Failed to initialize: {e}", flush=True)

    def _get_embedding_function(self):
        """Get the sentence-transformers embedding function for ChromaDB."""
        try:
            from chromadb.utils.embedding_functions import SentenceTransformerEmbeddingFunction
            return SentenceTransformerEmbeddingFunction(
                model_name=self._config.embedding_model
            )
        except ImportError:
            from chromadb.utils.embedding_functions import DefaultEmbeddingFunction
            print("[MEMORY] sentence-transformers not found, using default embeddings", flush=True)
            return DefaultEmbeddingFunction()

    @property
    def is_available(self) -> bool:
        return self._available

    # ── WRITE ─────────────────────────────────────────────────

    def save_solution(self, record: SolutionRecord) -> None:
        """Save a successful solution to memory."""
        if not self._available or not self._solutions:
            return

        doc_id = _stable_id(record.task)

        metadata = {
            "iterations_needed": record.iterations_needed,
            "timestamp": record.timestamp,
            "model_used": record.model_used,
            "num_files": len(record.files),
            "healer_fixes": json.dumps(record.healer_fixes),
        }

        doc_text = _solution_to_text(record)

        full_data = json.dumps({
            "task": record.task,
            "product_spec": record.product_spec,
            "plan": record.plan,
            "files": record.files,
            "iterations_needed": record.iterations_needed,
            "healer_fixes": record.healer_fixes,
            "model_used": record.model_used,
        })

        try:
            self._solutions.upsert(
                ids=[doc_id],
                documents=[doc_text],
                metadatas=[metadata],
            )
            _save_solution_data(self._config.db_path, doc_id, full_data)
            print(f"[MEMORY] Saved solution: {record.task[:80]}", flush=True)
        except Exception as e:
            print(f"[MEMORY] Failed to save solution: {e}", flush=True)

    def save_lesson(self, record: LessonRecord) -> None:
        """Save a lesson learned from a failed attempt."""
        if not self._available or not self._lessons or not self._config.save_lessons:
            return

        record.timestamp = record.timestamp or datetime.now().isoformat()
        doc_id = _stable_id(f"{record.error_type}:{record.what_went_wrong}:{record.avoid_this}")

        doc_text = (
            f"Error: {record.error_type}. "
            f"Problem: {record.what_went_wrong}. "
            f"Fix: {record.what_fixed_it}. "
            f"Avoid: {record.avoid_this}"
        )

        metadata = {
            "error_type": record.error_type,
            "timestamp": record.timestamp,
            "task_context": record.task_context[:500] if record.task_context else "",
        }

        try:
            self._lessons.upsert(
                ids=[doc_id],
                documents=[doc_text],
                metadatas=[metadata],
            )
            print(f"[MEMORY] Saved lesson: {record.error_type} — {record.avoid_this[:80]}", flush=True)
        except Exception as e:
            print(f"[MEMORY] Failed to save lesson: {e}", flush=True)

    # ── READ ──────────────────────────────────────────────────

    def recall_solutions(self, query: str) -> list[MemorySearchResult]:
        """Search for similar past successful solutions."""
        if not self._available or not self._solutions:
            return []

        if self._solutions.count() == 0:
            return []

        try:
            results = self._solutions.query(
                query_texts=[query],
                n_results=min(self._config.top_k, self._solutions.count()),
                include=["documents", "metadatas", "distances"],
            )

            matches: list[MemorySearchResult] = []
            if not results or not results["ids"] or not results["ids"][0]:
                return []

            for i, doc_id in enumerate(results["ids"][0]):
                distance = results["distances"][0][i] if results["distances"] else 1.0
                similarity = 1.0 - distance

                if similarity < self._config.min_similarity:
                    continue

                meta = results["metadatas"][0][i] if results["metadatas"] else {}

                full_data = _load_solution_data(self._config.db_path, doc_id)
                plan = None
                files: list[dict] = []
                task = query

                if full_data:
                    parsed = json.loads(full_data)
                    plan = parsed.get("plan")
                    files = parsed.get("files", [])
                    task = parsed.get("task", query)

                matches.append(MemorySearchResult(
                    task=task,
                    plan=plan,
                    files=files,
                    iterations_needed=meta.get("iterations_needed", 0),
                    similarity=round(similarity, 3),
                    timestamp=meta.get("timestamp", ""),
                ))

            return matches

        except Exception as e:
            print(f"[MEMORY] Recall failed: {e}", flush=True)
            return []

    def recall_lessons(self, query: str) -> list[LessonSearchResult]:
        """Search for relevant lessons from past failures."""
        if not self._available or not self._lessons:
            return []

        if self._lessons.count() == 0:
            return []

        try:
            results = self._lessons.query(
                query_texts=[query],
                n_results=min(self._config.top_k * 2, self._lessons.count()),
                include=["documents", "metadatas", "distances"],
            )

            matches: list[LessonSearchResult] = []
            if not results or not results["ids"] or not results["ids"][0]:
                return []

            for i, doc_id in enumerate(results["ids"][0]):
                distance = results["distances"][0][i] if results["distances"] else 1.0
                similarity = 1.0 - distance

                if similarity < self._config.min_similarity:
                    continue

                doc = results["documents"][0][i] if results["documents"] else ""
                meta = results["metadatas"][0][i] if results["metadatas"] else {}

                parts = _parse_lesson_doc(doc)

                matches.append(LessonSearchResult(
                    error_type=meta.get("error_type", parts.get("error_type", "")),
                    what_went_wrong=parts.get("what_went_wrong", ""),
                    what_fixed_it=parts.get("what_fixed_it", ""),
                    avoid_this=parts.get("avoid_this", ""),
                    similarity=round(similarity, 3),
                ))

            return matches[:self._config.top_k]

        except Exception as e:
            print(f"[MEMORY] Lesson recall failed: {e}", flush=True)
            return []

    # ── STATS ─────────────────────────────────────────────────

    def get_stats(self) -> dict[str, Any]:
        """Return memory statistics for the UI."""
        if not self._available:
            return {
                "available": False,
                "total_solutions": 0,
                "total_lessons": 0,
                "success_rate": 0,
            }

        total_solutions = self._solutions.count() if self._solutions else 0
        total_lessons = self._lessons.count() if self._lessons else 0

        avg_iterations = 0.0
        if total_solutions > 0 and self._solutions:
            try:
                all_meta = self._solutions.get(include=["metadatas"])
                iters = [m.get("iterations_needed", 0) for m in (all_meta["metadatas"] or [])]
                avg_iterations = sum(iters) / len(iters) if iters else 0
            except Exception:
                pass

        total_attempts = total_solutions + total_lessons
        success_rate = round(total_solutions / total_attempts * 100, 1) if total_attempts > 0 else 0

        return {
            "available": True,
            "total_solutions": total_solutions,
            "total_lessons": total_lessons,
            "success_rate": success_rate,
            "avg_iterations": round(avg_iterations, 1),
        }

    def reset(self) -> None:
        """Clear all memory. Use with caution."""
        if not self._available or not self._client:
            return
        try:
            self._client.delete_collection("solutions")
            self._client.delete_collection("lessons")
            ef = self._get_embedding_function()
            self._solutions = self._client.get_or_create_collection(
                name="solutions", embedding_function=ef,
                metadata={"hnsw:space": "cosine"},
            )
            self._lessons = self._client.get_or_create_collection(
                name="lessons", embedding_function=ef,
                metadata={"hnsw:space": "cosine"},
            )
            print("[MEMORY] All memory cleared", flush=True)
        except Exception as e:
            print(f"[MEMORY] Reset failed: {e}", flush=True)


# ── Helpers ───────────────────────────────────────────────────

def _stable_id(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()[:16]


def _solution_to_text(record: SolutionRecord) -> str:
    """Convert a solution record to searchable text."""
    parts = [record.task]
    if record.product_spec:
        parts.append(record.product_spec.get("scope", ""))
    if record.plan:
        parts.append(record.plan.get("problem_description", ""))
        for t in record.plan.get("tasks", []):
            parts.append(t.get("description", ""))
    return " ".join(parts)


def _save_solution_data(db_path: str, doc_id: str, data: str) -> None:
    """Save full solution data to a JSON file alongside the vector DB."""
    data_dir = Path(db_path) / "solution_data"
    data_dir.mkdir(parents=True, exist_ok=True)
    (data_dir / f"{doc_id}.json").write_text(data, encoding="utf-8")


def _load_solution_data(db_path: str, doc_id: str) -> str | None:
    """Load full solution data from JSON file."""
    path = Path(db_path) / "solution_data" / f"{doc_id}.json"
    if path.exists():
        return path.read_text(encoding="utf-8")
    return None


def _parse_lesson_doc(doc: str) -> dict[str, str]:
    """Parse a lesson document text back into components."""
    result: dict[str, str] = {}
    import re
    for key, pattern in [
        ("error_type", r"Error:\s*(.+?)\."),
        ("what_went_wrong", r"Problem:\s*(.+?)\."),
        ("what_fixed_it", r"Fix:\s*(.+?)\."),
        ("avoid_this", r"Avoid:\s*(.+?)$"),
    ]:
        m = re.search(pattern, doc)
        if m:
            result[key] = m.group(1).strip()
    return result


def format_memory_context(
    solutions: list[MemorySearchResult],
    lessons: list[LessonSearchResult],
) -> str:
    """Format recalled memories into context for the pipeline."""
    parts: list[str] = []

    if solutions:
        parts.append("## Past Similar Solutions (from memory)")
        for i, sol in enumerate(solutions, 1):
            parts.append(
                f"\n### Solution {i} (similarity: {sol.similarity:.0%}, "
                f"took {sol.iterations_needed} iteration(s))"
            )
            parts.append(f"Task: {sol.task}")
            if sol.plan:
                tasks_desc = [t.get("description", "") for t in sol.plan.get("tasks", [])]
                if tasks_desc:
                    parts.append(f"Plan tasks: {'; '.join(tasks_desc)}")
            if sol.files:
                for f in sol.files[:3]:
                    parts.append(f"File: {f.get('path', '?')}")
                    content = f.get("content", "")
                    if len(content) > 500:
                        content = content[:500] + "\n... (truncated)"
                    parts.append(f"```python\n{content}\n```")

    if lessons:
        parts.append("\n## Lessons from Past Mistakes (AVOID these)")
        for lesson in lessons:
            parts.append(
                f"- **{lesson.error_type}**: {lesson.what_went_wrong}\n"
                f"  Fix: {lesson.what_fixed_it}\n"
                f"  ⚠️ AVOID: {lesson.avoid_this}"
            )

    return "\n".join(parts)
