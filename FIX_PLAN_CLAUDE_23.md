# Fix Plan — 23 Non-Environmental Test Failures (Claude pass)

Date: 2026-08-11 (session 2)

Investigated each of the 7 named files against the actual current source
(not assumptions from the delegation brief — several diagnoses differ from
the brief's guess once the real failure trace was read). Verification
command for all of them:

```bash
MCLI_SKIP_ENV_TESTS=1 python3 -m pytest <file> -q --no-header --tb=short
```

---

## 1. test_router_golden.py (7 failures) — TEST FIX, not source

**Diagnosis:** Not a regression. Commit `aad2762` ("Rip synth-route
workarounds") deliberately deleted the old `_system_query_short_circuit`
and `_weather_short_circuit` route families (weather especially — it
shelled out to `wttr.in`, against the local-first philosophy). A later
commit (`953f31a`, "add RUN_SKILL bridge and deterministic routes")
reintroduced equivalent port/where-is/is-running/is-installed detection
under a new name, `deterministic_intent`, and a separate `acknowledgment`
route was added for pure chat acks ("thanks") in commit `555cc09`. Both are
real, intentional, currently-dispatched routes (`orchestrate()` returns
them, `handle()` dispatches them, harvest recording is correctly wired for
`task_type="deterministic"`). `weather` has no replacement — it's gone for
good, so "what's the weather" now falls through to a normal `local` chat
turn. The golden test file was never updated after either change.

**Fix:** Update the golden pins to the current, intentional route names —
do not resurrect deleted `weather`/`system_query` route strings in source,
that would contradict the operator's own 2026-05-16 architecture decision.

- `ChatRouting.test_thanks_routes_local` → expect `"acknowledgment"` (checks
  `response` key instead of `model`, matching the actual acknowledgment
  decision shape).
- `SystemQueryRouting.*` (4 tests) → expect `"deterministic_intent"`
  instead of `"system_query"`; `synth_reply` assertions unchanged (still
  present on this route).
- `WeatherRouting.test_whats_the_weather_short_circuits` → expect
  `"local"`; drop the now-inapplicable `synth_reply` assertion.
- `HarvestRecordedOnDeterministicShortCircuit` → source-pin check now looks
  for `'deterministic_intent'` in `handle()`'s source instead of the
  deleted `'system_query'` string.

**Verify:** `MCLI_SKIP_ENV_TESTS=1 python3 -m pytest test_router_golden.py -q`

---

## 2. test_browser_screenshot_parser.py (6 failures) — SOURCE BUG

**Diagnosis:** `_fallback_action()` already defaults bare
`BROWSER_SCREENSHOT` to `target="viewport"` correctly, and
`typed_actions.parse_directive()` does too — those unit tests pass. The
failures are all in `ApiParseActionsTests`, which go through
`stt_server._api_parse_actions()`. Its inner `add()` closure has:

```python
if chrome_extension:
    action["_5wh"] = _synthesize_5wh(...)
```

`chrome_extension` is not a parameter, local, or global anywhere in
`stt_server.py` — it's a `NameError` from day one (commit `57e68abe`, which
meant to check `source == "chrome_extension"` per its own commit message
and the docstring right above `_synthesize_5wh`). Because the typed-actions
call site wraps `add()` in a bare `try/except: pass`, the crash is silently
swallowed — but only *after* `add()` already did `seen.add(key)`. The
second (regex fallback) parse loop then sees the same `(kind, target)` key
already in `seen` and silently no-ops. Net effect: **every** action parsed
through `_api_parse_actions` — not just screenshots — was silently
dropped. This is a real, load-bearing bug, not test staleness.

**Fix:** `stt_server.py` — `if chrome_extension:` → `if source ==
"chrome_extension":` (`source` is already an `_api_parse_actions`
parameter in scope).

**Verify:** `MCLI_SKIP_ENV_TESTS=1 python3 -m pytest test_browser_screenshot_parser.py -q`

---

## 3. test_api_handle_wedge.py (2 failures) — TEST FIX

**Diagnosis:** The wedge-protection redesign (2026-05-14, same day as the
test) replaced a single `_API_HANDLE_LOCK` with a per-lane
`_API_HANDLE_LOCKS` dict (`{"local", "cloud_fast", "cloud_deep", "cloud",
"cloud_vision", "router"}`) specifically so cloud-lane traffic stops
queuing behind local Ollama inference — a real improvement documented in
the comment above the dict. The test file wasn't updated for the rename.
`_predict_api_handle_lane([], "hello", requested_model="")` resolves to
`"local"` in this repo (confirmed directly), matching the test's payload
(`prompt="hello"`, no model prefix).

**Fix:** Test file — both call sites of `stt_server._API_HANDLE_LOCK` →
`stt_server._API_HANDLE_LOCKS["local"]`.

**Verify:** `MCLI_SKIP_ENV_TESTS=1 python3 -m pytest test_api_handle_wedge.py -q`

---

## 4. test_auto_extract_lesson.py (1 failure) — TEST FIX

**Diagnosis:** `CodexFindingsRegressionGuard.test_hooks_repl_command_exists`
hardcodes `open("/home/user/scripts/master_ai.py")`. That path doesn't
exist on this machine (user is `elijah`, and this repo — not `~/scripts` —
is the canonical copy being tested). `master_ai` is already imported at
the top of the test file.

**Fix:** Test file — `open("/home/user/scripts/master_ai.py")` →
`open(master_ai.__file__)`.

**Verify:** `MCLI_SKIP_ENV_TESTS=1 python3 -m pytest test_auto_extract_lesson.py -q`

---

## 5. test_master_ai_parser.py (3 failures + 1 failing subtest) — MIXED

Two different root causes:

### 5a. Hardcoded `/home/user/...` paths (3 tests) — TEST FIX

`test_auto_context_codex_md_alias_reads_claude_handoff`,
`test_local_read_target_resolves_codex_possessive_md`, and
`test_local_read_target_resolves_codex_memory_alias` all assert an exact
literal string `/home/user/scripts/CLAUDE.md`. `_resolve_local_text_target`
and `auto_inject_context` already resolve paths correctly via
`Path.home()` and a `search_dirs` list that includes both `~/scripts` and
`cwd` — on this machine (`~/scripts` doesn't exist as a deployed copy of
this repo, so resolution correctly falls through to `cwd`, i.e. this
project directory) the real resolved file is
`.../master-ai-cli/CLAUDE.md`, not `/home/user/scripts/CLAUDE.md`. The
literal path was never portable past whatever machine/username first wrote
the test.

**Fix:** Test file — relax the three assertions to check the *resolved
file's basename is `CLAUDE.md`* (and that it exists), instead of a
hardcoded absolute path tied to one username and directory layout.

### 5b. `desktop_launch` leaks through the chrome-extension automation gate — SOURCE BUG

`test_chrome_extension_page_context_skips_pre_model_shortcircuits`
subtest `prompt='open hypnotix'` fails: `orchestrate()` returns
`route="desktop_launch"` instead of a model-bearing route. `orchestrate()`
already gates the ack / deterministic-intent / fast-classifier
short-circuits behind `if not _is_chrome_ext_automation:` (explicit
comment: "Pre-model short-circuits are disabled for chrome_extension
automation turns... those turns must reach a model-bearing route"), but
the desktop-app-launch short-circuit a few lines further down
(`_desktop_launch_short_circuit`) has no such guard — it fires
unconditionally whenever there's no Groq key (the earlier
`_is_chrome_ext_automation and have_groq` branch only intercepts the
Groq-key case). This is an inconsistency with the documented intent of the
surrounding code, not intentional.

**Fix:** `master_ai.py` — wrap the desktop-launch short-circuit block in
`if not _is_chrome_ext_automation:`, matching the other pre-model
short-circuits.

**Verify:** `MCLI_SKIP_ENV_TESTS=1 python3 -m pytest test_master_ai_parser.py -q`

---

## 6. test_phase7_10_tools.py (3 failures) — TEST FIX (environment shadowing, two layers)

**Diagnosis:** Not a bug in `subagents/find.py`, `subagents/
workflow_describer.py`, or `stt_server._tool_find` /
`_tool_describe_step` — all three already produce the exact shapes the
test expects. Two independent environment-shadowing problems on this dev
machine, both from an unrelated, older, live-deployed Fair Chance
job-application stack that happens to also live under `~/scripts`:

1. **Module shadowing.** This test file does
   `sys.path.insert(0, os.path.expanduser("~/scripts"))`, and
   `~/scripts/subagent_registry.py` is that unrelated stack's own registry
   module (different shape — `list_agents` instead of `list_subagents`,
   etc.). Because `~/scripts` is inserted ahead of this repo's own
   directory, `import subagent_registry` picks up the wrong module, and
   because Python caches `sys.modules["subagent_registry"]` process-wide,
   the corruption also silently affects `stt_server.py`'s internal
   `import subagent_registry as _sr` calls inside `_tool_find` /
   `_tool_describe_step`. Fixed by unconditionally inserting this file's
   own directory at `sys.path[0]` (an `if ... not in sys.path` guard isn't
   enough — pytest's own rootdir insertion already puts the repo dir
   *somewhere* in `sys.path`, just not at the front, so the guard was a
   no-op) and evicting any stale cached `subagent_registry` module first.

2. **Discovery-path shadowing.** Even with the correct module imported,
   `subagent_registry.discover()`'s default `SUBAGENTS_DIR` is
   `~/scripts/subagents` — and that directory genuinely exists on this
   machine too, containing the *other* stack's subagents
   (`profile_fetcher`, `posting_inspector`, `application_logger`). Because
   it exists, `discover()`'s fallback to this repo's own `subagents/`
   directory (`Path(__file__).parent / "subagents"`, which only triggers
   when the primary path is *missing*) never fires — so `find` and
   `workflow_describer` are never registered at all.

`test_subagent_registry.py` (the dedicated test for this module) already
carries fixes for both problems — cache eviction plus an explicit
`sr.discover(Path(__file__).parent / "subagents")` call inside its own
test methods. Neither pattern had been propagated to
`test_phase7_10_tools.py` when it was written.

**Fix, first pass:** Test file — mirror `test_subagent_registry.py`'s
guards: insert this file's own directory onto `sys.path` ahead of
`~/scripts` (unconditionally, not gated on membership — the original
`if str(REPO_ROOT) not in sys.path` guard in *both* files was itself a
no-op once pytest's own rootdir insertion had already put REPO_ROOT
*somewhere* in `sys.path`, just not at the front), evict
`sys.modules["subagent_registry"]` before importing, and explicitly call
`sr.discover(REPO_ROOT / "subagents")` after import.

**Third layer found only under the full suite:** with only the above
fix, all 7 tests passed running the file alone, but 2 of 3 phase7/10
tests still failed inside the full-suite run. Root cause: pytest collects
(imports) *every* test module before executing any test. Because
`test_subagent_registry.py` sorts after `test_phase7_10_tools.py`
alphabetically, its own (now-also-fixed) `del sys.modules[...]` guard
still runs *during collection* and creates a **second**, fresh
`subagent_registry` module object — with its own empty `_REGISTRY` that
only gets `find`/`workflow_describer` added once *its* test methods
execute later. `stt_server._tool_find` / `_tool_describe_step` re-resolve
`import subagent_registry as _sr` at *call time*, so by the time
`test_phase7_10_tools.py`'s tests run (earlier in execution order than
`test_subagent_registry.py`'s), they see that second module's registry
still short two entries — a real cross-file ordering hazard, not
something a sys.path fix alone can close.

**Final fix:** `test_phase7_10_tools.py` — added a `setUp()` to
`SemanticFindTests` and `WorkflowDescribeTests` that re-runs
`sys.modules["subagent_registry"].discover(REPO_ROOT / "subagents")`
immediately before each test, against whatever module object is
*currently* live rather than the one captured at this file's own
collection time. `discover()` is additive/idempotent, so this is safe to
re-run and makes the outcome independent of inter-file collection order.
Applied the same unconditional-insert fix to `test_subagent_registry.py`
too, since it carried the identical latent no-op guard.

**Verify:** `MCLI_SKIP_ENV_TESTS=1 python3 -m pytest test_phase7_10_tools.py -q`

---

## 7. test_read_fence.py (1 failure) — SOURCE BUG (defensive hardening)

**Diagnosis:** `test_master_ai_keys_denied` expects
`_read_path_ok(~/.master_ai_keys)` to be denied. On this machine
`~/.master_ai_keys` is a **symlink** to
`~/Desktop/Projects/keychain/master_ai_keys` (see memory
`project_claf_keys_symlink_fix`) — note the real file has no leading dot.
`_read_path_ok` resolves symlinks (`Path.resolve()`) and only checks the
**resolved** path against the secret-path denylist, so the resolved name
`.../keychain/master_ai_keys` doesn't match the `\.master_ai_keys$`
pattern, and it's still under `$HOME` so the allowed-roots check also
passes. The fence has a real gap: a secret-named symlink pointing at a
differently-named real file bypasses the denylist entirely, on any
machine, not just this one.

**Fix:** `master_ai.py` — `_read_path_ok` now checks the deny patterns
against **both** the originally-requested path and the resolved real path
(the resolved path is still what's used for the allowed-roots / symlink-
escape check, so that protection is unchanged — this only adds coverage,
never removes it).

**Verify:** `MCLI_SKIP_ENV_TESTS=1 python3 -m pytest test_read_fence.py -q`

---

## Summary of source changes (non-test files)

| File | Change |
|---|---|
| `stt_server.py` | `if chrome_extension:` → `if source == "chrome_extension":` in `_api_parse_actions`/`add()` |
| `master_ai.py` | `_read_path_ok`: deny-pattern check now also covers the pre-resolve requested path |
| `master_ai.py` | `orchestrate()`: desktop-launch short-circuit gated behind `not _is_chrome_ext_automation` |

## Summary of test-file changes

| File | Change |
|---|---|
| `test_router_golden.py` | Route-name pins updated: `system_query`→`deterministic_intent`, `thanks`→`acknowledgment`, weather→`local` (feature removed) |
| `test_api_handle_wedge.py` | `_API_HANDLE_LOCK` → `_API_HANDLE_LOCKS["local"]` |
| `test_auto_extract_lesson.py` | Hardcoded path → `master_ai.__file__` |
| `test_master_ai_parser.py` | Hardcoded `/home/user/...` assertions relaxed to basename/existence checks |
| `test_phase7_10_tools.py` | Added REPO_ROOT-first sys.path guard + `subagent_registry` cache eviction + explicit repo-dir `discover()` call in `setUp()` (survives cross-file collection ordering) |
| `test_subagent_registry.py` | Fixed the same latent `if not in sys.path` no-op guard (unconditional insert now) |

## Final verification

```bash
MCLI_SKIP_ENV_TESTS=1 python3 -m pytest -q --no-header --tb=no 2>&1 | tail -5
```
