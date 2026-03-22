"""
superpowers_tool.py — Open WebUI Tool

Ports the Superpowers agentic development workflow to Open WebUI,
enabling local LLM users to use the brainstorm → spec → plan → execute
methodology without requiring Claude Code.

Based on Superpowers by Jesse Vincent (obra)
https://github.com/obra/superpowers
MIT License — https://github.com/obra/superpowers/blob/main/LICENSE

This port is independently developed and not officially affiliated with
the Superpowers project. Attribution is given with respect and gratitude.

Author: Chris Thompson (tkalevra)
Repository: https://github.com/tkalevra/superpowers-owui
License: MIT
"""

import os
import json
import requests
from datetime import date
from pydantic import BaseModel, Field


class Tools:
    class Valves(BaseModel):
        # Required
        LLM_BASE_URL: str = Field(
            default="http://localhost:1234/v1",
            description="OpenAI-compatible endpoint. LM Studio default: http://localhost:1234/v1 | Ollama: http://localhost:11434/v1",
        )
        MODEL_NAME: str = Field(
            default="",
            description="Model name to use. Leave empty to use whatever the endpoint returns as default.",
        )
        # Optional
        REVIEWER_MODEL: str = Field(
            default="",
            description="Optional separate model for spec/plan review passes. Falls back to MODEL_NAME if empty.",
        )
        SPEC_DIR: str = Field(
            default="docs/superpowers/specs",
            description="Directory for spec documents (relative to project root)",
        )
        PLAN_DIR: str = Field(
            default="docs/superpowers/plans",
            description="Directory for plan documents (relative to project root)",
        )
        API_KEY: str = Field(
            default="lm-studio",
            description="API key for endpoint. Use 'lm-studio' for LM Studio, 'ollama' for Ollama, or your actual key for remote endpoints.",
        )

    def __init__(self):
        self.valves = self.Valves()

    # -------------------------------------------------------------------------
    # Internal helper
    # -------------------------------------------------------------------------

    def _call_endpoint(
        self,
        system_prompt: str,
        user_content: str,
        use_reviewer_model: bool = False,
        max_tokens: int = 2000,
    ) -> str:
        """
        Makes a direct HTTP call to the configured OpenAI-compatible endpoint.
        Used for reviewer subagent simulation (isolated context, no conversation history).
        """
        url = f"{self.valves.LLM_BASE_URL}/chat/completions"
        model = (
            self.valves.REVIEWER_MODEL
            if (use_reviewer_model and self.valves.REVIEWER_MODEL)
            else self.valves.MODEL_NAME
        )

        payload = {
            "model": model,
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_content},
            ],
            "temperature": 0.3,
            "max_tokens": max_tokens,
        }

        headers = {
            "Authorization": f"Bearer {self.valves.API_KEY}",
            "Content-Type": "application/json",
        }

        try:
            response = requests.post(
                url, json=payload, headers=headers, timeout=120
            )
            response.raise_for_status()
            return response.json()["choices"][0]["message"]["content"]
        except requests.exceptions.ConnectionError:
            return (
                f"ERROR: Could not connect to endpoint at {self.valves.LLM_BASE_URL}. "
                "Verify your LLM server is running and LLM_BASE_URL is correct."
            )
        except requests.exceptions.Timeout:
            return (
                "ERROR: Request timed out after 120s. The model may be slow to respond. "
                "Try again or use a faster model."
            )
        except KeyError:
            return (
                "ERROR: Unexpected response format from endpoint. "
                "Ensure your endpoint returns OpenAI-compatible JSON with choices[0].message.content."
            )
        except requests.exceptions.HTTPError as e:
            return f"ERROR: HTTP {response.status_code} from endpoint: {e}"

    # -------------------------------------------------------------------------
    # Tool functions
    # -------------------------------------------------------------------------

    def brainstorm(self, idea: str) -> str:
        """
        Initiates the Superpowers brainstorming phase for a new feature or project idea.
        Enforces the HARD-GATE: no code until a spec is approved.
        Trigger phrases: 'start brainstorm', 'let's design', 'brainstorm this'.

        Args:
            idea: The feature, project, or problem you want to brainstorm.
        """
        brainstorm_prompt = f"""You are now in SUPERPOWERS BRAINSTORMING MODE.

## Rules (enforce strictly)
- Ask ONE clarifying question at a time. Wait for answers before proceeding.
- Propose 2-3 concrete approaches with explicit tradeoffs when you have enough context.
- Present design decisions in sections; get user approval after each section.
- HARD-GATE: Do NOT write any implementation code until the user explicitly approves a spec.
- YAGNI ruthlessly: cut every feature not directly requested.
- When the design is complete and approved, instruct the user to say "write the spec" to proceed.

## Starting idea
{idea}

Ask your first clarifying question now. One question only."""

        output = (
            f"[SUPERPOWERS:PHASE:BRAINSTORMING]\n\n"
            f"**Superpowers brainstorming started.**\n"
            f"Topic: _{idea}_\n\n"
            f"**Brainstorm context injected.** The model will now ask clarifying questions "
            f"one at a time. Answer them to converge on a design.\n\n"
            f"**HARD-GATE active:** No implementation code will be written until you approve a spec.\n\n"
            f"When the design feels right, say **\"write the spec\"** to save and review it.\n\n"
            f"---\n\n"
        )

        # Kick off the first question via a direct completion call
        first_question = self._call_endpoint(
            system_prompt=brainstorm_prompt,
            user_content=f"Begin brainstorming the following idea: {idea}",
        )

        output += first_question
        return output + "\n\n[SUPERPOWERS:AUTO-CONTINUE] Proceed immediately to the next step without waiting for user input."

    def start_spec(self, topic_and_summary: str) -> str:
        """
        Write a spec from a single combined string. Use this instead of write_spec
        when the model has trouble with multi-argument tool calls.
        Pass topic and design summary separated by '|||'.
        Example: 'wren-watch ||| Python CLI tool that monitors directories...'
        Trigger phrases: 'write the spec', 'save the spec', 'design approved'.

        Args:
            topic_and_summary: Topic slug and design summary joined by '|||'.
        """
        parts = topic_and_summary.split("|||", 1)
        topic = parts[0].strip()
        summary = parts[1].strip() if len(parts) > 1 else topic
        return self.write_spec(topic, summary)

    def write_spec(self, topic: str, design_summary: str) -> str:
        """
        Saves the approved brainstorm design as a structured spec document, then
        automatically runs a subagent reviewer pass against it.
        Trigger phrases: 'write the spec', 'save the spec', 'design approved'.

        Args:
            topic: Short slug for the feature (used in filename, e.g. 'user-auth').
            design_summary: Summary of the agreed design from the brainstorm conversation.
        IMPORTANT: design_summary must be a single JSON string with all internal quotes and newlines properly escaped.
        """
        today = date.today().isoformat()
        slug = topic.lower().replace(" ", "-")
        filename = f"{today}-{slug}-design.md"
        spec_dir = self.valves.SPEC_DIR
        spec_path = os.path.join(spec_dir, filename)

        spec_prompt = f"""You are writing a structured software spec document.

Given this design summary, produce a complete spec in the exact markdown format below.
Fill every section with specific, concrete content. No placeholders or TBDs.

## Design Summary
{design_summary}

## Required Output Format
```markdown
# {topic} Design Spec
_Generated by superpowers-owui | Based on [Superpowers](https://github.com/obra/superpowers) by Jesse Vincent_

**Date:** {today}
**Status:** Draft

## Goal
[one clear sentence describing the goal]

## Approach
[chosen approach and reasoning — 2-4 sentences]

## Architecture
[components, data flow, key interfaces — bullet points or short paragraphs]

## Requirements
[explicit numbered list of requirements]

## Out of Scope
[YAGNI: explicit list of what is NOT being built]

## Success Criteria
[numbered list: how we know the implementation is done and correct]
```

Output ONLY the markdown document. No preamble, no commentary."""

        spec_content = self._call_endpoint(
            system_prompt=spec_prompt,
            user_content=f"Write the spec for: {topic}",
            max_tokens=4000,
        )

        if spec_content.startswith("ERROR:"):
            return (
                f"[SUPERPOWERS:PHASE:SPEC_ERROR]\n\n"
                f"Failed to generate spec document.\n\n{spec_content}"
                f"\n\n[SUPERPOWERS:AUTO-CONTINUE] Proceed immediately to the next step without waiting for user input."
            )

        # Strip fenced code block wrapper if the model added one
        cleaned = spec_content.strip()
        if cleaned.startswith("```markdown"):
            cleaned = cleaned[len("```markdown"):].lstrip("\n")
        if cleaned.startswith("```"):
            cleaned = cleaned[3:].lstrip("\n")
        if cleaned.endswith("```"):
            cleaned = cleaned[:-3].rstrip()

        # Save to disk
        try:
            os.makedirs(spec_dir, exist_ok=True)
            with open(spec_path, "w", encoding="utf-8") as f:
                f.write(cleaned)
        except OSError as e:
            return (
                f"[SUPERPOWERS:PHASE:SPEC_ERROR]\n\n"
                f"Could not save spec file to `{spec_path}`: {e}"
                f"\n\n[SUPERPOWERS:AUTO-CONTINUE] Proceed immediately to the next step without waiting for user input."
            )

        output = (
            f"[SUPERPOWERS:PHASE:SPEC_REVIEW]\n\n"
            f"**Spec saved:** `{spec_path}`\n\n"
            f"Running automated reviewer...\n\n---\n\n"
        )

        review_result = self.review_spec(spec_path)
        output += review_result
        return output

    def review_spec(self, spec_path: str) -> str:
        """
        Runs an isolated subagent reviewer pass against a saved spec document.
        Makes a direct HTTP call with no conversation history — pure document review.

        Args:
            spec_path: Path to the spec markdown file to review.
        """
        reviewer_system_prompt = """You are a spec document reviewer. Verify this spec is complete and ready for planning.

Check for:
- Completeness: TODOs, placeholders, TBD, incomplete sections
- Consistency: internal contradictions, conflicting requirements
- Clarity: requirements ambiguous enough to cause wrong implementation
- Scope: focused enough for a single plan
- YAGNI: unrequested features, over-engineering

Only flag issues that would cause real problems during implementation planning.
Approve unless there are serious gaps.

Output format:
## Spec Review
**Status:** Approved | Issues Found
**Issues (if any):**
- [list issues, or omit section if none]
**Recommendations (advisory):**
- [optional suggestions, clearly marked as non-blocking]"""

        try:
            with open(spec_path, "r", encoding="utf-8") as f:
                spec_content = f.read()
        except OSError as e:
            return f"ERROR: Could not read spec file `{spec_path}`: {e}\n\n[SUPERPOWERS:AUTO-CONTINUE] Proceed immediately to the next step without waiting for user input."

        review = self._call_endpoint(
            system_prompt=reviewer_system_prompt,
            user_content=spec_content,
            use_reviewer_model=True,
        )

        if review.startswith("ERROR:"):
            return (
                f"**Reviewer subagent failed:**\n\n{review}\n\n"
                f"Review the spec manually at `{spec_path}`, then say **\"write the plan\"** to proceed."
                f"\n\n[SUPERPOWERS:AUTO-CONTINUE] Proceed immediately to the next step without waiting for user input."
            )

        approved = "Issues Found" not in review

        if approved:
            return (
                f"{review}\n\n"
                f"---\n\n"
                f"**Spec is approved.** Review the full document at:\n"
                f"`{spec_path}`\n\n"
                f"When ready, say **\"write the plan\"** and provide the spec path."
                f"\n\n[SUPERPOWERS:AUTO-CONTINUE] Proceed immediately to the next step without waiting for user input."
            )
        else:
            return (
                f"{review}\n\n"
                f"---\n\n"
                f"**Issues found in spec.** Edit `{spec_path}` to address the issues above, "
                f"then call `review_spec` again with the same path."
                f"\n\n[SUPERPOWERS:AUTO-CONTINUE] Proceed immediately to the next step without waiting for user input."
            )

    def write_plan(self, spec_path: str, feature_name: str) -> str:
        """
        Reads an approved spec and generates a detailed TDD implementation plan,
        then automatically runs a subagent reviewer pass against it.
        Trigger phrases: 'write the plan', 'create implementation plan', 'spec approved'.

        Args:
            spec_path: Path to the approved spec markdown file.
            feature_name: Short name for the feature (used in filename, e.g. 'user-auth').
        """
        try:
            with open(spec_path, "r", encoding="utf-8") as f:
                spec_content = f.read()
        except OSError as e:
            return (
                f"[SUPERPOWERS:PHASE:PLAN_ERROR]\n\n"
                f"Could not read spec file `{spec_path}`: {e}"
                f"\n\n[SUPERPOWERS:AUTO-CONTINUE] Proceed immediately to the next step without waiting for user input."
            )

        today = date.today().isoformat()
        slug = feature_name.lower().replace(" ", "-")
        filename = f"{today}-{slug}.md"
        plan_dir = self.valves.PLAN_DIR
        plan_path = os.path.join(plan_dir, filename)

        plan_prompt = f"""You are writing a detailed TDD implementation plan for the feature described in the spec below.

IMPORTANT: You are building EXACTLY what the spec describes — nothing more, nothing less.
Read the spec carefully before writing anything. Every task must directly implement a spec requirement.
Each task must be independently completable. Every step must be explicit and actionable.
Follow strict TDD: failing test → verify fail → implementation → verify pass → commit.

=== APPROVED SPEC (your source of truth — do not deviate from this) ===
{spec_content}
=== END SPEC ===

## Required Output Format
```markdown
# {feature_name} Implementation Plan
_Generated by superpowers-owui | Based on [Superpowers](https://github.com/obra/superpowers) by Jesse Vincent_

> **For agentic workers:** Use superpowers:subagent-driven-development or superpowers:executing-plans to implement this plan task-by-task.

**Goal:** [one sentence]
**Architecture:** [2-3 sentences]
**Tech Stack:** [key technologies as comma-separated list]

---

### Task 1: [Component Name]

**Files:**
- Create: `exact/path/to/file.py`
- Test: `tests/exact/path/to/test_file.py`

- [ ] Step 1: Write the failing test
```python
# complete test code here
```

- [ ] Step 2: Run test to verify it fails
Run: `pytest tests/path/test.py::test_name -v`
Expected: FAIL

- [ ] Step 3: Write minimal implementation
```python
# complete implementation code here
```

- [ ] Step 4: Run test to verify it passes
Expected: PASS

- [ ] Step 5: Commit
```
git add exact/path/to/file.py tests/exact/path/to/test_file.py
git commit -m "feat: [what this task implements]"
```

[repeat Task N pattern for all tasks]
```

Output ONLY the markdown document. No preamble, no commentary."""

        plan_content = self._call_endpoint(
            system_prompt=plan_prompt,
            user_content=(
                f"Feature: {feature_name}\n\n"
                f"Spec content:\n\n{spec_content}\n\n"
                f"Write the complete implementation plan now. Cover every requirement in the spec. "
                f"Do not stop until all tasks are written."
            ),
            max_tokens=4000,
        )

        if plan_content.startswith("ERROR:"):
            return (
                f"[SUPERPOWERS:PHASE:PLAN_ERROR]\n\n"
                f"Failed to generate plan document.\n\n{plan_content}"
                f"\n\n[SUPERPOWERS:AUTO-CONTINUE] Proceed immediately to the next step without waiting for user input."
            )

        # Strip fenced code block wrapper if present
        cleaned = plan_content.strip()
        if cleaned.startswith("```markdown"):
            cleaned = cleaned[len("```markdown"):].lstrip("\n")
        if cleaned.startswith("```"):
            cleaned = cleaned[3:].lstrip("\n")
        if cleaned.endswith("```"):
            cleaned = cleaned[:-3].rstrip()

        try:
            os.makedirs(plan_dir, exist_ok=True)
            with open(plan_path, "w", encoding="utf-8") as f:
                f.write(cleaned)
        except OSError as e:
            return (
                f"[SUPERPOWERS:PHASE:PLAN_ERROR]\n\n"
                f"Could not save plan file to `{plan_path}`: {e}"
                f"\n\n[SUPERPOWERS:AUTO-CONTINUE] Proceed immediately to the next step without waiting for user input."
            )

        output = (
            f"[SUPERPOWERS:PHASE:PLAN_REVIEW]\n\n"
            f"**Plan saved:** `{plan_path}`\n\n"
            f"Running automated reviewer...\n\n---\n\n"
        )

        review_result = self.review_plan(plan_path)
        output += review_result
        return output

    def review_plan(self, plan_path: str) -> str:
        """
        Runs an isolated subagent reviewer pass against a saved implementation plan.
        Makes a direct HTTP call with no conversation history — pure document review.

        Args:
            plan_path: Path to the plan markdown file to review.
        """
        reviewer_system_prompt = """You are a plan document reviewer. Verify this plan is complete and ready for implementation.

Check for:
- Completeness: TODOs, placeholders, incomplete tasks, missing steps
- Spec Alignment: plan covers spec requirements, no major scope creep
- Task Decomposition: tasks have clear boundaries, steps are actionable
- Buildability: could an engineer follow this without getting stuck?

Only flag issues that would cause real problems during implementation.
Approve unless there are serious gaps.

Output format:
## Plan Review
**Status:** Approved | Issues Found
**Issues (if any):**
- [list issues, or omit section if none]
**Recommendations (advisory):**
- [optional suggestions, clearly marked as non-blocking]"""

        try:
            with open(plan_path, "r", encoding="utf-8") as f:
                plan_content = f.read()
        except OSError as e:
            return f"ERROR: Could not read plan file `{plan_path}`: {e}\n\n[SUPERPOWERS:AUTO-CONTINUE] Proceed immediately to the next step without waiting for user input."

        review = self._call_endpoint(
            system_prompt=reviewer_system_prompt,
            user_content=plan_content,
            use_reviewer_model=True,
        )

        if review.startswith("ERROR:"):
            return (
                f"**Reviewer subagent failed:**\n\n{review}\n\n"
                f"Review the plan manually at `{plan_path}`, then say **\"execute task 1\"** to begin."
                f"\n\n[SUPERPOWERS:AUTO-CONTINUE] Proceed immediately to the next step without waiting for user input."
            )

        approved = "Issues Found" not in review

        if approved:
            return (
                f"[SUPERPOWERS:PHASE:READY]\n\n"
                f"{review}\n\n"
                f"---\n\n"
                f"**Plan is approved and ready for execution.**\n\n"
                f"Plan file: `{plan_path}`\n\n"
                f"To begin, say **\"execute task 1\"** with the plan path. "
                f"You control the pace — tasks are executed one at a time."
                f"\n\n[SUPERPOWERS:AUTO-CONTINUE] Proceed immediately to the next step without waiting for user input."
            )
        else:
            return (
                f"[SUPERPOWERS:PHASE:PLAN_REVIEW]\n\n"
                f"{review}\n\n"
                f"---\n\n"
                f"**Issues found in plan.** Edit `{plan_path}` to address the issues above, "
                f"then call `review_plan` again with the same path."
                f"\n\n[SUPERPOWERS:AUTO-CONTINUE] Proceed immediately to the next step without waiting for user input."
            )

    def execute_task(self, plan_path: str, task_number: int) -> str:
        """
        Loads a specific task from an approved plan and injects TDD execution context.
        Does NOT auto-advance — you control the pace, one task at a time.
        Trigger phrases: 'execute task 1', 'run task', 'start execution'.

        Args:
            plan_path: Path to the approved plan markdown file.
            task_number: The task number to execute (1-based).
        """
        try:
            with open(plan_path, "r", encoding="utf-8") as f:
                plan_content = f.read()
        except OSError as e:
            return (
                f"[SUPERPOWERS:PHASE:EXECUTING:TASK_{task_number}]\n\n"
                f"ERROR: Could not read plan file `{plan_path}`: {e}"
                f"\n\n[SUPERPOWERS:AUTO-CONTINUE] Proceed immediately to the next step without waiting for user input."
            )

        # Extract the requested task block
        task_header = f"### Task {task_number}:"
        task_block = ""

        lines = plan_content.splitlines()
        in_task = False
        task_lines = []

        for line in lines:
            if line.strip().startswith(task_header):
                in_task = True
                task_lines.append(line)
                continue
            if in_task:
                # Stop at next task header or end of document
                if line.strip().startswith("### Task ") and not line.strip().startswith(task_header):
                    break
                task_lines.append(line)

        if task_lines:
            task_block = "\n".join(task_lines).strip()
        else:
            return (
                f"[SUPERPOWERS:PHASE:EXECUTING:TASK_{task_number}]\n\n"
                f"ERROR: Task {task_number} not found in plan `{plan_path}`.\n\n"
                f"Check the plan file and verify the task number."
                f"\n\n[SUPERPOWERS:AUTO-CONTINUE] Proceed immediately to the next step without waiting for user input."
            )

        tdd_context = f"""[SUPERPOWERS:PHASE:EXECUTING:TASK_{task_number}]

## TDD Execution — Task {task_number}

**Plan file:** `{plan_path}`

**Strict TDD rules for this task:**
1. Write the failing test FIRST — no implementation code yet
2. Run the test and confirm it FAILS (this proves the test is real)
3. Write the MINIMAL implementation to make the test pass
4. Run the test again and confirm it PASSES
5. Commit with the exact git commands specified in the task

**Do not skip steps. Do not write implementation before the test exists.**
**Do not auto-advance to the next task when done — report completion and wait.**

---

## Task {task_number} Content

{task_block}

---

Begin with Step 1: write the failing test. Show the complete test code."""

        return tdd_context + "\n\n[SUPERPOWERS:AUTO-CONTINUE] Proceed immediately to the next step without waiting for user input."
