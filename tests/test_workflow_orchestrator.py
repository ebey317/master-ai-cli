"""
Tests for Dynamic Workflow Orchestration.
"""

import os
import tempfile
from pathlib import Path

from scripts.workflow_orchestrator import (
    AdversarialConvergence,
    DeterministicWorker,
    DynamicWorkflow,
    JudgmentWorker,
    VerificationClaim,
    WorkResult,
    WorkUnit,
    build_codebase_sweep_workflow,
)


class TestDeterministicWorker:
    def test_process_unit_basic(self):
        class TestWorker(DeterministicWorker):
            def process_unit(self, unit: WorkUnit) -> WorkResult:
                return WorkResult(
                    unit_id=unit.id,
                    success=True,
                    output={"processed": unit.payload.get("value", 0) * 2},
                )

        worker = TestWorker()
        units = [WorkUnit(id="1", payload={"value": 5})]
        results = worker.execute_batch(units)

        assert len(results) == 1
        assert results[0].success
        assert results[0].output == {"processed": 10}

    def test_execute_batch_parallel(self):
        class SlowWorker(DeterministicWorker):
            def process_unit(self, unit: WorkUnit) -> WorkResult:
                import time

                time.sleep(0.05)
                return WorkResult(unit_id=unit.id, success=True, output=unit.payload)

        worker = SlowWorker()
        units = [WorkUnit(id=str(i), payload={"n": i}) for i in range(10)]

        import time

        start = time.time()
        results = worker.execute_batch(units, max_workers=5)
        elapsed = time.time() - start

        # With 5 workers, 10 units × 0.05s ≈ 0.1s (not 0.5s serial)
        assert elapsed < 0.3
        assert all(r.success for r in results)

    def test_sandboxed_execution(self):
        # Self-contained on purpose: execute_batch_sandboxed() inlines this
        # class into a standalone script run by a fresh interpreter, so a
        # method body may only use names it imports itself. Referencing
        # WorkResult from this module's enclosing scope cannot work in a
        # subprocess that never imports the test module. DeterministicWorker
        # is resolved by the generated script from its defining module, so
        # subclassing still works.
        class SandboxWorker(DeterministicWorker):
            def process_unit(self, unit):
                # This runs in a separate process
                import os

                from scripts.workflow_orchestrator import WorkResult

                return WorkResult(
                    unit_id=unit.id,
                    success=True,
                    output={"pid": os.getpid()},
                )

        worker = SandboxWorker()
        units = [WorkUnit(id="1", payload={})]

        with tempfile.TemporaryDirectory() as tmp:
            results = worker.execute_batch_sandboxed(units, Path(tmp))

        assert len(results) == 1
        assert results[0].success
        # It really did run elsewhere: a different pid than this process.
        assert results[0].output["pid"] != os.getpid()


class TestJudgmentWorker:
    def test_execute_batch_with_mock_llm(self):
        class MockWorker(JudgmentWorker):
            def build_prompt(self, unit: WorkUnit) -> str:
                return f"Process {unit.payload['value']}"

            def parse_response(self, unit: WorkUnit, response: str) -> WorkResult:
                return WorkResult(
                    unit_id=unit.id,
                    success=True,
                    output={"response": response},
                )

        def mock_llm(prompt: str) -> str:
            return f"LLM says: {prompt}"

        worker = MockWorker()
        units = [WorkUnit(id=str(i), payload={"value": i}) for i in range(3)]
        results = worker.execute_batch(units, mock_llm, max_concurrent=2)

        assert len(results) == 3
        assert all(r.success for r in results)
        assert all("LLM says:" in r.output["response"] for r in results)


