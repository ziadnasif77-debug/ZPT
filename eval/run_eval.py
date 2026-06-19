#!/usr/bin/env python3
"""Evaluation runner — benchmarks the current model with numeric scoring.

Usage:
    python eval/run_eval.py [--config config.yaml] [--tasks eval/tasks] [--output eval/results]
"""

from __future__ import annotations

import argparse
import json
import shutil
import sys
import tempfile
import uuid
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import yaml
from rich.console import Console
from rich.panel import Panel
from rich.table import Table

from autodev.config import load_config
from autodev.context_manager import ContextManager
from autodev.graph import build_graph
from autodev.llm_client import LLMClient
from autodev.observability import ObservabilityLogger
from autodev.sandbox import Sandbox, SandboxUnavailable

console = Console()


def load_tasks(tasks_dir: Path) -> list[dict]:
    tasks = []
    for f in sorted(tasks_dir.glob("*.yaml")):
        with f.open() as fh:
            task = yaml.safe_load(fh)
            task["_file"] = f.name
            tasks.append(task)
    return tasks


def run_single_task(
    task: dict,
    compiled,
    config,
    timeout_per_task: int = 300,
) -> dict:
    """Run one eval task through the full pipeline. Returns a result dict."""
    workspace = Path(tempfile.mkdtemp(prefix=f"autodev_eval_{task['name']}_"))

    initial_state = {
        "user_request": task["description"],
        "workspace_path": str(workspace),
        "plan": None,
        "plan_approved": False,
        "code_bundle": None,
        "test_result": None,
        "review": None,
        "iteration": 0,
        "attempt_history": [],
        "error_hashes": [],
        "final_status": "",
        "stop_reason": "",
        "feedback": "",
    }

    thread_id = f"eval-{task['name']}-{uuid.uuid4().hex[:8]}"
    thread_config = {"configurable": {"thread_id": thread_id}}

    result = {
        "task": task["name"],
        "difficulty": task.get("difficulty", "unknown"),
        "passed": False,
        "final_status": "error",
        "iterations": 0,
        "stop_reason": "",
        "files_created": [],
        "error": None,
    }

    try:
        for event in compiled.stream(initial_state, thread_config, stream_mode="updates"):
            for node_name, node_data in event.items():
                if node_name == "__interrupt__":
                    continue

        snapshot = compiled.get_state(thread_config)

        if snapshot.next:
            from langgraph.types import Command
            for event in compiled.stream(
                Command(resume="yes"), thread_config, stream_mode="updates"
            ):
                pass
            snapshot = compiled.get_state(thread_config)

        state = snapshot.values
        result["final_status"] = state.get("final_status", "unknown")
        result["iterations"] = state.get("iteration", 0)
        result["stop_reason"] = state.get("stop_reason", "")

        cb = state.get("code_bundle") or {}
        result["files_created"] = [f["path"] for f in cb.get("files", [])]

        review = state.get("review") or {}
        test_res = state.get("test_result") or {}

        result["passed"] = (
            state.get("final_status") == "success"
            and review.get("approved", False)
            and test_res.get("passed", False)
        )

    except Exception as exc:
        result["error"] = f"{type(exc).__name__}: {exc}"
    finally:
        shutil.rmtree(workspace, ignore_errors=True)

    return result


def score_results(results: list[dict]) -> dict:
    total = len(results)
    passed = sum(1 for r in results if r["passed"])

    by_difficulty = {}
    for r in results:
        d = r.get("difficulty", "unknown")
        by_difficulty.setdefault(d, {"total": 0, "passed": 0})
        by_difficulty[d]["total"] += 1
        if r["passed"]:
            by_difficulty[d]["passed"] += 1

    return {
        "total": total,
        "passed": passed,
        "score": f"{passed}/{total}",
        "percentage": round(passed / total * 100, 1) if total else 0,
        "by_difficulty": by_difficulty,
    }


