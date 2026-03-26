# SuperPowersWUI

A structured agentic development workflow for [Open WebUI](https://github.com/open-webui/open-webui) — 
works with any local or remote LLM via any OpenAI-compatible endpoint.

Instead of asking your LLM to "write me a script" and hoping for the best, SuperPowersWUI 
walks it through a proper development process: brainstorm the design, write a reviewed spec, 
generate a TDD implementation plan, then execute task by task. Each phase is reviewed by a 
sub-agent before moving forward. You stay in control of the pace.
```
brainstorm → spec → review → plan → review → execute
```

> **Based on [Superpowers](https://github.com/obra/superpowers) by [Jesse Vincent (obra)](https://github.com/obra)** — MIT License  
> The methodology, workflow design, and skill content are obra's work. This tool ports 
> that workflow into Open WebUI so it runs against local and remote LLMs without requiring 
> Claude Code. If this has been useful to you, consider [sponsoring obra](https://github.com/sponsors/obra).

---

## What it does

You describe what you want to build. The workflow takes it from there:

1. **Brainstorm** — clarifies the design before any code is written
2. **Spec** — writes a structured requirement document, then runs an automated reviewer against it
3. **Plan** — generates a task-by-task TDD implementation plan, reviewed before execution
4. **Execute** — injects each task with full TDD context; you control the pace, one task at a time

Each phase produces a saved document (spec, plan, scratch build log) that persists across 
conversations when used with [Fileshed](https://github.com/Fade78/Fileshed).

---

## Requirements

- [Open WebUI](https://github.com/open-webui/open-webui) 0.4.0+
- Any OpenAI-compatible LLM endpoint (LM Studio, Ollama, or remote API)
- Native Function Calling enabled on your model

---

## Installation

1. In Open WebUI, go to **Workspace → Tools → Add Tool**
2. Paste the contents of `superpowers_tool.py`
3. Enable the tool on your model

That's it. The tool works out of the box with default valves pointing to LM Studio 
on `localhost:1234`. Adjust the valves if your setup differs.

### Fileshed Integration (Recommended)

[Fileshed](https://github.com/Fade78/Fileshed) by [Fade78](https://github.com/Fade78) 
is a companion Open WebUI tool that gives you a persistent file workspace. With it 
installed, your specs and plans survive across conversations and are browsable in the 
Fileshed Storage zone.

**Setup:**

1. Install Fileshed in Open WebUI alongside this tool
2. Make sure both tools have matching `STORAGE_BASE_PATH` valve values  
   (default: `/app/backend/data/user_files`)
3. `FILESHED_COMPATIBLE` is `True` by default — no changes needed

**Without Fileshed:** set `FILESHED_COMPATIBLE: False`. Files still write to disk 
but require direct filesystem access to retrieve. All workflow functionality remains intact.

---

## Usage

Start a conversation with your model and use natural language:

| Say this | What happens |
|---|---|
| `brainstorm <your idea>` | Kicks off the design phase |
| `cook` | Runs the workflow autonomously to completion |
| `ask` | Pauses at each phase for your approval before continuing |
| `write the spec` | Saves the agreed design as a spec document |
| `write the plan` | Generates a TDD implementation plan from the spec |
| `execute task 1` | Runs task 1 — call again with `task 2`, `task 3`, etc. |

Switch between `cook` and `ask` mode at any time during a session.

### Phase markers

The tool embeds phase markers in its output so you always know where you are:

| Marker | Meaning |
|---|---|
| `[SUPERPOWERS:PHASE:SPEC_REVIEW]` | Spec written, reviewer running |
| `[SUPERPOWERS:PHASE:PLAN_REVIEW]` | Plan written, reviewer running |
| `[SUPERPOWERS:PHASE:READY]` | Plan approved, ready to execute |
| `[SUPERPOWERS:PHASE:EXECUTING:TASK_N]` | Executing task N |
| `[SUPERPOWERS:TASK:COMPLETE]` | Task done, waiting for your call to continue |

---

## Valves Reference

| Valve | Default | Description |
|---|---|---|
| `STORAGE_BASE_PATH` | `/app/backend/data/user_files` | Base path for specs, plans, and scratch files |
| `FILESHED_COMPATIBLE` | `True` | Write files to Fileshed-compatible paths |
| `COMPLEXITY` | `simple` | Controls spec/plan verbosity: `simple`, `medium`, `complex` |
| `SPEC_DIR` | `specs` | Subdirectory under superpowers/ for spec files |
| `PLAN_DIR` | `plans` | Subdirectory under superpowers/ for plan files |
| `ENABLE_SHELLCHECK` | `True` | Run shellcheck on bash code blocks during validation |
| `ENABLE_ESLINT` | `False` | Run eslint on JS/TS code blocks during validation |
| `VALIDATION_TIMEOUT` | `5` | Timeout in seconds for external validator calls |

---

## SkillStack — Command Validation Cache

SuperPowersWUI includes a built-in command knowledge cache that validates generated 
code before writing it to the scratch file. It checks flags, subcommands, and known 
bad patterns against a curated knowledge base, with fallback to man pages and online docs.

Manage it during a session with the `skillstack` tool function:

| Command | What it does |
|---|---|
| `skillstack -l sftp 2` | Learn sftp from man pages |
| `skillstack -l Get-NetFirewallRule 2 powershell` | Learn a PowerShell cmdlet |
| `skillstack -i sftp` | Inspect cached knowledge for a command |
| `skillstack -s` | Show cache statistics |
| `skillstack -dump` | Dump full cache as JSON |
| `skillstack -dump sftp` | Dump a single command's cache entry |
| `skillstack -health` | Check cache file integrity |
| `skillstack -validate` | Validate code from the current conversation |

---

## File Output

| File type | Path |
|---|---|
| Specs | `superpowers/specs/YYYY-MM-DD-<topic>-design.md` |
| Plans | `superpowers/plans/YYYY-MM-DD-<feature>.md` |
| Scratch | `superpowers/scratch/<feature>.scratch` |

All paths are relative to `STORAGE_BASE_PATH`. Directories are created automatically.

---

## Credits

**[Superpowers](https://github.com/obra/superpowers)** — [Jesse Vincent (obra)](https://github.com/obra) — MIT License  
The methodology, workflow design, and skill content this tool is built on.

**[Fileshed](https://github.com/Fade78/Fileshed)** — [Fade78](https://github.com/Fade78) — MIT License  
Recommended companion tool for persistent file storage in Open WebUI.

**SuperPowersWUI** — [Chris Thompson (tkalevra)](https://github.com/tkalevra) — MIT License  
This port. Independently developed, not officially affiliated with either upstream project.
