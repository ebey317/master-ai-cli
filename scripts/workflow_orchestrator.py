"""
Dynamic Workflow Orchestration for Sensei.

Portable adaptation of Hermes dynamic-workflow skill:
- Layer A: Deterministic fan-out via executable Python scripts (no LLM calls)
- Layer B: LLM-judgment fan-out via batched sub-agent delegation
- Adversarial-convergence verification: N attempts + M refuters → surviving claims only
- Synchronous-trap aware: foreground (single-turn) vs durable (kanban-backed) modes
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import threading
import time
import uuid
from abc import ABC, abstractmethod
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

# ──────────────────────────────────────────────────────────────────────
# Data structures
# ──────────────────────────────────────────────────────────────────────

@dataclass
class WorkUnit:
    """A single independent unit of work for fan-out."""
    id: str
    payload: dict[str, Any]          # input data for this unit
    metadata: dict[str, Any] = field(default_factory=dict)

@dataclass
class WorkResult:
    """Result from processing one work unit."""
    unit_id: str
    success: bool
    output: Any = None
    error: str | None = None
    artifacts: dict[str, Path] = field(default_factory=dict)  # files written

@dataclass
class VerificationClaim:
    """A claim produced by an attempt, subject to refutation."""
    claim_id: str
    content: str
    source_attempt: str
    location: str | None = None       # file:line or doc ref for "located" claims
    survived_refutation: bool = False

@dataclass
class WorkflowResult:
    """Final verified result of a workflow."""
    verified_claims: list[VerificationClaim]
    all_attempts: list[dict[str, Any]]
    convergence_rounds: int
    metadata: dict[str, Any] = field(default_factory=dict)


# ──────────────────────────────────────────────────────────────────────
# Layer A: Deterministic fan-out (execute_code equivalent)
# ──────────────────────────────────────────────────────────────────────

class DeterministicWorker(ABC):
    """
    Base class for Layer A workers. Runs in a sandboxed Python subprocess
    with only deterministic tools (file I/O, shell, search, web fetch).
    No LLM calls, no delegation.
    """

    # Override in subclass: allowed imports for the sandbox
    ALLOWED_IMPORTS = {
        "json", "os", "sys", "pathlib", "re", "subprocess",
        "tempfile", "textwrap", "hashlib", "datetime", "collections",
        "itertools", "functools", "dataclasses", "typing", "csv",
    }

    @abstractmethod
    def process_unit(self, unit: WorkUnit) -> WorkResult:
        """Process a single work unit. Must be deterministic."""
        pass

    def _write_script(self, units: list[WorkUnit], output_dir: Path) -> Path:
        """Generate a standalone Python script that processes all units."""
        script = f'''#!/usr/bin/env python3
# Auto-generated Layer A worker script
# Deterministic fan-out — no LLM calls, no delegation
import json
import sys
from pathlib import Path

# --- Worker implementation (inlined) ---
{self._get_worker_source()}
# --- End worker ---

def main():
    units_data = json.loads(sys.argv[1])
    output_dir = Path(sys.argv[2])
    output_dir.mkdir(parents=True, exist_ok=True)

    results = []
    for u in units_data:
        unit = type('WorkUnit', (), u)()
        worker = WorkerImpl()
        result = worker.process_unit(unit)
        results.append({{
            "unit_id": result.unit_id,
            "success": result.success,
            "output": result.output,
            "error": result.error,
            "artifacts": {{k: str(v) for k, v in result.artifacts.items()}}
        }})

    (output_dir / "results.json").write_text(json.dumps(results, indent=2))

if __name__ == "__main__":
    main()
'''
        script_path = output_dir / f"worker_{uuid.uuid4().hex[:8]}.py"
        script_path.write_text(script)
        script_path.chmod(0o755)
        return script_path

    def _get_worker_source(self) -> str:
        """Return the source code of the worker class for inlining."""
        import inspect
        return inspect.getsource(self.__class__)

    def execute_batch(
        self,
        units: list[WorkUnit],
        max_workers: int = 8,
        timeout_sec: int = 300,
    ) -> list[WorkResult]:
        """
        Execute units in parallel using ThreadPoolExecutor (in-process).
        For true sandboxing, use execute_batch_sandboxed() instead.
        """
        results = []
        with ThreadPoolExecutor(max_workers=max_workers) as ex:
            futures = {ex.submit(self.process_unit, u): u for u in units}
            for fut in as_completed(futures, timeout=timeout_sec):
                u = futures[fut]
                try:
                    results.append(fut.result())
                except Exception as e:
                    results.append(WorkResult(unit_id=u.id, success=False, error=str(e)))
        return results

    def execute_batch_sandboxed(
        self,
        units: list[WorkUnit],
        output_dir: Path | None = None,
        timeout_sec: int = 600,
    ) -> list[WorkResult]:
        """
        Execute units in a separate Python subprocess (sandboxed).
        The script has no access to LLM or delegation tools.
        """
        if output_dir is None:
            output_dir = Path(tempfile.mkdtemp(prefix="sensei_workflow_"))

        script_path = self._write_script(units, output_dir)
        units_json = json.dumps([
            {"id": u.id, "payload": u.payload, "metadata": u.metadata}
            for u in units
        ])

        try:
            subprocess.run(
                [sys.executable, str(script_path), units_json, str(output_dir)],
                check=True,
                timeout=timeout_sec,
                capture_output=True,
                text=True,
            )
        except subprocess.TimeoutExpired as e:
            return [WorkResult(unit_id=u.id, success=False, error="Timeout") for u in units]
        except subprocess.CalledProcessError as e:
            return [WorkResult(unit_id=u.id, success=False, error=e.stderr) for u in units]

        results_file = output_dir / "results.json"
        if not results_file.exists():
            return [WorkResult(unit_id=u.id, success=False, error="No results file")]

        raw = json.loads(results_file.read_text())
        return [
            WorkResult(
                unit_id=r["unit_id"],
                success=r["success"],
                output=r["output"],
                error=r["error"],
                artifacts={k: Path(v) for k, v in r.get("artifacts", {}).items()},
            )
            for r in raw
        ]


# ──────────────────────────────────────────────────────────────────────
# Layer B: LLM-judgment fan-out (delegate_task equivalent)
# ──────────────────────────────────────────────────────────────────────

class JudgmentWorker(ABC):
    """
    Base class for Layer B workers. Each unit is processed by an independent
    LLM call (simulated here; in Sensei, this would invoke the agent loop).
    """

    @abstractmethod
    def build_prompt(self, unit: WorkUnit) -> str:
        """Build the prompt for this unit's LLM call."""
        pass

    @abstractmethod
    def parse_response(self, unit: WorkUnit, response: str) -> WorkResult:
        """Parse the LLM response into a WorkResult."""
        pass

    def execute_batch(
        self,
        units: list[WorkUnit],
        llm_call: Callable[[str], str],
        max_concurrent: int = 3,
        timeout_sec: int = 180,
    ) -> list[WorkResult]:
        """
        Execute units in parallel via LLM calls.
        llm_call: function that takes a prompt and returns the response.
        """
        results = []
        semaphore = threading.Semaphore(max_concurrent)

        def process_one(unit: WorkUnit) -> WorkResult:
            with semaphore:
                prompt = self.build_prompt(unit)
                try:
                    response = llm_call(prompt)
                    return self.parse_response(unit, response)
                except Exception as e:
                    return WorkResult(unit_id=unit.id, success=False, error=str(e))

        with ThreadPoolExecutor(max_workers=max_concurrent) as ex:
            futures = {ex.submit(process_one, u): u for u in units}
            for fut in as_completed(futures, timeout=timeout_sec):
                results.append(fut.result())

        return results