class TestAdversarialConvergence:
    def test_basic_convergence(self):
        conv = AdversarialConvergence(n_attempts=2, m_refuters=1, max_rounds=2)

        def attempt_fn(framing: str, idx: int) -> list[VerificationClaim]:
            return [
                VerificationClaim(
                    claim_id=f"claim_{idx}",
                    content=f"Result for {framing}",
                    source_attempt=f"attempt_{idx}",
                    location="file.py:10",
                )
            ]

        def refuter_fn(claim: VerificationClaim, ref_idx: int) -> bool:
            # Accept all claims for this test
            return True

        def vary_framing_fn(q: str, idx: int) -> str:
            return f"{q} (attempt {idx})"

        result = conv.run("test question", attempt_fn, refuter_fn, vary_framing_fn)

        assert result.convergence_rounds <= 2
        assert len(result.verified_claims) == 2  # 2 attempts, both survived
        assert all(c.survived_refutation for c in result.verified_claims)

    def test_refutation_filters_claims(self):
        conv = AdversarialConvergence(n_attempts=3, m_refuters=2, max_rounds=1)

        def attempt_fn(framing: str, idx: int) -> list[VerificationClaim]:
            return [
                VerificationClaim(
                    claim_id=f"c{idx}",
                    content=f"Claim {idx}",
                    source_attempt=f"a{idx}",
                    # Only claim 0 carries a location. This used to be
                    # `if idx % 2 == 0`, which handed claim 2 a location too
                    # and so contradicted both the comment and the assertion
                    # below it -- the fixture has to actually exercise the
                    # require_location filter it claims to.
                    location="file.py:1" if idx == 0 else None,
                )
            ]

        def refuter_fn(claim: VerificationClaim, ref_idx: int) -> bool:
            return "Claim 1" not in claim.content  # Refute claim 1

        def vary_framing_fn(q: str, idx: int) -> str:
            return f"{q} {idx}"

        result = conv.run("q", attempt_fn, refuter_fn, vary_framing_fn)

        # Claim 1 refuted, claim 0 and 2 survive refutation but claim 2 has no
        # location and require_location is on, so only claim 0 survives.
        assert len(result.verified_claims) == 1, [
            c.content for c in result.verified_claims
        ]
        assert result.verified_claims[0].content == "Claim 0"


class TestDynamicWorkflow:
    def test_layer_a_only(self):
        class SimpleWorker(DeterministicWorker):
            def process_unit(self, unit: WorkUnit) -> WorkResult:
                return WorkResult(unit_id=unit.id, success=True, output=unit.payload)

        wf = DynamicWorkflow("test")
        units = [WorkUnit(id="1", payload={"x": 1}), WorkUnit(id="2", payload={"x": 2})]
        wf.add_layer_a(SimpleWorker(), units)

        result = wf.run_foreground(llm_call=lambda p: "unused")

        assert result.convergence_rounds == 0
        assert "SimpleWorker" in result.all_attempts[0]["results"]

    def test_build_codebase_sweep(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "a.py").write_text("def foo():\n    return 'secret'\n")
            (root / "b.py").write_text("def bar():\n    return 'public'\n")

            wf = build_codebase_sweep_workflow(
                root, "secret", n_verification_attempts=2
            )

            assert wf.name.startswith("sweep_")
            assert len(wf.layer_a_workers) == 1
            assert len(wf.layer_b_workers) == 1
            assert wf.verification is not None


if __name__ == "__main__":
    import pytest

    pytest.main([__file__, "-v"])


def test_missing_results_file_reports_per_unit(tmp_path):
    """The "No results file" path must report, not raise NameError.

    It referenced `u` with no `for u in units` around it, so the whole
    missing-results branch -- which is what you hit when a sandboxed worker
    dies without writing anything -- died with NameError instead.
    """

    class QuietWorker(DeterministicWorker):
        def process_unit(self, unit):
            raise RuntimeError("never runs")

    worker = QuietWorker()
    units = [WorkUnit(id="1", payload={}), WorkUnit(id="2", payload={})]

    # Neutralise the subprocess write so results.json is genuinely absent.
    worker._write_script = lambda u, d: tmp_path / "worker_none.py"  # type: ignore[method-assign]
    (tmp_path / "worker_none.py").write_text("raise SystemExit(0)")

    results = worker.execute_batch_sandboxed(units, tmp_path)

    assert [r.unit_id for r in results] == ["1", "2"]
    assert all(r.error == "No results file" for r in results), results
    assert not any(r.success for r in results)
