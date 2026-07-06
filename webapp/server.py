"""Web UI for the multi-provider evaluation harness.

Run from the project root:

    uvicorn webapp.server:app --reload

API:
    GET  /api/configs          -> available config files + their contents
    GET  /api/runs             -> run summaries (newest first)
    POST /api/runs             -> {"config": "config.demo.yaml"} starts a run
    GET  /api/runs/{run_id}    -> full run detail incl. partial results

Runs execute in a background thread; the frontend polls for progress.
Results are kept in memory and also written to runs/<run_id>/ on completion.
"""
from __future__ import annotations

import json
import pathlib
import threading
import time
import uuid
from dataclasses import asdict, dataclass, field

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from eval_agents.config import load_agents, load_config, load_tasks, select_use_case
from eval_agents.report import to_json, to_markdown, to_summary_json
from eval_agents.runner import run_evaluation

ROOT = pathlib.Path(__file__).resolve().parent.parent
STATIC = pathlib.Path(__file__).resolve().parent / "static"

app = FastAPI(title="multi-agent-eval")


@app.on_event("startup")
def _startup() -> None:
    _rehydrate_runs()


# ---------------------------------------------------------------- run store
@dataclass
class Run:
    id: str
    config_file: str
    tasks_file: str
    status: str = "running"  # running | completed | failed
    error: str | None = None
    started_at: float = field(default_factory=time.time)
    finished_at: float | None = None
    total_tasks: int = 0
    done_tasks: int = 0
    candidates: list[str] = field(default_factory=list)
    results: list[dict] = field(default_factory=list)

    results_loaded: bool = True  # False for rehydrated runs until results.json is read

    def summary(self) -> dict:
        return {
            "id": self.id,
            "config_file": self.config_file,
            "tasks_file": self.tasks_file,
            "status": self.status,
            "error": self.error,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "total_tasks": self.total_tasks,
            "done_tasks": self.done_tasks,
            "candidates": self.candidates,
        }

    @property
    def dir(self) -> pathlib.Path:
        return RUNS_DIR / self.id

    def save_meta(self) -> None:
        """Persist run metadata so history survives server restarts."""
        self.dir.mkdir(parents=True, exist_ok=True)
        (self.dir / "run.json").write_text(json.dumps(self.summary(), indent=2))

    def save_partial_results(self) -> None:
        (self.dir / "results.json").write_text(json.dumps(self.results, indent=2, default=str))


RUNS: dict[str, Run] = {}
RUNS_DIR = ROOT / "runs"
_LOCK = threading.Lock()


def _rehydrate_runs() -> None:
    """Load past runs from runs/<id>/ at startup. Runs that were mid-flight
    when the server stopped are marked failed (their partial results remain
    viewable). Older dirs without run.json get metadata synthesized."""
    if not RUNS_DIR.is_dir():
        return
    for d in RUNS_DIR.iterdir():
        if not d.is_dir() or d.name in RUNS:
            continue
        meta_path, results_path = d / "run.json", d / "results.json"
        try:
            if meta_path.is_file():
                meta = json.loads(meta_path.read_text())
                meta.pop("id", None)
                run = Run(id=d.name, **{k: v for k, v in meta.items() if k in Run.__dataclass_fields__})
            elif results_path.is_file():  # pre-persistence run dir
                results = json.loads(results_path.read_text())
                run = Run(
                    id=d.name, config_file="(unknown)", tasks_file="(unknown)",
                    status="completed", started_at=results_path.stat().st_mtime,
                    finished_at=results_path.stat().st_mtime,
                    total_tasks=len(results), done_tasks=len(results),
                    candidates=sorted({r["candidate"] for tr in results for r in tr["results"]}),
                )
            else:
                continue
        except Exception:
            continue
        if run.status == "running":
            run.status = "failed"
            run.error = "interrupted by server restart"
            run.finished_at = run.finished_at or time.time()
        run.results_loaded = False
        RUNS[run.id] = run


def _ensure_results(run: Run) -> None:
    """Lazy-load results.json for runs rehydrated from disk."""
    if run.results_loaded:
        return
    path = run.dir / "results.json"
    if path.is_file():
        try:
            run.results = json.loads(path.read_text())
        except Exception:
            pass
    run.results_loaded = True


def _execute(run: Run) -> None:
    try:
        config = load_config(ROOT / run.config_file)
        candidates, judge = load_agents(config)
        _, scorer = select_use_case(config)
        tasks = load_tasks(ROOT / run.tasks_file)
        run.total_tasks = len(tasks)
        run.candidates = [c.name for c in candidates]
        run.save_meta()

        def on_task_done(task_result, done, total):
            with _LOCK:
                run.results.append(asdict(task_result))
                run.done_tasks = done
                # persist incrementally so an interrupted run keeps partial data
                run.save_partial_results()
                run.save_meta()

        results = run_evaluation(tasks, candidates, judge, scorer=scorer, on_task_done=on_task_done)

        out = run.dir
        out.mkdir(parents=True, exist_ok=True)
        (out / "results.json").write_text(to_json(results))
        (out / "summary.json").write_text(to_summary_json(results, scorecard=config.get("scorecard")))
        (out / "report.md").write_text(to_markdown(results, scorecard=config.get("scorecard")))
        run.status = "completed"
    except Exception as exc:
        run.status = "failed"
        run.error = f"{type(exc).__name__}: {exc}"
    finally:
        run.finished_at = time.time()
        run.save_meta()


# ---------------------------------------------------------------- endpoints
class NewRun(BaseModel):
    config: str = "config.yaml"
    tasks: str | None = None  # default: config's `tasks:` field, else tasks.yaml


@app.get("/api/configs")
def list_configs() -> list[dict]:
    configs = []
    for path in sorted(ROOT.glob("config*.yaml")):
        cfg = load_config(path)
        configs.append(
            {
                "file": path.name,
                "use_case": cfg.get("use_case", "generic"),
                "tasks": cfg.get("tasks", "tasks.yaml"),
                "candidates": cfg.get("candidates", []),
                "judge": cfg.get("judge", {}),
            }
        )
    return configs


@app.get("/api/runs")
def list_runs() -> list[dict]:
    return [r.summary() for r in sorted(RUNS.values(), key=lambda r: r.started_at, reverse=True)]


@app.post("/api/runs", status_code=201)
def create_run(body: NewRun) -> dict:
    tasks = body.tasks
    if tasks is None:
        if "/" in body.config or "\\" in body.config or not (ROOT / body.config).is_file():
            raise HTTPException(400, f"file not found: {body.config}")
        tasks = load_config(ROOT / body.config).get("tasks", "tasks.yaml")

    for name in (body.config, tasks):
        # config/tasks must be a plain filename inside the project root
        if "/" in name or "\\" in name or not (ROOT / name).is_file():
            raise HTTPException(400, f"file not found: {name}")

    run = Run(id=uuid.uuid4().hex[:12], config_file=body.config, tasks_file=tasks)
    RUNS[run.id] = run
    run.save_meta()
    threading.Thread(target=_execute, args=(run,), daemon=True).start()
    return run.summary()


@app.get("/api/runs/{run_id}")
def get_run(run_id: str) -> dict:
    run = RUNS.get(run_id)
    if not run:
        raise HTTPException(404, "run not found")
    with _LOCK:
        _ensure_results(run)
        return {**run.summary(), "results": run.results}


@app.get("/")
def index() -> FileResponse:
    return FileResponse(STATIC / "index.html")


app.mount("/static", StaticFiles(directory=STATIC), name="static")
