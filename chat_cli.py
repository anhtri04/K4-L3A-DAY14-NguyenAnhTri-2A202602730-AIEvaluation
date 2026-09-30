"""Interactive one-turn Q/A CLI for the OrbitTech domain assistant.

Loads the real RAG system under evaluation (``domain_assistant``) and scores
each turn with the lab's evaluation core (``template``). History is persisted
to ``artifacts/chat_history.json``.

Usage:
    python chat_cli.py [--top-k 5] [--history artifacts/chat_history.json]

Commands (type at the Question prompt):
    /stats --all              aggregate metrics over every stored turn
    /stats --session [ID]     aggregate metrics for a session (default: current)
    /history                  list stored Q/A turns
    /help                     show available commands
    /quit                     exit
"""

from __future__ import annotations

import argparse
import json
import time
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from rich.console import Console
from rich.markdown import Markdown
from rich.panel import Panel
from rich.prompt import Prompt
from rich.rule import Rule
from rich.table import Table
from rich.text import Text

from domain_assistant import DomainAssistant
from template import EvalResult, QAPair, RAGASEvaluator

DEFAULT_CORPUS_DIR = Path("data/technology_store")
DEFAULT_HISTORY_PATH = Path("artifacts/chat_history.json")
SCHEMA_VERSION = "1.0"
METRIC_KEYS = (
    "context_recall",
    "context_precision",
    "faithfulness",
    "relevance",
    "completeness",
    "overall",
)
METRIC_LABELS = {
    "context_recall": "Context Recall",
    "context_precision": "Context Precision",
    "faithfulness": "Faithfulness",
    "relevance": "Relevance",
    "completeness": "Completeness",
    "overall": "Overall",
}


# ---------------------------------------------------------------------------
# Persistence
# ---------------------------------------------------------------------------

def _empty_history() -> dict[str, Any]:
    return {"schema_version": SCHEMA_VERSION, "sessions": [], "entries": []}


def load_history(path: Path) -> dict[str, Any]:
    if not path.exists():
        return _empty_history()
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return _empty_history()
    if not isinstance(data, dict):
        return _empty_history()
    data.setdefault("sessions", [])
    data.setdefault("entries", [])
    data.setdefault("schema_version", SCHEMA_VERSION)
    return data


def save_history(path: Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )


# ---------------------------------------------------------------------------
# Metric helpers
# ---------------------------------------------------------------------------

def _style(score: float | None) -> str:
    if score is None:
        return "dim"
    if score >= 0.8:
        return "green"
    if score >= 0.6:
        return "yellow"
    return "red"


def _bar(score: float | None) -> str:
    if score is None:
        return ""
    return "█" * max(0, min(20, round(score * 20)))


def _no_expected_result(
    evaluator: RAGASEvaluator, question: str, answer: str, context: str
) -> EvalResult:
    faithfulness = evaluator.evaluate_faithfulness(answer, context)
    relevance = evaluator.evaluate_relevance(answer, question)
    passed = faithfulness >= 0.5 and relevance >= 0.5
    failure_type: str | None = None
    if not passed:
        if faithfulness < 0.3:
            failure_type = "hallucination"
        elif relevance < 0.3:
            failure_type = "irrelevant"
        else:
            failure_type = "off_topic"
    return EvalResult(
        qa_pair=QAPair(question=question, expected_answer="", context=context),
        actual_answer=answer,
        faithfulness=faithfulness,
        relevance=relevance,
        completeness=0.0,
        passed=passed,
        failure_type=failure_type,
    )


def evaluate_turn(
    evaluator: RAGASEvaluator,
    question: str,
    expected: str,
    answer: str,
    retrieved_texts: list[str],
) -> tuple[dict[str, float | None], bool, str | None]:
    context = "\n\n".join(retrieved_texts)
    expected = expected.strip()

    if expected:
        result = evaluator.run_full_eval(
            answer=answer,
            question=question,
            context=context,
            expected=expected,
            contexts=retrieved_texts,
        )
        metrics: dict[str, float | None] = {
            "context_recall": result.context_recall,
            "context_precision": result.context_precision,
            "faithfulness": result.faithfulness,
            "relevance": result.relevance,
            "completeness": result.completeness,
            "overall": result.overall_score(),
        }
        return metrics, result.passed, result.failure_type

    result = _no_expected_result(evaluator, question, answer, context)
    metrics = {
        "context_recall": None,
        "context_precision": None,
        "faithfulness": result.faithfulness,
        "relevance": result.relevance,
        "completeness": None,
        "overall": (result.faithfulness + result.relevance) / 2.0,
    }
    return metrics, result.passed, result.failure_type


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------