# ──────────────────────────────────────────────────────────────────────
# Adversarial-Convergence Verification
# ──────────────────────────────────────────────────────────────────────

class AdversarialConvergence:
    """
    Verification recipe: N independent attempts with varied framings +
    M refuters per claim → keep only located claims that survive refutation.
    Iterate to convergence.
    """

    def __init__(
        self,
        n_attempts: int = 3,
        m_refuters: int = 2,
        max_rounds: int = 3,
        require_location: bool = True,
    ):
        self.n_attempts = n_attempts
        self.m_refuters = m_refuters
        self.max_rounds = max_rounds
        self.require_location = require_location

    def run(
        self,
        question: str,
        attempt_fn: Callable[[str, int], list[VerificationClaim]],
        refuter_fn: Callable[[VerificationClaim, int], bool],
        vary_framing_fn: Callable[[str, int], str],
    ) -> WorkflowResult:
        """
        Run the adversarial-convergence protocol.

        Args:
            question: The question/claim to verify
            attempt_fn(framing, attempt_idx) -> list[VerificationClaim]:
                Produce claims for a given framing
            refuter_fn(claim, refuter_idx) -> bool:
                Return True if claim SURVIVES this refuter
            vary_framing_fn(question, attempt_idx) -> str:
                Generate a varied framing for independent attempt

        Returns:
            WorkflowResult with verified claims that survived all rounds
        """
        all_attempts = []
        surviving_claims: dict[str, VerificationClaim] = {}

        for round_num in range(self.max_rounds):
            round_claims: dict[str, VerificationClaim] = {}

            # N independent attempts with varied framings
            for attempt_idx in range(self.n_attempts):
                framing = vary_framing_fn(question, attempt_idx + round_num * self.n_attempts)
                claims = attempt_fn(framing, attempt_idx)

                for claim in claims:
                    claim_id = f"r{round_num}_a{attempt_idx}_{claim.claim_id}"
                    claim.claim_id = claim_id
                    round_claims[claim_id] = claim

            all_attempts.append({
                "round": round_num,
                "claims": [c.__dict__ for c in round_claims.values()],
            })

            # M refuters per claim
            for claim in round_claims.values():
                survived = True
                for refuter_idx in range(self.m_refuters):
                    if not refuter_fn(claim, refuter_idx):
                        survived = False
                        break

                if survived:
                    if self.require_location and not claim.location:
                        survived = False

                if survived:
                    claim.survived_refutation = True
                    surviving_claims[claim.claim_id] = claim

            # Convergence check: no new surviving claims this round
            if round_num > 0 and len(surviving_claims) == len([
                c for c in round_claims.values() if c.survived_refutation
            ]):
                break

        return WorkflowResult(
            verified_claims=list(surviving_claims.values()),
            all_attempts=all_attempts,
            convergence_rounds=round_num + 1,
            metadata={
                "n_attempts": self.n_attempts,
                "m_refuters": self.m_refuters,
                "question": question,
            },
        )


