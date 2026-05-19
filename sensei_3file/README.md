# sensei_3file — local 3-file harness for tiny brains

Built 2026-05-19 from the Gemini AI Mode brainstorm captured in the `planner bm` Google Doc.

## What this is

A minimum-viable harness that runs the 3-file external-scaffolding pattern
against `qwen2.5:3b` (or any Ollama model). The thesis:

> If you give a 3B model a strict file-sandbox and atomic instructions,
> it can execute a 50-step plan flawlessly. Every step is Step 1.

Three files live in each project workspace:

| File              | Purpose                                              |
|-------------------|------------------------------------------------------|
| `master_plan.md`  | The roadmap. Read-only to the executor. The PLANNER writes this. |
| `active_step.txt` | The current single task. Composed by the harness from the next unchecked step. |
| `system_log.txt`  | Append-only journal — what got done.                |

A fourth file appears only on failure: `stuck_step.txt` (debug dump after 3 retries).

## Files in this directory

```
sensei_3file/
├── orchestrator.py            # the harness — runs one step per invocation
├── sensei_executor_prompt.md  # verbatim system prompt for qwen2.5:3b
├── templates/                 # starter files for a new workspace
│   ├── master_plan.template.md
│   ├── active_step.template.txt
│   └── system_log.template.txt
└── README.md                  # this file
```

## Bootstrapping a new workspace

```bash
mkdir -p ~/projects/my-task && cd ~/projects/my-task
cp ~/scripts/sensei_3file/templates/master_plan.template.md  master_plan.md
cp ~/scripts/sensei_3file/templates/active_step.template.txt active_step.txt
cp ~/scripts/sensei_3file/templates/system_log.template.txt  system_log.txt
git init
```

Then fill out `master_plan.md` with your atomic steps (or generate it via the
"Plan Author" upstream prompt in `planner bm`).

## Running the harness

One step:

```bash
python3 ~/scripts/sensei_3file/orchestrator.py --workdir ~/projects/my-task
```

Multiple steps in a row:

```bash
python3 ~/scripts/sensei_3file/orchestrator.py --workdir ~/projects/my-task --max-steps 10
```

Different model:

```bash
python3 ~/scripts/sensei_3file/orchestrator.py --workdir ~/projects/my-task --model qwen2.5:7b
```

## What the orchestrator does per step

1. Parses `master_plan.md`, finds the next `- [ ]` step.
2. Composes `active_step.txt` with objective + target + verification + (optional) dependency block.
3. Calls Ollama HTTP API at `localhost:11434` with the executor prompt as system.
4. Parses the response for `FILE:` / `===BEGIN===` / `===END===` / `LOG:` block.
5. Writes the file. Runs the step's verification command.
6. On pass: ticks `- [ ]` → `- [x]` in master_plan, appends to system_log, clears active_step.
7. On fail: appends the error to active_step, retries (up to 3 by default).
8. On 3 failures: writes `stuck_step.txt` and halts.

## Master plan format

The orchestrator parses each step like this:

```markdown
- [ ] Step 1: Short title of the action
  - Context/Objective: One or two sentences of absolute clarity.
  - File to target: `path/relative/to/workdir/file.ext`
  - Verification: <bash command that exits 0 on pass, non-zero on fail>
  - Dependency: (optional) data from a prior step the executor needs verbatim
```

Keys recognised: `Context/Objective`, `File to target`, `Verification`, `Dependency`.

## Known limits (v0)

- Each step writes a **complete file**, not a diff. Best for create/replace, not surgical edits.
- No git auto-commit / auto-rollback yet (Gemini's design includes `git checkout .` on failure). To add.
- No "planner" component yet — `master_plan.md` is written by you (or by a stronger upstream model using the prompt in `planner bm`).
- Verification commands run in shell with `shell=True`. Don't put untrusted strings in `Verification:`.
