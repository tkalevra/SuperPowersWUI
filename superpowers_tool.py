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
import re
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
        COMPLEXITY: str = Field(
            default="simple",
            description=(
                "Controls spec and plan verbosity. "
                "simple = concise bullet-point spec and minimal plan, fast. "
                "medium = standard spec with edge cases, full TDD plan. "
                "complex = exhaustive spec, detailed plan, full review cycles. "
                "Values: simple, medium, complex."
            ),
        )

    def __init__(self):
        self.valves = self.Valves()
        self.MAX_PLAN_REVISIONS = 2
        self._plan_revision_count = 0

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
        zone_relative_path = f"superpowers/{subdir}/{filename}" if storage_mode == "fileshed" else ""
        return path, storage_mode, zone_relative_path

    def _get_mode(self, messages: list = None) -> str:
        """
        Resolve current mode from conversation history.
        Scans all messages for the most recent [SUPERPOWERS:MODE:*]
        tag and returns "cook" or "ask". Defaults to "ask" if no
        tag found.
        """
        if not messages:
            return "ask"
        for msg in reversed(messages):
            content = ""
            if isinstance(msg, dict):
                c = msg.get("content", "")
                content = c if isinstance(c, str) else ""
            if "[SUPERPOWERS:MODE:COOK]" in content:
                return "cook"
            if "[SUPERPOWERS:MODE:ASK]" in content:
                return "ask"
        return "ask"

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
        max_retries: int = 2,
    ) -> str:
        import asyncio
        from open_webui.utils.chat import generate_chat_completion
        from open_webui.models.users import UserModel

        _refusal_phrases = [
            "i cannot", "i apologize", "i'm sorry", "i am sorry",
            "i'm unable", "i am unable", "i don't think i can",
            "i am not able", "i can't",
        ]

        messages = [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ]
        model_id = ((__metadata__ or {}).get("model") or {}).get("id", "") or (__model__ or {}).get("id", "")

        for attempt in range(max_retries + 1):
            if attempt > 0:
                await asyncio.sleep(1.5 ** attempt)
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
                content = response["choices"][0]["message"]["content"]
                preview = content[:500].lower()
                if any(phrase in preview for phrase in _refusal_phrases):
                    continue
                return content
            except Exception as e:
                if attempt >= max_retries:
                    return f"[SUPERPOWERS:ERROR] {e}"
                continue

        return "[SUPERPOWERS:ERROR] Sub-agent refused after all retry attempts."

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
        __messages__: list = None,
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
            mode_tag = "[SUPERPOWERS:MODE:COOK]"
            mode = "cook"
        elif "ask" in idea_lower:
            mode_tag = "[SUPERPOWERS:MODE:ASK]"
            mode = "ask"
        else:
            mode = self._get_mode(__messages__)
            mode_tag = f"[SUPERPOWERS:MODE:{mode.upper()}]"

        # First-time prompt: show only if no mode tag found in history
        if not __messages__ or not any(
            "[SUPERPOWERS:MODE:" in (m.get("content", "") if isinstance(m, dict) else "")
            for m in (__messages__ or [])
        ):
            if "cook" not in idea_lower and "ask" not in idea_lower:
                msg = (
                    "Ready to build. Two modes:\n\n**Cook** — I run autonomously to "
                    "completion. No interruptions unless something breaks.\n\n**Ask** — "
                    "I pause at each phase for your approval before continuing.\n\nSay "
                    "**cook** to go hands-off, or **ask** if you want control. "
                    "You can switch anytime by saying either word."
                )
                if __event_emitter__:
                    await __event_emitter__({"type": "message", "data": {"content": msg}})
                return msg + "\n\n[SUPERPOWERS:MODE:ASK]"

        if __event_emitter__:
            await __event_emitter__({"type": "status", "data": {"description": "Brainstorming...", "done": False}})

        if self.valves.COMPLEXITY == "simple":
            system_prompt = (
                "You are a senior software engineer. The user has described "
                "a task. In two sentences maximum, confirm your understanding "
                "of what needs to be built. Do not ask questions. Do not list "
                "features. Just confirm and stop."
            )
        else:
            system_prompt = (
                "You are a senior software architect. Ask ONE clarifying "
                "question to refine the user's idea before spec writing. "
                "Do not answer your own question. Do not write a spec yet."
            )

        result = await self._run_sub_agent(
            system_prompt=system_prompt,
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

        if self.valves.COMPLEXITY == "simple":
            return result + "\n\n[SUPERPOWERS:AUTO-CONTINUE] Proceed immediately to the next step without waiting for user input.\n" + mode_tag
        return result + "\n\n" + mode_tag

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
        __messages__: list = None,
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
        parts = topic_and_summary.split("|||", 1)
        topic = parts[0].strip()
        summary = parts[1].strip() if len(parts) > 1 else topic
        return await self.write_spec(
            topic, summary,
            __user__=__user__, __metadata__=__metadata__, __event_emitter__=__event_emitter__,
            __request__=__request__, __model__=__model__, __event_call__=__event_call__,
            __chat_id__=__chat_id__, __message_id__=__message_id__, __messages__=__messages__,
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
        __messages__: list = None,
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
        spec_path, storage_mode, zone_relative_path = self._resolve_path(self.valves.SPEC_DIR, filename, user_id)

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

PRECISION RULE for state-based or comparison logic:
When a requirement involves detecting change, difference, or
state transition (e.g. "detect file modifications"), you MUST
explicitly name every field compared and every condition that
triggers the event. Never write "detect changes" — write
"detect changes by comparing mtime AND size; trigger MODIFY
if either differs from the previous snapshot". Ambiguous
comparison requirements are invalid and will be rejected.

## Out of Scope
[YAGNI: explicit list of what is NOT being built]

## Success Criteria
[numbered list: how we know the implementation is done and correct]
```

Output ONLY the markdown document. No preamble, no commentary."""

        if self.valves.COMPLEXITY == "simple":
            spec_system_prompt = (
                "You are a senior software engineer. Write a concise spec "
                "for the following design. Use short bullet points only. "
                "Cover: purpose, inputs/outputs, key requirements, error "
                "handling. Maximum 300 words. No prose paragraphs."
            )
        elif self.valves.COMPLEXITY == "complex":
            spec_system_prompt = (
                "You are a senior software architect. Write an exhaustive "
                "technical spec covering: purpose, all inputs/outputs, full "
                "requirements list, edge cases, security considerations, "
                "error handling, success criteria, and constraints."
            )
        else:  # medium (default)
            spec_system_prompt = (
                "You are a senior software engineer. Write a clear technical "
                "spec covering: purpose, inputs/outputs, requirements, key "
                "edge cases, and error handling. Be thorough but concise."
            )

        spec_content = await self._run_sub_agent(
            system_prompt=spec_system_prompt,
            user_prompt=spec_prompt,
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

        # Validate and strip fenced code block wrapper
        valid, cleaned = self._validate_output(spec_content, "spec")
        if not valid:
            return (
                f"[SUPERPOWERS:PHASE:FATAL_ERROR]\n\n"
                f"Spec sub-agent output failed validation (refusal or missing required sections)."
                f"\n\n[SUPERPOWERS:AUTO-CONTINUE] Proceed immediately to the next step without waiting for user input."
            )

        # Save to disk atomically
        ok, err = self._atomic_write(spec_path, cleaned)
        if not ok:
            return (
                f"[SUPERPOWERS:PHASE:FATAL_ERROR]\n\n"
                f"Could not save spec file to `{spec_path}`: {err}"
                f"\n\n[SUPERPOWERS:AUTO-CONTINUE] Proceed immediately to the next step without waiting for user input."
            )

        storage_label = "Fileshed Storage zone (superpowers/)" if storage_mode == "fileshed" else "Standalone path"

        if self.valves.COMPLEXITY == "simple" and self._get_mode(__messages__) == "cook":
            if __event_emitter__:
                await __event_emitter__({"type": "status", "data": {"description": "Done.", "done": True}})
            return (
                f"[SUPERPOWERS:PHASE:SPEC_DONE]\n\n"
                f"**Spec saved:** `{spec_path}`\n"
                f"**Mode:** simple — skipping review, proceeding to plan.\n\n"
                f"[SUPERPOWERS:AUTO-CONTINUE]"
            )

        if self._get_mode(__messages__) == "ask":
            if __event_emitter__:
                await __event_emitter__({"type": "status", "data": {"description": "Done.", "done": True}})
            return (
                f"[SUPERPOWERS:PHASE:SPEC_REVIEW]\n\n"
                f"**Spec saved:** `{spec_path}`\n"
                f"**Storage:** {storage_label}\n\n"
                f"\n\n[SUPERPOWERS:PHASE:COMPLETE] Spec written. Reply **cook** to continue to review, or give feedback to revise."
                f"\n[SUPERPOWERS:MODE:ASK]"
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
            __chat_id__=__chat_id__, __message_id__=__message_id__, __messages__=__messages__,
        )
        output += review_result
        if __event_emitter__:
            await __event_emitter__({"type": "status", "data": {"description": "Done.", "done": True}})
        return output + "\n[SUPERPOWERS:MODE:COOK]"

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
        __messages__: list = None,
    ) -> str:
        """
        Runs a sub-agent reviewer pass against a saved spec document.

        Args:
            spec_path: Path to the spec markdown file to review.

        If the spec or plan contains code samples, call analyze_code on them
        before returning your verdict.

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

        user_id = (__user__ or {}).get("id", "")
        resolved_path, _, zone_relative_path = self._resolve_path(self.valves.SPEC_DIR, os.path.basename(spec_path), user_id)
        read_path = resolved_path if (self.valves.FILESHED_COMPATIBLE and zone_relative_path) else spec_path
        try:
            with open(read_path, "r", encoding="utf-8") as f:
                spec_content = f.read()
        except OSError as e:
            return (
                f"[SUPERPOWERS:PHASE:FATAL_ERROR]\n\n"
                f"Could not read spec file `{read_path}`: {e}"
                f"\n\n[SUPERPOWERS:AUTO-CONTINUE] Proceed immediately to the next step without waiting for user input."
            )

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

        valid, review = self._validate_output(review, "raw")
        if not valid:
            return (
                f"[SUPERPOWERS:PHASE:FATAL_ERROR]\n\n"
                f"Spec reviewer sub-agent output failed validation (refusal detected)."
                f"\n\n[SUPERPOWERS:AUTO-CONTINUE] Proceed immediately to the next step without waiting for user input."
            )

        approved = "BLOCKED" not in review

        if self._get_mode(__messages__) == "ask":
            if __event_emitter__:
                await __event_emitter__({"type": "status", "data": {"description": "Done.", "done": True}})
            if approved:
                return (
                    f"{review}\n\n"
                    f"---\n\n"
                    f"**Spec is approved.** Review the full document at:\n"
                    f"`{spec_path}`\n\n"
                    f"[SUPERPOWERS:PHASE:COMPLETE] Spec approved. Reply **cook** to continue to plan, or give feedback to revise."
                    f"\n[SUPERPOWERS:MODE:ASK]"
                )
            else:
                return (
                    f"{review}\n\n"
                    f"---\n\n"
                    f"**Issues found in spec.** Address the blocking issues above (maximum two), then call `review_spec` again. Fix only what is listed — do not attempt to fix anything else."
                    f"\n\n[SUPERPOWERS:PHASE:COMPLETE] Issues found. Address them, then call review_spec again."
                    f"\n[SUPERPOWERS:MODE:ASK]"
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
                f"\n[SUPERPOWERS:MODE:COOK]"
            )
        else:
            if __event_emitter__:
                await __event_emitter__({"type": "status", "data": {"description": "Done.", "done": True}})
            return (
                f"{review}\n\n"
                f"---\n\n"
                f"**Issues found in spec.** Address the blocking issues above (maximum two), then call `review_spec` again. Fix only what is listed — do not attempt to fix anything else."
                f"\n\n[SUPERPOWERS:AUTO-CONTINUE] Proceed immediately to the next step without waiting for user input."
                f"\n[SUPERPOWERS:MODE:COOK]"
            )

    async def write_plan(
        self,
        spec_path: str,
        feature_name: str,
        revision_notes: str = "",
        __user__: dict = None,
        __metadata__: dict = None,
        __event_emitter__: typing.Callable[[dict], typing.Any] = None,
        __request__=None,
        __model__: dict = None,
        __event_call__=None,
        __chat_id__: str = "",
        __message_id__: str = "",
        __messages__: list = None,
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
        user_id = (__user__ or {}).get("id", "")
        resolved_spec, _, spec_zone_path = self._resolve_path(self.valves.SPEC_DIR, os.path.basename(spec_path), user_id)
        read_spec_path = resolved_spec if (self.valves.FILESHED_COMPATIBLE and spec_zone_path) else spec_path
        try:
            with open(read_spec_path, "r", encoding="utf-8") as f:
                spec_content = f.read()
        except OSError as e:
            return (
                f"[SUPERPOWERS:PHASE:FATAL_ERROR]\n\n"
                f"Could not read spec file `{read_spec_path}`: {e}"
                f"\n\n[SUPERPOWERS:AUTO-CONTINUE] Proceed immediately to the next step without waiting for user input."
            )

        today = date.today().isoformat()
        slug = feature_name.lower().replace(" ", "-")
        filename = f"{today}-{slug}.md"
        user_id = (__user__ or {}).get("id", "") if __user__ else ""
        plan_path, storage_mode, zone_relative_path = self._resolve_path(self.valves.PLAN_DIR, filename, user_id)

        plan_prompt = f"""CRITICAL: You are writing a TDD TASK PLAN, not an implementation.

Code blocks in the plan are CONTRACTS only — they define what must
be built, not how. Every code block must follow these rules:

STUBS ONLY — a stub is:
- A function/class signature with `pass` or
  `raise NotImplementedError`
- A test with a single `assert False, "not yet implemented"` body
- An import block showing expected module structure

NEVER write in a plan code block:
- Any loop (for, while)
- Any conditional logic (if/elif/else) beyond type guards
- Any complete function body beyond pass/raise NotImplementedError
- More than 5 lines of executable code per block
- Working implementations, algorithms, or business logic

If you find yourself writing real logic, stop and replace it
with a stub. The test execution phase writes real code.

You are writing a detailed TDD implementation plan for the feature described in the spec below.

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

        user_prompt = (
            f"Feature: {feature_name}\n\n"
            f"Spec content:\n\n{spec_content}\n\n"
            f"Write the complete implementation plan now. Cover every requirement in the spec. "
            f"Do not stop until all tasks are written."
        )
        if revision_notes:
            user_prompt += f"\n\n## Revision Notes (address these before finalizing):\n{revision_notes}"

        if self.valves.COMPLEXITY == "simple":
            plan_system_prompt = (
                "You are a senior software engineer. Write a minimal TDD "
                "implementation plan. Each major function or component is "
                "a separate task — never collapse the full implementation "
                "into a single task. Minimum 3 tasks, maximum 5. Each task: "
                "one failing test, one implementation step, one commit. "
                "No prose. No preamble."
            )
        elif self.valves.COMPLEXITY == "complex":
            plan_system_prompt = (
                "You are a senior software engineer. Write a detailed TDD "
                "implementation plan. Each task must include: failing test "
                "code, full implementation steps, edge case handling, and "
                "a commit message. Cover all requirements from the spec."
            )
        else:  # medium
            plan_system_prompt = (
                "You are a senior software engineer. Write a TDD "
                "implementation plan. Each task: failing test, "
                "implementation steps, commit message. Be complete "
                "but avoid unnecessary verbosity."
            )

        plan_content = await self._run_sub_agent(
            system_prompt=plan_system_prompt,
            user_prompt=plan_prompt + "\n\n" + user_prompt,
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

        # Validate and strip fenced code block wrapper
        valid, cleaned = self._validate_output(plan_content, "plan")
        if not valid:
            return (
                f"[SUPERPOWERS:PHASE:FATAL_ERROR]\n\n"
                f"Plan sub-agent output failed validation (refusal or missing required structure)."
                f"\n\n[SUPERPOWERS:AUTO-CONTINUE] Proceed immediately to the next step without waiting for user input."
            )

        ok, err = self._atomic_write(plan_path, cleaned)
        if not ok:
            return (
                f"[SUPERPOWERS:PHASE:FATAL_ERROR]\n\n"
                f"Could not save plan file to `{plan_path}`: {err}"
                f"\n\n[SUPERPOWERS:AUTO-CONTINUE] Proceed immediately to the next step without waiting for user input."
            )

        storage_label = "Fileshed Storage zone (superpowers/)" if storage_mode == "fileshed" else "Standalone path"

        if self.valves.COMPLEXITY == "simple" and self._get_mode(__messages__) == "cook":
            if __event_emitter__:
                await __event_emitter__({"type": "status", "data": {"description": "Done.", "done": True}})
            return (
                f"[SUPERPOWERS:PHASE:PLAN_DONE]\n\n"
                f"**Plan saved:** `{plan_path}`\n"
                f"**Mode:** simple — skipping review, proceeding to execute task 1.\n\n"
                f"[SUPERPOWERS:AUTO-CONTINUE]"
            )

        if self._get_mode(__messages__) == "ask":
            if __event_emitter__:
                await __event_emitter__({"type": "status", "data": {"description": "Done.", "done": True}})
            return (
                f"[SUPERPOWERS:PHASE:PLAN_REVIEW]\n\n"
                f"**Plan saved:** `{plan_path}`\n"
                f"**Storage:** {storage_label}\n\n"
                f"\n\n[SUPERPOWERS:PHASE:COMPLETE] Plan written. Reply **cook** to continue to review, or give feedback to revise."
                f"\n[SUPERPOWERS:MODE:ASK]"
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
            __chat_id__=__chat_id__, __message_id__=__message_id__, __messages__=__messages__,
        )
        output += review_result
        if __event_emitter__:
            await __event_emitter__({"type": "status", "data": {"description": "Done.", "done": True}})
        return output + "\n[SUPERPOWERS:MODE:COOK]"

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
        __messages__: list = None,
    ) -> str:
        """
        Runs a sub-agent reviewer pass against a saved implementation plan.

        Args:
            plan_path: Path to the plan markdown file to review.

        If the spec or plan contains code samples, call analyze_code on them
        before returning your verdict.

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
- Stub enforcement: BLOCK if any single code block contains a loop (for/while), conditional logic beyond type guards, more than 5 lines of executable code, or a complete working implementation. Stubs with pass/raise NotImplementedError and test skeletons with assert False are correct and expected.

Only flag issues that would cause real problems during implementation.
Approve unless there are serious gaps.

Output format:
## Plan Review
**Status:** APPROVED or BLOCKED
**Issues (if any):**
- [list issues, or omit section if none]
**Recommendations (advisory):**
- [optional suggestions, clearly marked as non-blocking]"""

        user_id = (__user__ or {}).get("id", "")
        resolved_path, _, zone_relative_path = self._resolve_path(self.valves.PLAN_DIR, os.path.basename(plan_path), user_id)
        read_path = resolved_path if (self.valves.FILESHED_COMPATIBLE and zone_relative_path) else plan_path
        try:
            with open(read_path, "r", encoding="utf-8") as f:
                plan_content = f.read()
        except OSError as e:
            return (
                f"[SUPERPOWERS:PHASE:FATAL_ERROR]\n\n"
                f"Could not read plan file `{read_path}`: {e}"
                f"\n\n[SUPERPOWERS:AUTO-CONTINUE] Proceed immediately to the next step without waiting for user input."
            )

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

        valid, review = self._validate_output(review, "raw")
        if not valid:
            return (
                f"[SUPERPOWERS:PHASE:FATAL_ERROR]\n\n"
                f"Plan reviewer sub-agent output failed validation (refusal detected)."
                f"\n\n[SUPERPOWERS:AUTO-CONTINUE] Proceed immediately to the next step without waiting for user input."
            )

        approved = "BLOCKED" not in review

        if not approved:
            self._plan_revision_count += 1
            if self._plan_revision_count >= self.MAX_PLAN_REVISIONS:
                self._plan_revision_count = 0
                review = review.replace("BLOCKED", "APPROVED")
                review += (
                    "\n\n[SUPERPOWERS:WARNING] Plan force-approved after "
                    f"{self.MAX_PLAN_REVISIONS} revision attempts. "
                    "Remaining issues are advisory — address during "
                    "execution if tests fail."
                )
                approved = True

        if self._get_mode(__messages__) == "ask":
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
                    f"\n[SUPERPOWERS:MODE:ASK]"
                )
            else:
                return (
                    f"[SUPERPOWERS:PHASE:PLAN_REVIEW]\n\n"
                    f"{review}\n\n"
                    f"---\n\n"
                    f"**Issues found in plan.** Call `write_plan` again with the same `spec_path` and `feature_name`, "
                    f"passing the blocking issues above as `revision_notes`. Do not edit the file directly."
                    f"\n\n[SUPERPOWERS:PHASE:COMPLETE] Issues found. Call write_plan again with revision_notes."
                    f"\n[SUPERPOWERS:MODE:ASK]"
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
                f"\n[SUPERPOWERS:MODE:COOK]"
            )
        else:
            if __event_emitter__:
                await __event_emitter__({"type": "status", "data": {"description": "Done.", "done": True}})
            return (
                f"[SUPERPOWERS:PHASE:PLAN_REVIEW]\n\n"
                f"{review}\n\n"
                f"---\n\n"
                f"**Issues found in plan.** Call `write_plan` again with the same `spec_path` and `feature_name`, "
                f"passing the blocking issues above as `revision_notes`. Do not edit the file directly."
                f"\n\n[SUPERPOWERS:AUTO-CONTINUE] Proceed immediately to the next step without waiting for user input."
                f"\n[SUPERPOWERS:MODE:COOK]"
            )

    def _validate_code(self, content: str) -> list:
        """
        Extract Python code blocks from content and validate each one.
        Returns a list of issue strings. Empty list means all clean.

        Runs two passes:
        1. ast.parse — catches all syntax errors (always available)
        2. pyflakes  — catches undefined names, missing imports,
                       scope issues, duplicate definitions
                       (gracefully skipped if not installed)
        """
        import ast

        issues = []

        # Extract all ```python ... ``` blocks from the content
        code_blocks = re.findall(
            r"```python\s*\n(.*?)```",
            content,
            re.DOTALL
        )

        if not code_blocks:
            return issues

        for i, code in enumerate(code_blocks, start=1):
            label = f"Block {i}"

            # Pass 1: syntax check via ast.parse
            try:
                ast.parse(code)
            except SyntaxError as e:
                issues.append(
                    f"{label} syntax error at line {e.lineno}: {e.msg}"
                )
                continue  # No point running pyflakes on broken syntax

            # Pass 2: static analysis via pyflakes (optional)
            try:
                from pyflakes import api as pyflakes_api

                class _StringReporter:
                    def __init__(self):
                        self.messages = []
                    def unexpectedError(self, filename, msg):
                        self.messages.append(f"unexpected error: {msg}")
                    def syntaxError(self, filename, msg, lineno, offset, text):
                        self.messages.append(f"syntax error line {lineno}: {msg}")
                    def flake(self, message):
                        self.messages.append(str(message))

                reporter = _StringReporter()
                pyflakes_api.check(code, filename=f"<block{i}>",
                                   reporter=reporter)

                for msg in reporter.messages:
                    if any(k in msg for k in [
                        "undefined name",
                        "redefinition of unused",
                        "imported but unused",
                        "local variable",
                        "referenced before assignment",
                    ]):
                        issues.append(f"{label}: {msg}")

            except ImportError:
                pass  # pyflakes not installed — skip silently

        return issues

    def _validate_output(self, content: str, output_type: str) -> tuple:
        """
        Validate sub-agent output for refusals and required structure.
        Returns (is_valid: bool, cleaned_content: str).
        cleaned_content has markdown fences stripped on success.
        output_type: "spec", "plan", or "raw"
        """
        refusal_phrases = [
            "i cannot", "i apologize", "i'm sorry", "i am sorry",
            "i'm unable", "i am unable", "i don't think i can",
            "i am not able", "i can't",
        ]
        preview = content[:500].lower()
        for phrase in refusal_phrases:
            if phrase in preview:
                return False, content

        # Strip fenced code block wrapper
        cleaned = content.strip()
        if cleaned.startswith("```markdown"):
            cleaned = cleaned[len("```markdown"):].lstrip("\n")
        if cleaned.startswith("```"):
            cleaned = cleaned[3:].lstrip("\n")
        if cleaned.endswith("```"):
            cleaned = cleaned[:-3].rstrip()

        if output_type == "spec":
            required = ["## Goal", "## Approach", "## Requirements"]
            for marker in required:
                if marker not in cleaned:
                    return False, cleaned

        elif output_type == "plan":
            if "### Task 1:" not in cleaned or "Step" not in cleaned:
                return False, cleaned

        return True, cleaned

    def _atomic_write(self, path: str, content: str) -> tuple:
        """
        Write content to path atomically using a temp file + os.replace().
        Returns (True, "") on success, (False, error_msg) on failure.
        """
        import tempfile
        dir_name = os.path.dirname(path)
        try:
            fd, tmp_path = tempfile.mkstemp(dir=dir_name)
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as f:
                    f.write(content)
                os.replace(tmp_path, path)
            except Exception:
                try:
                    os.unlink(tmp_path)
                except OSError:
                    pass
                raise
        except OSError as e:
            return False, str(e)
        return True, ""

    def _get_scratch_path(self, feature_name: str, user_id: str) -> str:
        path, _, _ = self._resolve_path(
            "scratch", f"{feature_name}.scratch", user_id
        )
        return path

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
        __messages__: list = None,
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
        user_id = (__user__ or {}).get("id", "")
        resolved_path, _, zone_relative_path = self._resolve_path(self.valves.PLAN_DIR, os.path.basename(plan_path), user_id)
        read_path = resolved_path if (self.valves.FILESHED_COMPATIBLE and zone_relative_path) else plan_path
        try:
            with open(read_path, "r", encoding="utf-8") as f:
                plan_content = f.read()
        except OSError as e:
            return (
                f"[SUPERPOWERS:PHASE:FATAL_ERROR]\n\n"
                f"Could not read plan file `{read_path}`: {e}"
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

        execute_system_prompt = (
            "You are a senior software engineer implementing a single "
            "task from a TDD plan.\n\n"
            "Output format — follow this exactly:\n"
            "1. The complete failing test file, in a fenced code block "
            "   with the correct language tag.\n"
            "2. The complete implementation file that makes it pass, "
            "   in a fenced code block.\n"
            "3. A single line starting with COMMIT: followed by the "
            "   commit message.\n\n"
            "Rules:\n"
            "- Every file must be complete. No ellipsis, no truncation, "
            "  no 'rest remains unchanged'.\n"
            "- No preamble before the first code block.\n"
            "- No explanation between code blocks.\n"
            "- No prose after the COMMIT line.\n"
            "- If the task specifies bash, write bash. If Python, write "
            "  Python. Match the language in the plan.\n"
            "- The test must fail before the implementation exists. "
            "  Write it that way."
        )

        result = await self._run_sub_agent(
            system_prompt=execute_system_prompt,
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

        combined_result = result

        # Validate sub-agent output before writing anything
        valid, combined_result = self._validate_output(combined_result, "raw")
        if not valid:
            return (
                f"[SUPERPOWERS:PHASE:FATAL_ERROR]\n\n"
                f"Execute sub-agent output failed validation (refusal detected) for task {task_number}."
                f"\n\n[SUPERPOWERS:AUTO-CONTINUE] Proceed immediately to the next step without waiting for user input."
            )

        # Scratch file persistence
        feature_name = os.path.basename(plan_path)
        feature_name = re.sub(r"^\d{4}-\d{2}-\d{2}-", "", feature_name)
        feature_name = feature_name.removesuffix(".md")
        scratch_path = self._get_scratch_path(feature_name, user_id)
        if task_number == 1:
            ok, err = self._atomic_write(
                scratch_path,
                f"# {feature_name} — Scratch Build\n\n## Task 1\n\n{combined_result}\n\n"
            )
            if not ok:
                return (
                    f"[SUPERPOWERS:PHASE:FATAL_ERROR]\n\n"
                    f"Could not write scratch file `{scratch_path}`: {err}"
                    f"\n\n[SUPERPOWERS:AUTO-CONTINUE] Proceed immediately to the next step without waiting for user input."
                )
        else:
            try:
                with open(scratch_path, "a", encoding="utf-8") as f:
                    f.write(f"## Task {task_number}\n\n{combined_result}\n\n")
            except OSError as _e:
                return (
                    f"[SUPERPOWERS:PHASE:FATAL_ERROR]\n\n"
                    f"Could not append to scratch file `{scratch_path}`: {_e}"
                    f"\n\n[SUPERPOWERS:AUTO-CONTINUE] Proceed immediately to the next step without waiting for user input."
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
            f"{combined_result}\n\n"
            f"**Scratch file:** `{scratch_path}`\n"
            f"`shed_read` it in Fileshed to see the full accumulated "
            f"output across all tasks.\n\n"
            f"**Tip:** If Fileshed is installed, verify the plan file with:\n"
            f"`shed_exec(zone=\"storage\", "
            f"path=\"superpowers/plans/{os.path.basename(plan_path)}\", "
            f"cmd=\"cat\")`"
        )

        # Validate all code blocks in the combined output
        validation_issues = self._validate_code(tdd_context)
        if validation_issues:
            issue_list = "\n".join(f"  - {iss}" for iss in validation_issues)
            tdd_context += (
                f"\n\n---\n\n"
                f"[SUPERPOWERS:VALIDATION:FAILED]\n\n"
                f"**Static analysis found {len(validation_issues)} issue(s) "
                f"in generated code blocks. These must be fixed before "
                f"running tests:**\n\n"
                f"{issue_list}\n\n"
                f"Fix the issues above, then re-run this task before "
                f"proceeding to the next one."
            )
        else:
            tdd_context += (
                f"\n\n[SUPERPOWERS:VALIDATION:PASSED] "
                f"Syntax and static analysis clean."
            )

        # On final task completion, append scratch read instruction
        total_tasks = len(re.findall(r"^### Task ", plan_content, re.MULTILINE))
        if task_number == total_tasks:
            tdd_context += (
                f"\n\n[SUPERPOWERS:TASKS:COMPLETE] All tasks executed. "
                f"Read the scratch file and output the complete final "
                f"implementation in a single fenced code block:\n"
                f"`shed_read zone=storage "
                f"path=superpowers/scratch/{feature_name}.scratch`"
            )

        if __event_emitter__:
            await __event_emitter__({"type": "status", "data": {"description": "Done.", "done": True}})
        mode = self._get_mode(__messages__)
        return (
            tdd_context
            + "\n\n[SUPERPOWERS:AUTO-CONTINUE] Proceed immediately to the next step without waiting for user input."
            + f"\n[SUPERPOWERS:MODE:{mode.upper()}]"
        )