# ──────────────────────────────────────────────────────────────────────
# High-level Workflow Builder
# ──────────────────────────────────────────────────────────────────────

class DynamicWorkflow:
    """
    Compose Layer A (deterministic) + Layer B (judgment) + Verification
    into a complete foreground workflow.
    """

    def __init__(self, name: str, work_dir: Path | None = None):
        self.name = name
        self.work_dir = work_dir or Path(tempfile.mkdtemp(prefix=f"sensei_wf_{name}_"))
        self.work_dir.mkdir(parents=True, exist_ok=True)
        self.layer_a_workers: list[DeterministicWorker] = []
        self.layer_b_workers: list[tuple[str, JudgmentWorker, list[WorkUnit]]] = []
        self.verification: AdversarialConvergence | None = None

    def add_layer_a(self, worker: DeterministicWorker, units: list[WorkUnit]) -> "DynamicWorkflow":
        """Add a deterministic fan-out stage."""
        self.layer_a_workers.append(worker)
        # Store units as metadata on worker for execution
        worker._workflow_units = units  # type: ignore[attr-defined]
        return self

    def add_layer_b(
        self,
        name: str,
        worker: JudgmentWorker,
        units: list[WorkUnit],
    ) -> "DynamicWorkflow":
        """Add an LLM-judgment fan-out stage."""
        self.layer_b_workers.append((name, worker, units))
        return self

    def set_verification(self, verification: AdversarialConvergence) -> "DynamicWorkflow":
        """Set the adversarial-convergence verification for the final output."""
        self.verification = verification
        return self

    def run_foreground(
        self,
        llm_call: Callable[[str], str] | None = None,
    ) -> WorkflowResult:
        """
        Run the complete workflow in foreground (single turn).
        Returns verified result.
        """
        all_results: dict[str, list[WorkResult]] = {}

        # Layer A: deterministic pre-pass
        for worker in self.layer_a_workers:
            units = getattr(worker, "_workflow_units", [])
            results = worker.execute_batch_sandboxed(units, self.work_dir / f"layer_a_{worker.__class__.__name__}")
            all_results[worker.__class__.__name__] = results

        # Layer B: LLM-judgment fan-out
        if llm_call is None:
            raise ValueError("llm_call required for Layer B execution")

        for name, worker, units in self.layer_b_workers:
            results = worker.execute_batch(units, llm_call)
            all_results[name] = results

        # Verification (if configured)
        if self.verification:
            # Collect claims from Layer B results
            def attempt_fn(framing: str, idx: int) -> list[VerificationClaim]:
                # In practice, this would re-run Layer B with varied framing
                # For now, synthesize from existing results
                claims = []
                for rname, results in all_results.items():
                    for r in results:
                        if r.success and r.output:
                            claims.append(VerificationClaim(
                                claim_id=f"{rname}_{r.unit_id}",
                                content=str(r.output),
                                source_attempt=f"round_0_attempt_{idx}",
                                location=r.artifacts.get("source") if r.artifacts else None,
                            ))
                return claims[:self.verification.n_attempts]

            def refuter_fn(claim: VerificationClaim, ref_idx: int) -> bool:
                # Simple refutation: check if claim is contradicted by other claims
                # Real implementation would call LLM with refutation prompt
                return True  # placeholder

            def vary_framing_fn(q: str, idx: int) -> str:
                framings = [
                    q,
                    f"Critically evaluate: {q}",
                    f"From a skeptical perspective: {q}",
                    f"Assuming the opposite is true: {q}",
                ]
                return framings[idx % len(framings)]

            return self.verification.run(
                question=self.name,
                attempt_fn=attempt_fn,
                refuter_fn=refuter_fn,
                vary_framing_fn=vary_framing_fn,
            )

        # No verification: return raw results
        return WorkflowResult(
            verified_claims=[],
            all_attempts=[{"results": {k: [r.__dict__ for r in v] for k, v in all_results.items()}}],
            convergence_rounds=0,
            metadata={"work_dir": str(self.work_dir)},
        )