def display_results(results: list[dict], score: dict, model: str) -> None:
    table = Table(title=f"Eval Results — {model}")
    table.add_column("#", style="dim", width=3)
    table.add_column("Task", style="bold")
    table.add_column("Difficulty")
    table.add_column("Status", justify="center")
    table.add_column("Iterations", justify="center")
    table.add_column("Notes")

    for i, r in enumerate(results, 1):
        status = "[green]PASS[/]" if r["passed"] else "[red]FAIL[/]"
        notes = r.get("stop_reason", "") or r.get("error", "") or ""
        notes = notes[:60]
        diff_color = {"trivial": "dim", "easy": "green", "medium": "yellow", "hard": "red"}.get(
            r.get("difficulty", ""), ""
        )
        diff_text = f"[{diff_color}]{r.get('difficulty', '?')}[/{diff_color}]" if diff_color else r.get("difficulty", "?")
        table.add_row(str(i), r["task"], diff_text, status, str(r["iterations"]), notes)

    console.print(table)
    console.print()

    pct = score["percentage"]
    color = "green" if pct >= 70 else "yellow" if pct >= 40 else "red"
    console.print(Panel(
        f"[bold {color}]{score['score']} tasks passed ({pct}%)[/]\n"
        f"Model: {model}\n"
        + "\n".join(f"  {d}: {v['passed']}/{v['total']}" for d, v in score["by_difficulty"].items()),
        title="Score",
        border_style=color,
    ))


def save_results(
    results: list[dict],
    score: dict,
    model: str,
    output_dir: Path,
) -> Path:
    output_dir.mkdir(parents=True, exist_ok=True)
    ts = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    model_safe = model.replace("/", "_").replace(":", "_")
    filename = f"eval_{model_safe}_{ts}.json"
    filepath = output_dir / filename

    report = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "model": model,
        "score": score,
        "results": results,
    }
    filepath.write_text(json.dumps(report, indent=2, ensure_ascii=False))
    return filepath


def main() -> None:
    parser = argparse.ArgumentParser(description="AutoDev evaluation runner")
    parser.add_argument("-c", "--config", default="config.yaml")
    parser.add_argument("-t", "--tasks", default="eval/tasks")
    parser.add_argument("-o", "--output", default="eval/results")
    args = parser.parse_args()

    config = load_config(args.config)
    model = config.models.default

    console.print(Panel(f"[bold]Evaluating model:[/] {model}", title="AutoDev Eval", border_style="cyan"))

    tasks = load_tasks(Path(args.tasks))
    if not tasks:
        console.print("[red]No eval tasks found.[/]")
        sys.exit(1)
    console.print(f"Found {len(tasks)} eval tasks\n")

    config.human_in_the_loop.approve_plan = False

    logger = ObservabilityLogger(config.observability.log_dir, session_id=f"eval_{datetime.now(timezone.utc).strftime('%Y%m%d_%H%M%S')}")
    llm = LLMClient(config, logger)
    ctx = ContextManager(config, llm)

    sandbox = None
    try:
        sandbox = Sandbox(config.sandbox)
        console.print("[green]Docker sandbox ready[/]\n")
    except SandboxUnavailable as e:
        console.print(f"[yellow]Warning: {e}[/]")
        console.print("[yellow]Eval will run without sandbox — results may differ.[/]\n")

    compiled = build_graph(config, llm, sandbox, ctx)

    results = []
    for i, task in enumerate(tasks, 1):
        console.print(f"[bold][{i}/{len(tasks)}][/] Running: {task['name']} ({task.get('difficulty', '?')})...", end=" ")
        result = run_single_task(task, compiled, config)
        results.append(result)
        status = "[green]PASS[/]" if result["passed"] else "[red]FAIL[/]"
        console.print(status)

    score = score_results(results)
    display_results(results, score, model)

    filepath = save_results(results, score, model, Path(args.output))
    console.print(f"\nResults saved to: {filepath}")


if __name__ == "__main__":
    main()