def render_turn(
    console: Console,
    question: str,
    expected: str,
    record: dict[str, Any],
) -> None:
    console.print()
    console.print(Panel(Text(question), title="Question", border_style="cyan"))
    console.print(
        Panel(Markdown(record["actual_answer"]), title="Answer", border_style="green")
    )
    if expected:
        console.print(
            Panel(
                Text(expected, style="italic"),
                title="Expected answer",
                border_style="magenta",
                title_align="left",
            )
        )

    contexts = record["retrieved_contexts"]
    context_table = Table(
        title=f"Retrieved contexts ({len(contexts)})", show_lines=False, expand=True
    )
    context_table.add_column("#", justify="right", style="dim", no_wrap=True)
    context_table.add_column("Source", style="cyan", no_wrap=True)
    context_table.add_column("Chunk", style="dim", no_wrap=True)
    context_table.add_column("Score", justify="right", style="blue", no_wrap=True)
    context_table.add_column("Text", overflow="fold")
    for index, chunk in enumerate(contexts, start=1):
        text = " ".join(chunk["text"].split())
        if len(text) > 200:
            text = text[:197] + "..."
        context_table.add_row(
            str(index),
            chunk["source_doc"],
            chunk["chunk_id"],
            f"{chunk['score']:.3f}",
            text,
        )
    console.print(context_table)

    metrics = record["metrics"]
    metric_table = Table(title="Metrics", show_header=True, expand=True)
    metric_table.add_column("Metric", style="bold")
    metric_table.add_column("Score", justify="right")
    metric_table.add_column("", overflow="fold")
    for key in METRIC_KEYS:
        score = metrics.get(key)
        label = METRIC_LABELS[key]
        display = "N/A" if score is None else f"{score:.3f}"
        metric_table.add_row(
            label,
            Text(display, style=_style(score)),
            Text(_bar(score), style=_style(score)),
        )
    console.print(metric_table)
    console.print(Rule(style="dim"))


def render_history(console: Console, entries: list[dict[str, Any]]) -> None:
    if not entries:
        console.print("[yellow]No history yet.[/yellow]")
        return
    table = Table(title=f"History ({len(entries)} turns)", expand=True)
    table.add_column("#", justify="right", style="dim", no_wrap=True)
    table.add_column("Session", style="dim", no_wrap=True)
    table.add_column("Time", style="dim", no_wrap=True)
    table.add_column("Question", overflow="fold")
    table.add_column("Overall", justify="right", no_wrap=True)
    table.add_column("Result", no_wrap=True)
    for index, entry in enumerate(entries, start=1):
        overall = entry["metrics"].get("overall")
        result = "PASS" if entry["passed"] else "FAIL"
        if entry["failure_type"]:
            result = f"{result} ({entry['failure_type']})"
        table.add_row(
            str(index),
            entry["session_id"][-9:],
            entry["timestamp"][11:19],
            entry["question"],
            Text("N/A" if overall is None else f"{overall:.3f}", style=_style(overall)),
            Text(result, style=_style(1.0 if entry["passed"] else 0.2)),
        )
    console.print(table)


