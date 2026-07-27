#!/usr/bin/env python3
"""Verify ~/scripts/schemas/browser_planner_v1.json.

Three checks:
  1. JSON parses
  2. Schema self-validates under Draft 2020-12
  3. Fixture suite — labelled valid examples must validate, labelled invalid
     examples must produce at least one error.

Exits 0 if all three pass, 1 otherwise.
"""
import json
import sys
from pathlib import Path

try:
    from jsonschema import Draft202012Validator, SchemaError
except ImportError:
    sys.stderr.write("FAIL: jsonschema not installed (pip3 install --user jsonschema)\n")
    sys.exit(1)

SCHEMA_PATH = Path("/home/user/scripts/schemas/browser_planner_v1.json")


def main() -> int:
    # 1. JSON parse
    try:
        schema = json.loads(SCHEMA_PATH.read_text())
        print("PASS  json parses")
    except json.JSONDecodeError as e:
        print(f"FAIL  json parse: {e}")
        return 1

    # 2. Schema self-validation
    try:
        Draft202012Validator.check_schema(schema)
        print("PASS  schema self-validates (Draft 2020-12)")
    except SchemaError as e:
        print(f"FAIL  schema invalid: {e.message} at {list(e.path)}")
        return 1

    # Helper: validate a payload against #/$defs/<name>
    defs = schema["$defs"]

    def validate(name: str, payload: dict) -> list[str]:
        sub = {"$ref": f"#/$defs/{name}", "$defs": defs}
        v = Draft202012Validator(sub)
        return [
            f"{'/'.join(str(p) for p in e.absolute_path) or '<root>'}: {e.message}"
            for e in v.iter_errors(payload)
        ]

    # 3. Fixture suite
    fixtures = [
        # (envelope_name, payload, expected_valid, description)
        (
            "PlannerStep",
            {
                "envelope": "planner_step",
                "step_id": "s1",
                "goal_summary": "Find the candidate resume in Drive",
                "executor_instruction": "Open the visible 'Resume Detailed.pdf' tile",
                "constraints": {"allowed_directive_types": ["BROWSER_CLICK"]},
                "expected_outcome": "PDF viewer opens",
                "rationale": {"reason": "File visible in page signature.", "uncertainty": "low"},
            },
            True,
            "minimal PlannerStep",
        ),
        (
            "PlannerStep",
            {
                "envelope": "planner_step",
                "step_id": "s2",
                "goal_summary": "Need user input",
                "executor_instruction": "Ask user which resume to use",
                "constraints": {"allowed_directive_types": ["BROWSER_READ"]},
                "expected_outcome": "user chooses",
                "rationale": {"reason": "Multiple resumes visible.", "uncertainty": "medium"},
                "needs_user_input": True,
                "user_question": None,
            },
            False,
            "needs_user_input=true with user_question=null (conditional violation)",
        ),
        (
            "PlannerStep",
            {
                "envelope": "planner_step",
                "step_id": "s3",
                "goal_summary": "Need user input",
                "executor_instruction": "Ask user which resume",
                "constraints": {"allowed_directive_types": ["BROWSER_READ"]},
                "expected_outcome": "user chooses",
                "rationale": {"reason": "Multiple visible.", "uncertainty": "medium"},
                "needs_user_input": True,
                "user_question": {
                    "prompt": "Which resume should I open?",
                    "options": ["Resume Detailed.pdf", "Resume Short.pdf"],
                },
            },
            True,
            "needs_user_input=true with valid user_question",
        ),
        (
            "ExecutorDirective",
            {
                "envelope": "executor_directive",
                "step_id": "s1",
                "type": "BROWSER_NAV",
                "args": {"url": "https://example.com"},
                "synthesized_from": {"planner_step_id": "s1", "executor_model": "fast_lane"},
            },
            True,
            "BROWSER_NAV valid https URL",
        ),
        (
            "ExecutorDirective",
            {
                "envelope": "executor_directive",
                "step_id": "s1",
                "type": "BROWSER_NAV",
                "args": {"url": "javascript:alert(1)"},
                "synthesized_from": {"planner_step_id": "s1", "executor_model": "fast_lane"},
            },
            False,
            "BROWSER_NAV with javascript: URL must be blocked by ^https?:// pattern",
        ),
        (
            "ExecutorDirective",
            {
                "envelope": "executor_directive",
                "step_id": "s1",
                "type": "BROWSER_NAV",
                "args": {"url": "file:///etc/passwd"},
                "synthesized_from": {"planner_step_id": "s1", "executor_model": "fast_lane"},
            },
            False,
            "BROWSER_NAV with file:// URL must be blocked",
        ),
        (
            "ExecutorDirective",
            {
                "envelope": "executor_directive",
                "step_id": "s1",
                "type": "BROWSER_CLICK",
                "args": {},
                "synthesized_from": {"planner_step_id": "s1", "executor_model": "fast_lane"},
            },
            False,
            "BROWSER_CLICK with no selector/text/role must fail anyOf",
        ),
        (
            "ExecutorDirective",
            {
                "envelope": "executor_directive",
                "step_id": "s1",
                "type": "BROWSER_CLICK",
                "args": {"text": "Sign in"},
                "synthesized_from": {"planner_step_id": "s1", "executor_model": "fast_lane"},
            },
            True,
            "BROWSER_CLICK with text only is valid",
        ),
        (
            "ExecutorDirective",
            {
                "envelope": "executor_directive",
                "step_id": "s1",
                "type": "BROWSER_FILL",
                "args": {"text": ""},
                "synthesized_from": {"planner_step_id": "s1", "executor_model": "fast_lane"},
            },
            False,
            "BROWSER_FILL with empty text must fail minLength: 1",
        ),
        (
            "ExecutorDirective",
            {
                "envelope": "executor_directive",
                "step_id": "s1",
                "type": "BROWSER_SCREENSHOT",
                "args": {"scope": "element"},
                "synthesized_from": {"planner_step_id": "s1", "executor_model": "fast_lane"},
            },
            False,
            "BROWSER_SCREENSHOT scope=element without selector must fail conditional",
        ),
        (
            "ExecutorDirective",
            {
                "envelope": "executor_directive",
                "step_id": "s1",
                "type": "BROWSER_SCREENSHOT",
                "args": {"scope": "viewport"},
                "synthesized_from": {"planner_step_id": "s1", "executor_model": "fast_lane"},
            },
            True,
            "BROWSER_SCREENSHOT scope=viewport without selector is valid",
        ),
        (
            "ExecutorDirective",
            {
                "envelope": "executor_directive",
                "step_id": "s1",
                "type": "BROWSER_WAIT",
                "args": {"condition": "selector_visible"},
                "synthesized_from": {"planner_step_id": "s1", "executor_model": "fast_lane"},
            },
            False,
            "BROWSER_WAIT selector_visible without selector must fail conditional",
        ),
        (
            "ExecutorDirective",
            {
                "envelope": "executor_directive",
                "step_id": "s1",
                "type": "BROWSER_WAIT",
                "args": {"condition": "text_present", "text": "Loaded"},
                "synthesized_from": {"planner_step_id": "s1", "executor_model": "fast_lane"},
            },
            True,
            "BROWSER_WAIT text_present with text is valid",
        ),
        (
            "ExecutorDirective",
            {
                "envelope": "executor_directive",
                "step_id": "s1",
                "type": "BROWSER_SCROLL",
                "args": {"direction": "top", "amount": 500},
                "synthesized_from": {"planner_step_id": "s1", "executor_model": "fast_lane"},
            },
            False,
            "BROWSER_SCROLL direction=top with amount must fail (top forbids amount)",
        ),
        (
            "ExecutorDirective",
            {
                "envelope": "executor_directive",
                "step_id": "s1",
                "type": "BROWSER_SCROLL",
                "args": {"direction": "into_view", "selector": "#footer"},
                "synthesized_from": {"planner_step_id": "s1", "executor_model": "fast_lane"},
            },
            True,
            "BROWSER_SCROLL into_view with selector is valid",
        ),
        (
            "ExecutorDirective",
            {
                "envelope": "executor_directive",
                "step_id": "s1",
                "type": "BROWSER_KEY",
                "args": {"key": "Enter"},
                "synthesized_from": {"planner_step_id": "s1", "executor_model": "fast_lane"},
            },
            True,
            "BROWSER_KEY Enter is valid",
        ),
        (
            "ExecutorDirective",
            {
                "envelope": "executor_directive",
                "step_id": "s1",
                "type": "BROWSER_KEY",
                "args": {"key": "a"},
                "synthesized_from": {"planner_step_id": "s1", "executor_model": "fast_lane"},
            },
            False,
            "BROWSER_KEY 'a' (typed character) must fail enum",
        ),
        (
            "ExecutorObservation",
            {
                "envelope": "executor_observation",
                "step_id": "s1",
                "status": "ok",
                "directive_emitted": {
                    "envelope": "executor_directive",
                    "step_id": "s1",
                    "type": "BROWSER_READ",
                    "args": {},
                    "synthesized_from": {"planner_step_id": "s1", "executor_model": "fast_lane"},
                },
                "observation": "Read 320 chars from page",
                "error_class": "selector_not_found",
                "page_signature": {"url": "https://example.com", "title": "Example"},
            },
            False,
            "ExecutorObservation status=ok with non-null error_class must fail consistency",
        ),
        (
            "ExecutorObservation",
            {
                "envelope": "executor_observation",
                "step_id": "s1",
                "status": "ok",
                "directive_emitted": {
                    "envelope": "executor_directive",
                    "step_id": "s1",
                    "type": "BROWSER_READ",
                    "args": {},
                    "synthesized_from": {"planner_step_id": "s1", "executor_model": "fast_lane"},
                },
                "observation": "Read 320 chars from page",
                "error_class": None,
                "page_signature": {"url": "https://example.com", "title": "Example"},
            },
            True,
            "ExecutorObservation status=ok with error_class=null is valid",
        ),
        (
            "ExecutorObservation",
            {
                "envelope": "executor_observation",
                "step_id": "s1",
                "status": "soft_fail",
                "directive_emitted": {
                    "envelope": "executor_directive",
                    "step_id": "s1",
                    "type": "BROWSER_CLICK",
                    "args": {"selector": "#missing"},
                    "synthesized_from": {"planner_step_id": "s1", "executor_model": "fast_lane"},
                },
                "observation": "Selector not found",
                "error_class": None,
                "page_signature": {"url": "https://example.com", "title": "Example"},
            },
            False,
            "ExecutorObservation status=soft_fail with error_class=null must fail consistency",
        ),
        (
            "PlannerDone",
            {
                "envelope": "planner_done",
                "step_id": "s5",
                "summary": "Application submitted",
                "evidence": [{"source": "page_signature", "locator": "Submission confirmation page"}],
                "user_visible": "I submitted your application to Acme.",
            },
            True,
            "PlannerDone minimal valid",
        ),
        (
            "PlannerDone",
            {
                "envelope": "planner_done",
                "step_id": "s5",
                "summary": "Done",
                "evidence": [],
                "user_visible": "Done.",
            },
            False,
            "PlannerDone with empty evidence array must fail minItems: 1",
        ),
        (
            "PlannerReplan",
            {
                "envelope": "planner_replan",
                "step_id": "s4",
                "reason": "Page changed mid-step, need to re-read.",
                "triggered_by": "page_state_changed",
            },
            True,
            "PlannerReplan minimal valid",
        ),
        (
            "BrowserTaskState",
            {
                "task_id": "tabcd1234",
                "goal": "Apply for the field tech job on Indeed",
                "history": [],
                "current_planner": "fast_lane",
                "round_count": 0,
                "status": "active",
            },
            True,
            "BrowserTaskState minimal valid",
        ),
    ]

    passed = 0
    failed = 0
    fail_details = []
    for name, payload, expected_valid, desc in fixtures:
        errors = validate(name, payload)
        actual_valid = len(errors) == 0
        if actual_valid == expected_valid:
            passed += 1
            print(f"PASS  [{name:20s}] {desc}")
        else:
            failed += 1
            if expected_valid:
                fail_details.append(f"  Expected VALID but got errors:\n    " + "\n    ".join(errors[:3]))
                print(f"FAIL  [{name:20s}] {desc}")
            else:
                fail_details.append("  Expected INVALID but validated cleanly")
                print(f"FAIL  [{name:20s}] {desc}")
            print(fail_details[-1])

    print()
    print(f"Fixture summary: {passed} PASS / {failed} FAIL out of {len(fixtures)}")
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
