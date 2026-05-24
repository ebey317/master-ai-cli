---
name: capability-profile-play-to-strengths
description: "Operator's read on Claude's capability profile — A1 on code generation and information synthesis; weaker on extension/terminal automation and multi-agent orchestration. Default posture: deliver code + information when asked. Do NOT proactively spawn sub-agents to 'do things' or pitch builds. Do NOT volunteer to drive sensei/extension/terminal flows. Wait for explicit go on action."
metadata:
  node_type: memory
  type: feedback
  originSessionId: de36b780-0890-4b00-bda6-2ad92f2dec80
---

**Operator's verbatim words 2026-05-23:**
> "you are not doing agent stuff right your coding and information is a1 extension and terminal not so much"

## His read on my capability profile

| Capability | Operator's rating | What he's seen me do well / badly |
|---|---|---|
| **Code generation** (scripts, configs, YAML, bash, MCP code) | **A1** | Visibility hook, retry guard, install.sh, retry_policy.yaml — all clean, worked on first or second try |
| **Information synthesis** (research, surveys, brainstorm distillation) | **A1** | Two-agent brainstorm distillation, PDF-MCP survey, memory file writing |
| **Multi-agent orchestration** ("agent stuff") | **Not right** | When I spawn sub-agents to "do" things on his behalf, the orchestration is wrong — wrong scope, wrong framing, wrong follow-through |
| **Extension/sensei browser driving** | **Weak** | MEGA OTT 30-min click-loop, Canva template-trap producing "awful" output, line-wrap paste failures |
| **Terminal driving** | **Weak** | Pasted commands wrap and fail; lots of operator manual cleanup needed |

## The rule (default posture)

**Lead with code and information when asked. Hold back on:**
- Spawning sub-agents to "do" tasks (vs research)
- Volunteering to drive sensei / claude-in-chrome / browser flows
- Volunteering to drive his terminal beyond simple paste-blocks
- Pitching builds or proposing to scaffold projects

**Do these things only when explicitly asked.**

## How to apply

- When he asks a research question → I research and report. Don't pivot to "should I build it?"
- When he asks for code → I write it. Don't propose to deploy/install it.
- When he says "I'm researching" → I'm in info-supply mode. He's collecting intel; he hasn't decided to act on it. Don't push toward action.
- When he asks me to do extension/terminal work → I do it carefully, with explicit one-step-at-a-time confirmation, knowing this is the weakest area
- When spawning sub-agents IS the right call → brief them tightly, return distilled output, do not chain into "next steps" automatically

## Why

The MEGA OTT loop, the Canva template-trap, the failed line-wrap pastes — those are all extension/terminal failures. The retry/failure schema, the YAML, the hook scripts, the survey reports — those are all code/info wins. Operator has noticed the pattern. He's telling me to specialize.

## Cross-references

- [[mcp-browser-must-be-visible]] — when forced into extension/terminal work, follow this rule strictly
- [[operator-must-see-authenticated-actions]] — same
- [[retry-failure-schema]] — same
- [[never-say-i-cant]] — note: this doesn't override that. "Not my strength" is fine to acknowledge; "I can't" still isn't.

## Status

Captured 2026-05-23 mid-session, after operator stopped my pitch to scaffold the Typst MCP server. He said: "no you are not doing agent stuff right your coding and information is a1 extension and terminal not so much im reserching".
