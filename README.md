# superpowers-owui

Open WebUI Tool port of [Superpowers](https://github.com/obra/superpowers) by Jesse Vincent.

Brings the brainstorm → spec → plan → execute agentic development workflow
to any Open WebUI installation using any OpenAI-compatible local or remote LLM.

## Credits

This tool is a port of [Superpowers](https://github.com/obra/superpowers) by
[Jesse Vincent (obra)](https://github.com/obra), used under MIT license.

The methodology, skill content, and workflow design are obra's work.
This port adapts them for the Open WebUI ecosystem.

---

## Installation

1. In Open WebUI, go to **Settings → Tools → Add Tool**
2. Paste the contents of `superpowers_tool.py`
3. Configure Valves:
   - `LLM_BASE_URL`: your endpoint (default: `http://localhost:1234/v1` for LM Studio)
   - `MODEL_NAME`: your model identifier (e.g. `lmstudio-community/Meta-Llama-3.1-8B-Instruct-GGUF`)
   - `API_KEY`: `lm-studio` for LM Studio, `ollama` for Ollama, or your real key for remote
4. Enable the tool in your model's settings

## Workflow

| Say this | What happens |
|----------|--------------|
| `brainstorm <idea>` | Starts brainstorming phase — one question at a time |
| `write the spec` | Saves spec document + runs automated reviewer |
| `write the plan` | Generates TDD implementation plan + runs automated reviewer |
| `execute task 1` | Injects TDD context for task 1, you control pacing |

### Phase markers

Every function embeds a `[SUPERPOWERS:PHASE:X]` tag in its output. These allow the
model (and you) to track where you are in the workflow:

| Marker | Meaning |
|--------|---------|
| `[SUPERPOWERS:PHASE:BRAINSTORMING]` | Clarifying questions in progress |
| `[SUPERPOWERS:PHASE:SPEC_REVIEW]` | Spec written, reviewer running |
| `[SUPERPOWERS:PHASE:PLAN_REVIEW]` | Plan written, reviewer running |
| `[SUPERPOWERS:PHASE:READY]` | Plan approved, ready to execute |
| `[SUPERPOWERS:PHASE:EXECUTING:TASK_N]` | Executing task N |

## Stack Compatibility

Tested with:
- LM Studio (default config, `http://localhost:1234/v1`)
- Ollama (`http://localhost:11434/v1`)
- Any OpenAI-compatible endpoint

## Valves Reference

| Valve | Default | Description |
|-------|---------|-------------|
| `LLM_BASE_URL` | `http://localhost:1234/v1` | Your OpenAI-compatible endpoint |
| `MODEL_NAME` | _(empty)_ | Model identifier. Empty = endpoint default |
| `REVIEWER_MODEL` | _(empty)_ | Separate model for reviewer passes. Falls back to MODEL_NAME |
| `SPEC_DIR` | `docs/superpowers/specs` | Where spec files are saved |
| `PLAN_DIR` | `docs/superpowers/plans` | Where plan files are saved |
| `API_KEY` | `lm-studio` | Auth key for your endpoint |

## How the reviewer simulation works

When `write_spec` or `write_plan` completes, the tool makes a **second isolated HTTP
completion call** to your endpoint — no conversation history, no chat context. Just the
document content and a reviewer system prompt. This approximates Superpowers' subagent
review loops without requiring native subagent support in Open WebUI.

Set `REVIEWER_MODEL` to a different (faster/cheaper) model to keep reviewer passes quick.

## Agent-Driven Mode (optional)

Add this to your model's system prompt to enable automatic workflow triggering without
explicit tool invocations:

```
When the user wants to build something, automatically invoke the superpowers brainstorm tool.
When brainstorming is complete and the user approves the design, invoke superpowers write_spec.
When the spec is approved, invoke superpowers write_plan.
When executing, invoke superpowers execute_task for each task individually.
Always follow the phase markers [SUPERPOWERS:PHASE:X] to track workflow state.
```

## File Output

Spec and plan documents are saved with datestamped filenames:
- Specs: `docs/superpowers/specs/YYYY-MM-DD-<topic>-design.md`
- Plans: `docs/superpowers/plans/YYYY-MM-DD-<feature>.md`

Directories are created automatically (`os.makedirs(exist_ok=True)`).
Paths are relative to wherever Open WebUI is running.