# ──────────────────────────────────────────────────────────────────────
# Example workers (templates for users to extend)
# ──────────────────────────────────────────────────────────────────────

class FileAnalysisWorker(DeterministicWorker):
    """Example Layer A: analyze files in parallel (grep, parse, extract)."""

    def process_unit(self, unit: WorkUnit) -> WorkResult:
        file_path = Path(unit.payload["path"])
        pattern = unit.payload.get("pattern", "")

        try:
            content = file_path.read_text()
            matches = [line for line in content.splitlines() if pattern in line]

            out_file = Path(tempfile.mktemp(suffix=".json", dir="/tmp"))
            out_file.write_text(json.dumps({"file": str(file_path), "matches": matches}))

            return WorkResult(
                unit_id=unit.id,
                success=True,
                output={"match_count": len(matches)},
                artifacts={"results": out_file},
            )
        except Exception as e:
            return WorkResult(unit_id=unit.id, success=False, error=str(e))


class CodeReviewWorker(JudgmentWorker):
    """Example Layer B: LLM code review per file."""

    def build_prompt(self, unit: WorkUnit) -> str:
        file_path = unit.payload["path"]
        content = unit.payload.get("content", Path(file_path).read_text())
        return f"""Review this code for bugs, security issues, and style:

File: {file_path}
---
{content}
---
Return JSON: {{"issues": [...], "severity": "low|medium|high|critical"}}"""

    def parse_response(self, unit: WorkUnit, response: str) -> WorkResult:
        try:
            data = json.loads(response)
            return WorkResult(
                unit_id=unit.id,
                success=True,
                output=data,
            )
        except json.JSONDecodeError:
            return WorkResult(unit_id=unit.id, success=False, error="Invalid JSON response")


# ──────────────────────────────────────────────────────────────────────
# Kanban Swarm integration point (durable workflows)
# ──────────────────────────────────────────────────────────────────────

class KanbanSwarmBridge:
    """
    Bridge to the durable kanban-swarm subsystem for workflows that must
    survive interruptions (hours/days). This is a stub — wire to actual
    hermes_cli/kanban_swarm.py when available.
    """

    def __init__(self, db_path: Path | None = None):
        self.db_path = db_path or Path.home() / ".sensei" / "kanban.db"
        self.db_path.parent.mkdir(parents=True, exist_ok=True)

    def enqueue_workflow(self, workflow: DynamicWorkflow) -> str:
        """Persist workflow as a kanban task graph. Returns task graph ID."""
        # TODO: Implement actual kanban persistence
        task_graph_id = f"wf_{uuid.uuid4().hex[:12]}"
        (self.db_path.parent / f"{task_graph_id}.json").write_text(json.dumps({
            "name": workflow.name,
            "work_dir": str(workflow.work_dir),
            "status": "pending",
            "created": time.time(),
        }))
        return task_graph_id

    def resume_workflow(self, task_graph_id: str) -> DynamicWorkflow | None:
        """Resume a workflow from kanban state."""
        # TODO: Implement
        return None


# ──────────────────────────────────────────────────────────────────────
# Convenience: build a workflow from a simple spec
# ──────────────────────────────────────────────────────────────────────

def build_codebase_sweep_workflow(
    root: Path,
    pattern: str,
    review_prompt: str | None = None,
    n_verification_attempts: int = 3,
) -> DynamicWorkflow:
    """
    Build a standard codebase sweep: Layer A finds matches, Layer B reviews,
    verification cross-checks findings.
    """
    # Layer A: find all matching files
    import glob
    files = list(root.rglob("*"))
    files = [f for f in files if f.is_file() and f.suffix in {".py", ".js", ".ts", ".go", ".rs"}]

    units = [
        WorkUnit(id=f"file_{i}", payload={"path": str(f), "pattern": pattern})
        for i, f in enumerate(files)
    ]

    wf = DynamicWorkflow(f"sweep_{pattern.replace(' ', '_')}")
    wf.add_layer_a(FileAnalysisWorker(), units)

    # Layer B: review each file with matches
    # (In practice, filter units to only those with matches first)
    review_worker = CodeReviewWorker()
    wf.add_layer_b("code_review", review_worker, units[:50])  # limit for demo

    # Verification
    wf.set_verification(AdversarialConvergence(
        n_attempts=n_verification_attempts,
        m_refuters=2,
        max_rounds=2,
    ))

    return wf
