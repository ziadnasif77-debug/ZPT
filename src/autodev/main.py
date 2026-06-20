"""CLI entry point — rich interface with phase indicators."""

from __future__ import annotations

import json
import sys
import uuid
from pathlib import Path

from rich.console import Console
from rich.panel import Panel
from rich.prompt import Confirm, Prompt
from rich.table import Table
from rich.text import Text

from langgraph.types import Command

from autodev.config import load_config
from autodev.context_manager import ContextManager
from autodev.graph import build_graph
from autodev.llm_client import LLMClient
from autodev.observability import ObservabilityLogger
from autodev.sandbox import Sandbox, SandboxUnavailable

console = Console()

PHASE_STYLE = {
    "architect":     ("bold cyan",    "[cyan]Architect[/]"),
    "approval_gate": ("bold yellow",  "[yellow]Approval[/]"),
    "developer":     ("bold green",   "[green]Developer[/]"),
    "tester":        ("bold magenta", "[magenta]Tester[/]"),
    "reviewer":      ("bold blue",    "[blue]Reviewer[/]"),
    "prepare_retry": ("bold red",     "[red]Retry[/]"),
    "done":          ("bold green",   "[green]Done[/]"),
    "failed":        ("bold red",     "[red]Failed[/]"),
}

PHASE_ICON = {
    "architect": "\U0001f9e0",
    "approval_gate": "⏸️ ",
    "developer": "\U0001f4bb",
    "tester": "\U0001f9ea",
    "reviewer": "\U0001f50d",
    "prepare_retry": "\U0001f504",
    "done": "✅",
    "failed": "❌",
}


def _render_node(node: str, data: dict, iteration: int) -> None:
    icon = PHASE_ICON.get(node, "•")
    _, label = PHASE_STYLE.get(node, ("", node))
    iter_tag = f" [dim](iteration {iteration})[/]" if iteration > 0 else ""

    if node == "architect":
        plan = data.get("plan")
        if plan:
            console.print(f"\n{icon} {label}{iter_tag}")
            console.print(Panel(
                _format_plan(plan),
                title="Proposed Plan",
                border_style="cyan",
            ))
    elif node == "developer":
        cb = data.get("code_bundle")
        if cb:
            files = cb.get("files", [])
            names = [f["path"] for f in files]
            console.print(f"\n{icon} {label}{iter_tag} — wrote {len(files)} file(s): {', '.join(names)}")
    elif node == "tester":
        tr = data.get("test_result")
        if tr:
            status = "[green]PASSED[/]" if tr.get("passed") else "[red]FAILED[/]"
            console.print(f"\n{icon} {label}{iter_tag} — {status} (exit code {tr.get('exit_code', '?')})")
            stderr = tr.get("stderr", "").strip()
            if stderr and not tr.get("passed"):
                console.print(Panel(stderr[:1000], title="stderr", border_style="red"))
    elif node == "reviewer":
        rv = data.get("review")
        if rv:
            verdict = "[green]APPROVED[/]" if rv.get("approved") else "[red]REJECTED[/]"
            console.print(f"\n{icon} {label}{iter_tag} — {verdict}")
            if rv.get("summary"):
                console.print(f"   {rv['summary']}")
            for c in rv.get("comments", []):
                sev = c.get("severity", "info")
                color = {"error": "red", "warning": "yellow"}.get(sev, "dim")
                console.print(f"   [{color}][{sev}][/{color}] {c.get('file_path','')}: {c.get('message','')}")
    elif node == "prepare_retry":
        console.print(f"\n{icon} {label}{iter_tag} — preparing feedback for developer...")
    elif node == "done":
        console.print(f"\n{icon} {label}")
    elif node == "failed":
        reason = data.get("stop_reason", "unknown reason")
        console.print(f"\n{icon} {label} — {reason}")


def _format_plan(plan: dict) -> str:
    lines = [f"[bold]Problem:[/] {plan.get('problem_description', 'N/A')}"]
    files = plan.get("files_needed", [])
    if files:
        lines.append(f"[bold]Files:[/] {', '.join(files)}")
    deps = plan.get("dependencies", [])
    if deps:
        lines.append(f"[bold]Dependencies:[/] {', '.join(deps)}")
    tasks = plan.get("tasks", [])
    if tasks:
        lines.append("[bold]Tasks:[/]")
        for i, t in enumerate(tasks, 1):
            lines.append(f"  {i}. {t.get('description', '')} ({t.get('file_path', '')})")
    criteria = plan.get("acceptance_criteria", [])
    if criteria:
        lines.append("[bold]Acceptance Criteria:[/]")
        for c in criteria:
            lines.append(f"  • {c}")
    return "\n".join(lines)


