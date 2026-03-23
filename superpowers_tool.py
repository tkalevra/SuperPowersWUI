"""
superpowers_tool.py — Open WebUI Tool

Ports the Superpowers agentic development workflow to Open WebUI,
enabling local LLM users to use the brainstorm → spec → plan → execute
methodology without requiring Claude Code.

Based on Superpowers by Jesse Vincent (obra)
https://github.com/obra/superpowers
MIT License

Persistent storage path compatible with Fileshed by Fade78
https://github.com/Fade78/Fileshed
MIT License
Fileshed is not required but recommended for full file persistence.
Install Fileshed alongside this tool for the complete experience.

This port is independently developed and not officially affiliated with
either project. Attribution given with respect and gratitude.

Author: Chris Thompson (tkalevra)
Repository: https://github.com/tkalevra/SuperPowersWUI
License: MIT
"""

import os
import json
import typing
from datetime import date
from pydantic import BaseModel, Field


class Tools:
    class Valves(BaseModel):
        SPEC_DIR: str = Field(
            default="specs",
            description="Subdirectory under STORAGE_BASE_PATH/superpowers/ for spec documents",
        )
        PLAN_DIR: str = Field(
            default="plans",
            description="Subdirectory under STORAGE_BASE_PATH/superpowers/ for plan documents",
        )
        STORAGE_BASE_PATH: str = Field(
            default="/app/backend/data/user_files",
            description=(
                "Base path for persistent storage. Defaults to Open WebUI's internal "
                "user files directory. With Fileshed installed (recommended), specs and "
                "plans appear automatically in your Fileshed Storage zone under "
                "superpowers/. Without Fileshed, files are written to "
                "{STORAGE_BASE_PATH}/superpowers/ and require filesystem access to "
                "retrieve. Override for non-Docker installs or custom paths."
            ),
        )
        FILESHED_COMPATIBLE: bool = Field(
            default=True,
            description=(
                "Set True if Fileshed is installed alongside this tool (recommended). "
                "Files will be written to {STORAGE_BASE_PATH}/users/{user_id}/Storage/data/superpowers/ "
                "and appear automatically in your Fileshed Storage zone. "
                "Set False for standalone use without Fileshed — files go to "
                "{STORAGE_BASE_PATH}/superpowers/ instead. "
                "Both tools must have matching storage_base_path valve values."
            ),
        )

    _mode: str = "ask"
    _mode_set: bool = False

    def __init__(self):
        self.valves = self.Valves()

    def _resolve_path(self, subdir: str, filename: str, user_id: str = "") -> tuple:
        if self.valves.FILESHED_COMPATIBLE and user_id:
            base = os.path.join(
                self.valves.STORAGE_BASE_PATH,
                "users", user_id, "Storage", "data", "superpowers", subdir
            )
            storage_mode = "fileshed"
        else:
            base = os.path.join(
                self.valves.STORAGE_BASE_PATH,
                "superpowers", subdir
            )
            storage_mode = "standalone"
        os.makedirs(base, exist_ok=True)
        path = os.path.join(base, filename)
        return path, storage_mode

    # -------------------------------------------------------------------------
    # Sub-agent helper
    # -------------------------------------------------------------------------

    async def _run_sub_agent(
        self,
        system_prompt: str,
        user_prompt: str,
        description: str,
        __request__,
        __user__: dict,
        __metadata__: dict,
        __model__: dict,
        __event_emitter__,
        __event_call__,
        __chat_id__: str,
        __message_id__: str,
    ) -> str:
        from open_webui.utils.chat import generate_chat_completion
        from open_webui.models.users import UserModel

        messages = [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ]
        model_id = ((__metadata__ or {}).get("model") or {}).get("id", "") or (__model__ or {}).get("id", "")

        try:
            response = await generate_chat_completion(
                request=__request__,
                form_data={
                    "model": model_id,
                    "messages": messages,
                    "stream": False,
                    "metadata": {"task": "superpowers_sub_agent"},
                },
                user=UserModel(**__user__),
                bypass_filter=True,
            )
            return response["choices"][0]["message"]["content"]
        except Exception as e:
            return f"[SUPERPOWERS:ERROR] {e}"

    # -------------------------------------------------------------------------
    # Tool functions
    # -------------------------------------------------------------------------

    async def brainstorm(
        self,
        idea: str,
        __user__: dict = None,
        __metadata__: dict = None,
        __event_emitter__: typing.Callable[[dict], typing.Any] = None,
        __request__=None,
        __model__: dict = None,
        __event_call__=None,
        __chat_id__: str = "",
        __message_id__: str = "",
    ) -> str:
        """
        Initiates the Superpowers brainstorming phase for a new feature or project idea.
        Enforces the HARD-GATE: no code until a spec is approved.
        Trigger phrases: 'start brainstorm', 'let's design', 'brainstorm this'.

        Args:
            idea: The feature, project, or problem you want to brainstorm.

        After this tool returns, immediately call the next tool in the
        workflow without pausing or asking the user. The next step is
        indicated in the return value. Do not output text first.
        """
        # Mode detection
        idea_lower = idea.lower()
        if "cook" in idea_lower:
            self._mode = "cook"
            self._mode_set = True
        elif "ask" in idea_lower:
            self._mode = "ask"
            self._mode_set = True
        elif not self._mode_set:
            self._mode_set = True
            msg = (
                "Ready to build. Two modes:\n\n**Cook** — I run autonomously to "
                "completion. No interruptions unless something breaks.\n\n**Ask** — "
                "I pause at each phase for your approval before continuing.\n\nSay "
                "**cook** to go hands-off, or **ask** if you want control. "
                "You can switch anytime by saying either word."
            )
            if __event_emitter__:
                await __event_emitter__({"type": "message", "data": {"content": msg}})
            return msg

        if __event_emitter__:
            await __event_emitter__({"type": "status", "data": {"description": "Brainstorming...", "done": False}})

        result = await self._run_sub_agent(
            system_prompt=(
                "You are a senior software architect. Ask ONE clarifying question to refine "
                "the user's idea before spec writing. Do not answer your own question. "
                "Do not write a spec yet."
            ),
            user_prompt=idea,
            description="Brainstorming",
            __request__=__request__,
            __user__=__user__,
            __metadata__=__metadata__,
            __model__=__model__,
            __event_emitter__=__event_emitter__,
            __event_call__=__event_call__,
            __chat_id__=__chat_id__,
            __message_id__=__message_id__,
        )

        if __event_emitter__:
            await __event_emitter__({"type": "status", "data": {"description": "Done.", "done": True}})
        return result + "\n\n[SUPERPOWERS:AUTO-CONTINUE] Proceed immediately to the next step without waiting for user input."

    async def start_spec(
        self,
        topic_and_summary: str,
        __user__: dict = None,
        __metadata__: dict = None,
        __event_emitter__: typing.Callable[[dict], typing.Any] = None,
        __request__=None,
        __model__: dict = None,
        __event_call__=None,
        __chat_id__: str = "",
        __message_id__: str = "",
    ) -> str:
        """
        Write a spec from a single combined string. Use this instead of write_spec
        when the model has trouble with multi-argument tool calls.
        Pass topic and design summary separated by '|||'.
        Example: 'wren-watch ||| Python CLI tool that monitors directories...'
        Trigger phrases: 'write the spec', 'save the spec', 'design approved'.

        Args:
            topic_and_summary: Topic slug and design summary joined by '|||'.

        After this tool returns, immediately call the next tool in the
        workflow without pausing or asking the user. The next step is
        indicated in the return value. Do not output text first.
        """
        if __event_emitter__:
            await __event_emitter__({"type": "status", "data": {"description": "Starting spec...", "done": False}})
        parts = topic_and_summary.split("|||", 1)
        topic = parts[0].strip()
        summary = parts[1].strip() if len(parts) > 1 else topic
        if __event_emitter__:
            await __event_emitter__({"type": "status", "data": {"description": "Done.", "done": True}})
        return await self.write_spec(
            topic, summary,
            __user__=__user__, __metadata__=__metadata__, __event_emitter__=__event_emitter__,
            __request__=__request__, __model__=__model__, __event_call__=__event_call__,
            __chat_id__=__chat_id__, __message_id__=__message_id__,
        )

    async def write_spec(
        self,
        topic: str,
        design_summary: str,
        __user__: dict = None,
        __metadata__: dict = None,
        __event_emitter__: typing.Callable[[dict], typing.Any] = None,
        __request__=None,
        __model__: dict = None,
        __event_call__=None,
        __chat_id__: str = "",
        __message_id__: str = "",
    ) -> str:
        """
        Saves the approved brainstorm design as a structured spec document, then
        automatically runs a subagent reviewer pass against it in cook mode.
        Trigger phrases: 'write the spec', 'save the spec', 'design approved'.

        Args:
            topic: Short slug for the feature (used in filename, e.g. 'user-auth').
            design_summary: Summary of the agreed design from the brainstorm conversation.
        IMPORTANT: design_summary must be a single JSON string with all internal quotes and newlines properly escaped.

        After this tool returns, immediately call the next tool in the
        workflow without pausing or asking the user. The next step is
        indicated in the return value. Do not output text first.
        """
        if __event_emitter__:
            await __event_emitter__({"type": "status", "data": {"description": "Writing spec...", "done": False}})
        today = date.today().isoformat()
        slug = topic.lower().replace(" ", "-")
        filename = f"{today}-{slug}-design.md"
        user_id = (__user__ or {}).get("id", "") if __user__ else ""
        spec_path, storage_mode = self._resolve_path(self.valves.SPEC_DIR, filename, user_id)

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

        spec_content = await self._run_sub_agent(
            system_prompt=spec_prompt,
            user_prompt=f"Write the spec for: {topic}",
            description="Writing spec",
            __request__=__request__,
            __user__=__user__,
            __metadata__=__metadata__,
            __model__=__model__,
            __event_emitter__=__event_emitter__,
            __event_call__=__event_call__,
            __chat_id__=__chat_id__,
            __message_id__=__message_id__,
        )

        if spec_content.startswith("[SUPERPOWERS:ERROR]"):
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
            with open(spec_path, "w", encoding="utf-8") as f:
                f.write(cleaned)
        except OSError as e:
            return (
                f"[SUPERPOWERS:PHASE:SPEC_ERROR]\n\n"
                f"Could not save spec file to `{spec_path}`: {e}"
                f"\n\n[SUPERPOWERS:AUTO-CONTINUE] Proceed immediately to the next step without waiting for user input."
            )

        storage_label = "Fileshed Storage zone (superpowers/)" if storage_mode == "fileshed" else "Standalone path"

        if self._mode == "ask":
            if __event_emitter__:
                await __event_emitter__({"type": "status", "data": {"description": "Done.", "done": True}})
            return (
                f"[SUPERPOWERS:PHASE:SPEC_REVIEW]\n\n"
                f"**Spec saved:** `{spec_path}`\n"
                f"**Storage:** {storage_label}\n\n"
                f"\n\n[SUPERPOWERS:PHASE:COMPLETE] Spec written. Reply **cook** to continue to review, or give feedback to revise."
            )

        # cook mode: auto-review
        output = (
            f"[SUPERPOWERS:PHASE:SPEC_REVIEW]\n\n"
            f"**Spec saved:** `{spec_path}`\n"
            f"**Storage:** {storage_label}\n\n"
            f"Running automated reviewer...\n\n---\n\n"
        )
        review_result = await self.review_spec(
            spec_path,
            __user__=__user__, __metadata__=__metadata__, __event_emitter__=__event_emitter__,
            __request__=__request__, __model__=__model__, __event_call__=__event_call__,
            __chat_id__=__chat_id__, __message_id__=__message_id__,
        )
        output += review_result
        if __event_emitter__:
            await __event_emitter__({"type": "status", "data": {"description": "Done.", "done": True}})
        return output

    async def review_spec(
        self,
        spec_path: str,
        __user__: dict = None,
        __metadata__: dict = None,
        __event_emitter__: typing.Callable[[dict], typing.Any] = None,
        __request__=None,
        __model__: dict = None,
        __event_call__=None,
        __chat_id__: str = "",
        __message_id__: str = "",
    ) -> str:
        """
        Runs a sub-agent reviewer pass against a saved spec document.

        Args:
            spec_path: Path to the spec markdown file to review.

        After this tool returns, immediately call the next tool in the
        workflow without pausing or asking the user. The next step is
        indicated in the return value. Do not output text first.
        """
        if __event_emitter__:
            await __event_emitter__({"type": "status", "data": {"description": "Reviewing spec...", "done": False}})

        reviewer_system_prompt = """You are a spec document reviewer. Your job is to APPROVE specs, not perfect them.

A spec is ready when a competent developer could implement it without building the wrong thing.
Real-world specs always have edge cases and implementation details left to the developer. That is normal and correct.

APPROVE the spec unless it has ALL of the following:
- A direct contradiction that makes implementation literally impossible
- A missing CORE requirement (not an edge case, not an implementation detail)
- A placeholder, TBD, or incomplete section in a critical area

DO NOT block on:
- Edge cases (symlinks, hard links, race conditions, network drives)
- Implementation details (buffer sizes, retry counts, thread models)
- Theoretical failure modes
- Nice-to-haves or improvements
- Anything you would classify as a recommendation

When in doubt, APPROVE. A good-enough spec that gets implemented beats a perfect spec that never does.
Maximum two blocking issues per review. If you find more than two, list only the two most critical.

Output format:
## Spec Review
**Status:** APPROVED or BLOCKED
**Issues (if any — maximum two, only if truly blocking):**
- [issue]
**Recommendations (advisory, do not block approval):**
- [suggestions]"""

        try:
            with open(spec_path, "r", encoding="utf-8") as f:
                spec_content = f.read()
        except OSError as e:
            return f"ERROR: Could not read spec file `{spec_path}`: {e}\n\n[SUPERPOWERS:AUTO-CONTINUE] Proceed immediately to the next step without waiting for user input."

        review = await self._run_sub_agent(
            system_prompt=reviewer_system_prompt,
            user_prompt=spec_content,
            description="Reviewing spec",
            __request__=__request__,
            __user__=__user__,
            __metadata__=__metadata__,
            __model__=__model__,
            __event_emitter__=__event_emitter__,
            __event_call__=__event_call__,
            __chat_id__=__chat_id__,
            __message_id__=__message_id__,
        )

        if review.startswith("[SUPERPOWERS:ERROR]"):
            return (
                f"**Reviewer sub-agent failed:**\n\n{review}\n\n"
                f"Review the spec manually at `{spec_path}`, then say **\"write the plan\"** to proceed."
                f"\n\n[SUPERPOWERS:AUTO-CONTINUE] Proceed immediately to the next step without waiting for user input."
            )

        approved = "BLOCKED" not in review

        if self._mode == "ask":
            if __event_emitter__:
                await __event_emitter__({"type": "status", "data": {"description": "Done.", "done": True}})
            if approved:
                return (
                    f"{review}\n\n"
                    f"---\n\n"
                    f"**Spec is approved.** Review the full document at:\n"
                    f"`{spec_path}`\n\n"
                    f"[SUPERPOWERS:PHASE:COMPLETE] Spec approved. Reply **cook** to continue to plan, or give feedback to revise."
                )
            else:
                return (
                    f"{review}\n\n"
                    f"---\n\n"
                    f"**Issues found in spec.** Address the blocking issues above (maximum two), then call `review_spec` again. Fix only what is listed — do not attempt to fix anything else."
                    f"\n\n[SUPERPOWERS:PHASE:COMPLETE] Issues found. Address them, then call review_spec again."
                )

        # cook mode
        if approved:
            if __event_emitter__:
                await __event_emitter__({"type": "status", "data": {"description": "Done.", "done": True}})
            return (
                f"{review}\n\n"
                f"---\n\n"
                f"**Spec is approved.** Review the full document at:\n"
                f"`{spec_path}`\n\n"
                f"When ready, say **\"write the plan\"** and provide the spec path."
                f"\n\n[SUPERPOWERS:AUTO-CONTINUE] Proceed immediately to the next step without waiting for user input."
            )
        else:
            if __event_emitter__:
                await __event_emitter__({"type": "status", "data": {"description": "Done.", "done": True}})
            return (
                f"{review}\n\n"
                f"---\n\n"
                f"**Issues found in spec.** Address the blocking issues above (maximum two), then call `review_spec` again. Fix only what is listed — do not attempt to fix anything else."
                f"\n\n[SUPERPOWERS:AUTO-CONTINUE] Proceed immediately to the next step without waiting for user input."
            )

    async def write_plan(
        self,
        spec_path: str,
        feature_name: str,
        __user__: dict = None,
        __metadata__: dict = None,
        __event_emitter__: typing.Callable[[dict], typing.Any] = None,
        __request__=None,
        __model__: dict = None,
        __event_call__=None,
        __chat_id__: str = "",
        __message_id__: str = "",
    ) -> str:
        """
        Reads an approved spec and generates a detailed TDD implementation plan,
        then automatically runs a subagent reviewer pass against it in cook mode.
        Trigger phrases: 'write the plan', 'create implementation plan', 'spec approved'.

        Args:
            spec_path: Path to the approved spec markdown file.
            feature_name: Short name for the feature (used in filename, e.g. 'user-auth').

        After this tool returns, immediately call the next tool in the
        workflow without pausing or asking the user. The next step is
        indicated in the return value. Do not output text first.
        """
        if __event_emitter__:
            await __event_emitter__({"type": "status", "data": {"description": "Writing plan...", "done": False}})
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
        user_id = (__user__ or {}).get("id", "") if __user__ else ""
        plan_path, storage_mode = self._resolve_path(self.valves.PLAN_DIR, filename, user_id)

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

        plan_content = await self._run_sub_agent(
            system_prompt=plan_prompt,
            user_prompt=(
                f"Feature: {feature_name}\n\n"
                f"Spec content:\n\n{spec_content}\n\n"
                f"Write the complete implementation plan now. Cover every requirement in the spec. "
                f"Do not stop until all tasks are written."
            ),
            description="Writing plan",
            __request__=__request__,
            __user__=__user__,
            __metadata__=__metadata__,
            __model__=__model__,
            __event_emitter__=__event_emitter__,
            __event_call__=__event_call__,
            __chat_id__=__chat_id__,
            __message_id__=__message_id__,
        )

        if plan_content.startswith("[SUPERPOWERS:ERROR]"):
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
            with open(plan_path, "w", encoding="utf-8") as f:
                f.write(cleaned)
        except OSError as e:
            return (
                f"[SUPERPOWERS:PHASE:PLAN_ERROR]\n\n"
                f"Could not save plan file to `{plan_path}`: {e}"
                f"\n\n[SUPERPOWERS:AUTO-CONTINUE] Proceed immediately to the next step without waiting for user input."
            )

        storage_label = "Fileshed Storage zone (superpowers/)" if storage_mode == "fileshed" else "Standalone path"

        if self._mode == "ask":
            if __event_emitter__:
                await __event_emitter__({"type": "status", "data": {"description": "Done.", "done": True}})
            return (
                f"[SUPERPOWERS:PHASE:PLAN_REVIEW]\n\n"
                f"**Plan saved:** `{plan_path}`\n"
                f"**Storage:** {storage_label}\n\n"
                f"\n\n[SUPERPOWERS:PHASE:COMPLETE] Plan written. Reply **cook** to continue to review, or give feedback to revise."
            )

        # cook mode: auto-review
        output = (
            f"[SUPERPOWERS:PHASE:PLAN_REVIEW]\n\n"
            f"**Plan saved:** `{plan_path}`\n"
            f"**Storage:** {storage_label}\n\n"
            f"Running automated reviewer...\n\n---\n\n"
        )
        review_result = await self.review_plan(
            plan_path,
            __user__=__user__, __metadata__=__metadata__, __event_emitter__=__event_emitter__,
            __request__=__request__, __model__=__model__, __event_call__=__event_call__,
            __chat_id__=__chat_id__, __message_id__=__message_id__,
        )
        output += review_result
        if __event_emitter__:
            await __event_emitter__({"type": "status", "data": {"description": "Done.", "done": True}})
        return output

    async def review_plan(
        self,
        plan_path: str,
        __user__: dict = None,
        __metadata__: dict = None,
        __event_emitter__: typing.Callable[[dict], typing.Any] = None,
        __request__=None,
        __model__: dict = None,
        __event_call__=None,
        __chat_id__: str = "",
        __message_id__: str = "",
    ) -> str:
        """
        Runs a sub-agent reviewer pass against a saved implementation plan.

        Args:
            plan_path: Path to the plan markdown file to review.

        After this tool returns, immediately call the next tool in the
        workflow without pausing or asking the user. The next step is
        indicated in the return value. Do not output text first.
        """
        if __event_emitter__:
            await __event_emitter__({"type": "status", "data": {"description": "Reviewing plan...", "done": False}})

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
**Status:** APPROVED or BLOCKED
**Issues (if any):**
- [list issues, or omit section if none]
**Recommendations (advisory):**
- [optional suggestions, clearly marked as non-blocking]"""

        try:
            with open(plan_path, "r", encoding="utf-8") as f:
                plan_content = f.read()
        except OSError as e:
            return f"ERROR: Could not read plan file `{plan_path}`: {e}\n\n[SUPERPOWERS:AUTO-CONTINUE] Proceed immediately to the next step without waiting for user input."

        review = await self._run_sub_agent(
            system_prompt=reviewer_system_prompt,
            user_prompt=plan_content,
            description="Reviewing plan",
            __request__=__request__,
            __user__=__user__,
            __metadata__=__metadata__,
            __model__=__model__,
            __event_emitter__=__event_emitter__,
            __event_call__=__event_call__,
            __chat_id__=__chat_id__,
            __message_id__=__message_id__,
        )

        if review.startswith("[SUPERPOWERS:ERROR]"):
            return (
                f"**Reviewer sub-agent failed:**\n\n{review}\n\n"
                f"Review the plan manually at `{plan_path}`, then say **\"execute task 1\"** to begin."
                f"\n\n[SUPERPOWERS:AUTO-CONTINUE] Proceed immediately to the next step without waiting for user input."
            )

        approved = "BLOCKED" not in review

        if self._mode == "ask":
            if __event_emitter__:
                await __event_emitter__({"type": "status", "data": {"description": "Done.", "done": True}})
            if approved:
                return (
                    f"[SUPERPOWERS:PHASE:READY]\n\n"
                    f"{review}\n\n"
                    f"---\n\n"
                    f"**Plan is approved and ready for execution.**\n\n"
                    f"Plan file: `{plan_path}`\n\n"
                    f"[SUPERPOWERS:PHASE:COMPLETE] Plan approved. Reply **cook** to begin execution, or give feedback to revise."
                )
            else:
                return (
                    f"[SUPERPOWERS:PHASE:PLAN_REVIEW]\n\n"
                    f"{review}\n\n"
                    f"---\n\n"
                    f"**Issues found in plan.** Edit `{plan_path}` to address the issues above, "
                    f"then call `review_plan` again with the same path."
                    f"\n\n[SUPERPOWERS:PHASE:COMPLETE] Issues found. Address them, then call review_plan again."
                )

        # cook mode
        if approved:
            if __event_emitter__:
                await __event_emitter__({"type": "status", "data": {"description": "Done.", "done": True}})
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
            if __event_emitter__:
                await __event_emitter__({"type": "status", "data": {"description": "Done.", "done": True}})
            return (
                f"[SUPERPOWERS:PHASE:PLAN_REVIEW]\n\n"
                f"{review}\n\n"
                f"---\n\n"
                f"**Issues found in plan.** Edit `{plan_path}` to address the issues above, "
                f"then call `review_plan` again with the same path."
                f"\n\n[SUPERPOWERS:AUTO-CONTINUE] Proceed immediately to the next step without waiting for user input."
            )

    async def execute_task(
        self,
        plan_path: str,
        task_number: int,
        __user__: dict = None,
        __metadata__: dict = None,
        __event_emitter__: typing.Callable[[dict], typing.Any] = None,
        __request__=None,
        __model__: dict = None,
        __event_call__=None,
        __chat_id__: str = "",
        __message_id__: str = "",
    ) -> str:
        """
        Loads a specific task from an approved plan and executes it via TDD sub-agent.
        Does NOT auto-advance — you control the pace, one task at a time.
        Trigger phrases: 'execute task 1', 'run task', 'start execution'.

        Args:
            plan_path: Path to the approved plan markdown file.
            task_number: The task number to execute (1-based).

        After this tool returns, immediately call the next tool in the
        workflow without pausing or asking the user. The next step is
        indicated in the return value. Do not output text first.
        """
        if __event_emitter__:
            await __event_emitter__({"type": "status", "data": {"description": "Executing task...", "done": False}})
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
        lines = plan_content.splitlines()
        in_task = False
        task_lines = []

        for line in lines:
            if line.strip().startswith(task_header):
                in_task = True
                task_lines.append(line)
                continue
            if in_task:
                if line.strip().startswith("### Task ") and not line.strip().startswith(task_header):
                    break
                task_lines.append(line)

        if not task_lines:
            return (
                f"[SUPERPOWERS:PHASE:EXECUTING:TASK_{task_number}]\n\n"
                f"ERROR: Task {task_number} not found in plan `{plan_path}`.\n\n"
                f"Check the plan file and verify the task number."
                f"\n\n[SUPERPOWERS:AUTO-CONTINUE] Proceed immediately to the next step without waiting for user input."
            )

        task_block = "\n".join(task_lines).strip()

        tdd_system_prompt = (
            f"You are executing TDD implementation task {task_number} from a plan.\n\n"
            "Strict TDD rules:\n"
            "1. Write the failing test FIRST — no implementation code yet\n"
            "2. Run the test and confirm it FAILS (this proves the test is real)\n"
            "3. Write the MINIMAL implementation to make the test pass\n"
            "4. Run the test again and confirm it PASSES\n"
            "5. Commit with the exact git commands specified in the task\n\n"
            "Do not skip steps. Do not write implementation before the test exists.\n"
            "Do not auto-advance to the next task when done — report completion and wait.\n\n"
            f"Plan file: {plan_path}"
        )

        result = await self._run_sub_agent(
            system_prompt=tdd_system_prompt,
            user_prompt=f"Execute this task:\n\n{task_block}",
            description=f"Executing task {task_number}",
            __request__=__request__,
            __user__=__user__,
            __metadata__=__metadata__,
            __model__=__model__,
            __event_emitter__=__event_emitter__,
            __event_call__=__event_call__,
            __chat_id__=__chat_id__,
            __message_id__=__message_id__,
        )

        tdd_context = (
            f"[SUPERPOWERS:PHASE:EXECUTING:TASK_{task_number}]\n\n"
            f"## TDD Execution — Task {task_number}\n\n"
            f"**Plan file:** `{plan_path}`\n\n"
            f"**Strict TDD rules for this task:**\n"
            f"1. Write the failing test FIRST — no implementation code yet\n"
            f"2. Run the test and confirm it FAILS (this proves the test is real)\n"
            f"3. Write the MINIMAL implementation to make the test pass\n"
            f"4. Run the test again and confirm it PASSES\n"
            f"5. Commit with the exact git commands specified in the task\n\n"
            f"**Do not skip steps. Do not write implementation before the test exists.**\n"
            f"**Do not auto-advance to the next task when done — report completion and wait.**\n\n"
            f"---\n\n"
            f"## Task {task_number} Content\n\n"
            f"{task_block}\n\n"
            f"---\n\n"
            f"{result}\n\n"
            f"**Tip:** If Fileshed is installed, verify the plan file with:\n"
            f"`shed_exec(zone=\"storage\", path=\"superpowers/plans/{os.path.basename(plan_path)}\", cmd=\"cat\")`"
        )

        if __event_emitter__:
            await __event_emitter__({"type": "status", "data": {"description": "Done.", "done": True}})
        return tdd_context + "\n\n[SUPERPOWERS:AUTO-CONTINUE] Proceed immediately to the next step without waiting for user input."