def render_stats(
    console: Console, entries: list[dict[str, Any]], title: str
) -> None:
    if not entries:
        console.print(f"[yellow]No entries for {title}.[/yellow]")
        return

    table = Table(title=title, expand=True)
    table.add_column("Metric", style="bold")
    table.add_column("Avg", justify="right")
    table.add_column("Min", justify="right")
    table.add_column("Max", justify="right")
    table.add_column("N", justify="right", style="dim")
    table.add_column("", overflow="fold")
    for key in METRIC_KEYS:
        values = [
            entry["metrics"][key]
            for entry in entries
            if entry["metrics"].get(key) is not None
        ]
        if not values:
            table.add_row(METRIC_LABELS[key], "N/A", "N/A", "N/A", "0", "")
            continue
        average = sum(values) / len(values)
        table.add_row(
            METRIC_LABELS[key],
            Text(f"{average:.3f}", style=_style(average)),
            f"{min(values):.3f}",
            f"{max(values):.3f}",
            str(len(values)),
            Text(_bar(average), style=_style(average)),
        )
    console.print(table)

    passed = sum(1 for entry in entries if entry["passed"])
    pass_rate = passed / len(entries)
    console.print(
        f"Pass rate: [{_style(pass_rate)}]{passed}/{len(entries)} "
        f"({pass_rate * 100:.1f}%)[/]"
    )

    failures: dict[str, int] = {}
    for entry in entries:
        if entry["failure_type"]:
            failures[entry["failure_type"]] = failures.get(entry["failure_type"], 0) + 1
    if failures:
        distribution = "  ".join(
            f"[red]{name}[/red]: {count}"
            for name, count in sorted(failures.items())
        )
        console.print(f"Failures: {distribution}")
    else:
        console.print("Failures: [green]none[/green]")


def render_welcome(
    console: Console,
    session_id: str,
    model: str,
    top_k: int,
    history_path: Path,
) -> None:
    body = Text()
    body.append("OrbitTech Support — Evaluation CLI\n\n", style="bold cyan")
    body.append("Enter a ", style="dim")
    body.append("question", style="bold")
    body.append(", then an ", style="dim")
    body.append("expected answer", style="bold")
    body.append(" (optional) to score the turn.\n", style="dim")
    body.append(f"Session: {session_id}\n", style="dim")
    body.append(f"Model:   {model}  ·  top_k: {top_k}\n", style="dim")
    body.append(f"History: {history_path}\n\n", style="dim")
    body.append("Commands: ", style="dim")
    body.append("/stats --all", style="bold green")
    body.append("  ", style="dim")
    body.append("/stats --session [ID]", style="bold green")
    body.append("  ", style="dim")
    body.append("/history", style="bold green")
    body.append("  ", style="dim")
    body.append("/quit", style="bold green")
    console.print(Panel(body, border_style="blue"))


# ---------------------------------------------------------------------------
# Commands
# ---------------------------------------------------------------------------

def parse_stats_args(
    parts: list[str], current_session: str
) -> tuple[str, str | None]:
    mode: str | None = None
    session: str | None = None
    index = 1
    while index < len(parts):
        token = parts[index]
        if token == "--all":
            mode = "all"
        elif token == "--session":
            mode = "session"
            if index + 1 < len(parts) and not parts[index + 1].startswith("-"):
                session = parts[index + 1]
                index += 1
        elif mode == "session" and session is None:
            session = token
        index += 1

    if mode is None:
        mode = "all"
    if mode == "session" and session in (None, "", "[]"):
        session = current_session
    return mode, session


def handle_command(
    console: Console,
    command: str,
    history: dict[str, Any],
    current_session: str,
) -> bool:
    """Handle a slash command. Returns True when the CLI should quit."""
    parts = command.split()
    name = parts[0].lower()

    if name in ("/quit", "/exit", "/q"):
        return True
    if name == "/help":
        console.print(
            "Commands: [bold green]/stats --all[/], "
            "[bold green]/stats --session [ID][/], "
            "[bold green]/history[/], [bold green]/quit[/]"
        )
        return False
    if name == "/history":
        render_history(console, history["entries"])
        return False
    if name == "/stats":
        mode, session = parse_stats_args(parts, current_session)
        if mode == "all":
            render_stats(console, history["entries"], "Stats — all history")
        else:
            session_entries = [
                entry
                for entry in history["entries"]
                if entry["session_id"] == session
            ]
            if not session_entries and session != current_session:
                known = ", ".join(
                    sorted({entry["session_id"] for entry in history["entries"]})
                ) or "none"
                console.print(
                    f"[red]Unknown session[/red] '{session}'. Known: {known}"
                )
                return False
            render_stats(console, session_entries, f"Stats — session {session}")
        return False

    console.print(
        f"[red]Unknown command[/red] '{name}'. Try [bold]/help[/bold]."
    )
    return False