def _render_final(state: dict, workspace: Path, session_id: str) -> None:
    console.print()
    status = state.get("final_status", "unknown")
    if status == "success":
        console.print(Panel(
            _build_success_summary(state, workspace, session_id),
            title="✅ AutoDev Complete",
            border_style="green",
        ))
    else:
        console.print(Panel(
            _build_failure_summary(state, session_id),
            title="❌ AutoDev Stopped",
            border_style="red",
        ))


def _build_success_summary(state: dict, workspace: Path, session_id: str) -> str:
    lines = ["[bold green]Code generated and approved![/]\n"]
    cb = state.get("code_bundle") or {}
    files = cb.get("files", [])
    if files:
        lines.append("[bold]Files created:[/]")
        for f in files:
            fp = workspace / f["path"]
            size = fp.stat().st_size if fp.exists() else 0
            lines.append(f"  • {f['path']} ({size} bytes)")
    lines.append(f"\n[bold]Workspace:[/] {workspace.resolve()}")
    iters = state.get("iteration", 0)
    if iters > 0:
        lines.append(f"[bold]Iterations:[/] {iters}")
    lines.append(f"[bold]Session:[/] {session_id}")
    return "\n".join(lines)


def _build_failure_summary(state: dict, session_id: str) -> str:
    lines = [f"[bold red]Reason:[/] {state.get('stop_reason', 'unknown')}\n"]
    iters = state.get("iteration", 0)
    lines.append(f"[bold]Iterations completed:[/] {iters}")
    tr = state.get("test_result") or {}
    if tr.get("stderr"):
        lines.append(f"\n[bold]Last error:[/]\n{tr['stderr'][:500]}")
    lines.append(f"\n[bold]Session:[/] {session_id}")
    return "\n".join(lines)


def main() -> None:
    import argparse

    parser = argparse.ArgumentParser(
        description="AutoDev — Local AI development team",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("request", nargs="?", help="Development request in natural language")
    parser.add_argument("-c", "--config", default="config.yaml", help="Path to config.yaml")
    parser.add_argument("-w", "--workspace", default=None, help="Workspace directory")
    args = parser.parse_args()

    config = load_config(args.config)
    workspace = Path(args.workspace or config.workspace_dir)
    workspace.mkdir(parents=True, exist_ok=True)

    request = args.request
    if not request:
        request = Prompt.ask("[bold]What would you like to build?[/]")
    if not request.strip():
        console.print("[red]No request provided. Exiting.[/]")
        sys.exit(1)

    console.print(Panel(
        f"[bold]{request}[/]",
        title="\U0001f680 AutoDev",
        subtitle=f"workspace: {workspace.resolve()}",
        border_style="bold white",
    ))

    logger = ObservabilityLogger(config.observability.log_dir)
    llm = LLMClient(config, logger)
    context_mgr = ContextManager(config, llm)

    sandbox = None
    try:
        sandbox = Sandbox(config.sandbox)
        console.print("[green]Docker sandbox ready[/]")
    except SandboxUnavailable as e:
        console.print(f"[yellow]Warning: {e}[/]")
        console.print("[yellow]Code will NOT be executed in sandbox — test results will be synthetic.[/]")

    compiled = build_graph(config, llm, sandbox, context_mgr)

    initial_state: dict = {
        "user_request": request,
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

    thread_id = str(uuid.uuid4())
    thread_config = {"configurable": {"thread_id": thread_id}}

    try:
        _run_graph(compiled, initial_state, thread_config, workspace, logger.session_id)
    except KeyboardInterrupt:
        console.print("\n[yellow]Interrupted. Session saved — can be resumed.[/]")
        sys.exit(130)


def _run_graph(
    compiled: any,
    initial_state: dict,
    thread_config: dict,
    workspace: Path,
    session_id: str,
) -> None:
    current_input: dict | None = initial_state
    iteration = 0

    while True:
        events = list(compiled.stream(
            current_input,
            thread_config,
            stream_mode="updates",
        ))

        for event in events:
            for node_name, node_data in event.items():
                if node_name == "__interrupt__":
                    continue
                iter_val = node_data.get("iteration", iteration)
                _render_node(node_name, node_data, iter_val)

                if node_name == "prepare_retry":
                    iteration = node_data.get("iteration", iteration)

        snapshot = compiled.get_state(thread_config)

        if not snapshot.next:
            _render_final(snapshot.values, workspace, session_id)
            return

        if snapshot.next == ("approval_gate",):
            interrupts = snapshot.tasks
            if interrupts:
                plan_data = None
                for task in interrupts:
                    for intr in getattr(task, "interrupts", []):
                        val = getattr(intr, "value", None)
                        if isinstance(val, dict) and val.get("type") == "plan_approval":
                            plan_data = val.get("plan")

                if plan_data:
                    approved = Confirm.ask("\n[bold yellow]Approve this plan?[/]")
                    resume_val = "yes" if approved else "no"
                else:
                    resume_val = "yes"

                current_input = Command(resume=resume_val)
                continue

        _render_final(snapshot.values, workspace, session_id)
        return


if __name__ == "__main__":
    main()
