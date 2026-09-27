"""
Tests for Dynamic Workflow Orchestration.
"""

import json
import tempfile
from pathlib import Path

from scripts.workflow_orchestrator import (
    WorkUnit,
    WorkResult,
    VerificationClaim,
    DeterministicWorker,
    JudgmentWorker,
    AdversarialConvergence,
    DynamicWorkflow,
    FileAnalysisWorker,
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
        class SandboxWorker(DeterministicWorker):
            def process_unit(self, unit: WorkUnit) -> WorkResult:
                # This runs in a separate process
                return WorkResult(
                    unit_id=unit.id,
                    success=True,
                    output={"pid": __import__("os").getpid()},
                )

        worker = SandboxWorker()
        units = [WorkUnit(id="1", payload={})]

        with tempfile.TemporaryDirectory() as tmp:
            results = worker.execute_batch_sandboxed(units, Path(tmp))

        assert len(results) == 1
        assert results[0].success


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
                    location="file.py:1" if idx % 2 == 0 else None,  # Only even have location
                )
            ]

        def refuter_fn(claim: VerificationClaim, ref_idx: int) -> bool:
            return "Claim 1" not in claim.content  # Refute claim 1

        def vary_framing_fn(q: str, idx: int) -> str:
            return f"{q} {idx}"

        result = conv.run("q", attempt_fn, refuter_fn, vary_framing_fn)

        # Claim 1 refuted, claim 0 and 2 survive but claim 2 has no location (require_location=True)
        # So only claim 0 should survive
        assert len(result.verified_claims) == 1
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

            wf = build_codebase_sweep_workflow(root, "secret", n_verification_attempts=2)

            assert wf.name.startswith("sweep_")
            assert len(wf.layer_a_workers) == 1
            assert len(wf.layer_b_workers) == 1
            assert wf.verification is not None


if __name__ == "__main__":
    import pytest
    pytest.main([__file__, "-v"])