# ---------------------------------------------------------------------------
# Main loop
# ---------------------------------------------------------------------------

def run(args: argparse.Namespace) -> int:
    console = Console()
    history_path = args.history.expanduser()
    history = load_history(history_path)

    session_id = (
        datetime.now().strftime("%Y%m%d-%H%M%S") + "-" + uuid.uuid4().hex[:6]
    )

    try:
        assistant = DomainAssistant.from_corpus(args.corpus_dir, top_k=args.top_k)
    except Exception as exc:  # noqa: BLE001 - surface a friendly startup error
        console.print(f"[red]Failed to initialise the domain assistant:[/red] {exc}")
        console.print("[dim]Check .env (OPENAI_API_KEY / OPENAI_MODEL) and the corpus.[/dim]")
        return 2

    model = getattr(assistant.generator, "model", assistant.generator.__class__.__name__)
    now = datetime.now(UTC).isoformat()
    history["sessions"].append(
        {
            "id": session_id,
            "started_at": now,
            "ended_at": None,
            "model": model,
            "top_k": args.top_k,
            "corpus_id": assistant.corpus_id,
        }
    )
    save_history(history_path, history)

    render_welcome(console, session_id, model, args.top_k, history_path)
    evaluator = RAGASEvaluator()

    try:
        while True:
            try:
                raw = Prompt.ask("\n[bold cyan]Question[/bold cyan] [dim](/help)[/dim]")
            except (EOFError, KeyboardInterrupt):
                break

            question = raw.strip()
            if not question:
                continue
            if question.startswith("/"):
                if handle_command(console, question, history, session_id):
                    break
                continue

            try:
                expected = Prompt.ask(
                    "[bold magenta]Expected answer[/bold magenta] "
                    "[dim](optional, Enter to skip)[/dim]",
                    default="",
                )
            except (EOFError, KeyboardInterrupt):
                break

            try:
                with console.status("[cyan]Retrieving and generating...[/cyan]"):
                    started = time.perf_counter()
                    response = assistant.answer_with_trace(question)
                    elapsed = time.perf_counter() - started
            except Exception as exc:  # noqa: BLE001 - keep the chat alive
                console.print(f"[red]Generation failed:[/red] {exc}")
                continue

            retrieved = [
                {
                    "source_doc": chunk.source_doc,
                    "chunk_id": chunk.chunk_id,
                    "text": chunk.text,
                    "score": round(chunk.score, 6),
                }
                for chunk in response.retrieved_chunks
            ]
            metrics, passed, failure_type = evaluate_turn(
                evaluator,
                question,
                expected,
                response.actual_answer,
                [chunk["text"] for chunk in retrieved],
            )
            record = {
                "id": uuid.uuid4().hex,
                "session_id": session_id,
                "timestamp": datetime.now(UTC).isoformat(),
                "question": question,
                "expected_answer": expected.strip(),
                "actual_answer": response.actual_answer,
                "retrieved_contexts": retrieved,
                "metrics": metrics,
                "passed": passed,
                "failure_type": failure_type,
                "elapsed_s": round(elapsed, 2),
            }
            render_turn(console, question, expected.strip(), record)
            history["entries"].append(record)
            save_history(history_path, history)
    finally:
        for session in history["sessions"]:
            if session["id"] == session_id:
                session["ended_at"] = datetime.now(UTC).isoformat()
        save_history(history_path, history)

    console.print("[dim]Session saved. Goodbye.[/dim]")
    return 0


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--corpus-dir",
        type=Path,
        default=DEFAULT_CORPUS_DIR,
        help=f"Corpus directory (default: {DEFAULT_CORPUS_DIR})",
    )
    parser.add_argument("--top-k", type=int, default=5, help="Retriever top-k")
    parser.add_argument(
        "--history",
        type=Path,
        default=DEFAULT_HISTORY_PATH,
        help=f"History JSON file (default: {DEFAULT_HISTORY_PATH})",
    )
    return parser.parse_args()


if __name__ == "__main__":
    raise SystemExit(run(parse_args()))
