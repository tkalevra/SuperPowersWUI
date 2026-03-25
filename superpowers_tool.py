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
        ENABLE_SHELLCHECK: bool = Field(
            default=True,
            description=(
                "Run shellcheck on bash/sh code blocks during validation if shellcheck "
                "is installed. Falls back to pattern matching if not found."
            ),
        )
        ENABLE_ESLINT: bool = Field(
            default=False,
            description=(
                "Run eslint on JavaScript/TypeScript code blocks during validation if "
                "eslint is installed. Falls back to pattern matching if not found."
            ),
        )
        VALIDATION_TIMEOUT: int = Field(
            default=5,
            description="Timeout in seconds for external validator subprocess calls (shellcheck, eslint, etc.).",
        )
        RUN_TESTS_IN_EXECUTE: bool = Field(
            default=False,
            description=(
                "If True, execute_task will attempt to run generated test and "
                "implementation code in a sandboxed environment to verify TDD cycle."
            ),
        )
        ENABLE_MAN_PAGE_VALIDATION: bool = Field(
            default=True,
            description=(
                "Query man pages for command validation as a fallback when a command "
                "is not in the curated KB. Requires man to be available in the container."
            ),
        )
        AUTO_REVALIDATE_AFTER_LEARN: bool = Field(
            default=True,
            description=(
                "After a successful -l learn, scan recent conversation messages for "
                "bash/shell code that uses the newly learned command and report any issues."
            ),
        )
        BATCH_LEARN_MODE: bool = Field(
            default=False,
            description=(
                "Disable auto-revalidation during batch learning. Toggle at runtime with "
                "'skillstack -batch on/off'. Re-enable and run 'skillstack -revalidate' "
                "when done to check all recently learned commands at once."
            ),
        )
        CURATED_KB_PATH: str = Field(
            default="/mnt/skills/public/bash-validation/curated_kb.json",
            description=(
                "Path to the curated bash command knowledge base JSON file. "
                "For non-Docker installs, point this at the curated_kb.json "
                "included in the SuperpowersWUI repo under bash-validation/."
            ),
        )

    def __init__(self):
        self.valves = self.Valves()
        self.MAX_PLAN_REVISIONS = 2
        self._plan_revision_count = 0
        self._recently_learned: list = []
        self._batch_mode: bool = None  # None = use valve; True/False = runtime override

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

CRITICAL IMPLEMENTATION GUIDANCE:
For ANY requirement that involves an external tool, shell command,
API call, or library function, you MUST include ALL of the following
in the requirement text — not in a separate section:
1. The exact command, function signature, or API method
2. A concrete working example with real arguments (not placeholders)
3. The expected output or exit code

GOOD requirement:
"Transfer file via SFTP batch mode:
```
sftp -b - user@host <<'EOF'
cd /remote/path
put /local/file.txt
bye
EOF
```
Exit 0 on success, non-zero on failure. Do NOT use sftp with -r flag
(does not exist); use put -r inside the batch block for directories."

BAD requirement (will be rejected):
"Transfer file via SFTP"

GOOD requirement:
"Parse JSON with error handling:
```python
import json
try:
    data = json.loads(input_str)
except json.JSONDecodeError as e:
    return None
```"

BAD requirement (will be rejected):
"Parse JSON from input"

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

CRITICAL: DOMAIN-SPECIFIC VALIDATION
For every code block or command reference in the plan, verify:

1. Command/API correctness:
   - Commands exist and the flags shown are valid (e.g. sftp has no -r flag)
   - API calls use the correct method signatures
   - Library functions are called with the right argument types and order

2. Variable and naming consistency:
   - Variable names used consistently throughout all tasks
   - Environment variable names match any referenced documentation
   - No shadowing or silent redefinition across tasks

3. Logic soundness:
   - Control flow cannot have unreachable branches
   - Conditionals are not trivially always-true or always-false
   - Error handling paths actually handle the error (no silent swallow)

4. Language-specific patterns:
   Bash: SFTP must use batch mode (-b or heredoc), not interactive stdin piping.
         SSH RemoteCommand or ProxyJump patterns must be syntactically valid.
   Python: imports must match usage; types passed to functions must be consistent.
   JavaScript: async functions must be awaited; promises must have .catch() or try/catch.

BLOCK the plan if you find:
- A command flag that does not exist (e.g. sftp -r, grep -P on macOS without ggrep)
- Variable name mismatch between tasks or between docs and code
- Logic that cannot possibly produce the described outcome
- Hardcoded secrets or credentials in any code block
- eval/exec usage without explicit justification

Only flag issues that would cause real failures. Approve unless there are serious gaps.

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
        Extract code blocks from content and validate with language-specific
        tools. Returns a list of issue strings. Empty list means all clean.
        """
        issues = []

        code_blocks = re.findall(
            r"^\s*```(\w+)\s*\n(.*?)\n\s*```",
            content,
            re.DOTALL | re.MULTILINE
        )

        if not code_blocks:
            return issues

        for i, (language, code) in enumerate(code_blocks, start=1):
            lang = language.lower()
            label = f"{language.upper()} Block {i}"

            if lang in ("python", "py"):
                issues.extend(self._validate_python(code, label, i))
            elif lang in ("bash", "sh", "shell"):
                issues.extend(self._validate_bash(code, label))
            elif lang in ("javascript", "js", "typescript", "ts"):
                issues.extend(self._validate_javascript(code, label))

            issues.extend(self._validate_generic_patterns(code, label, lang))

        return issues

    def _validate_python(self, code: str, label: str, block_index: int) -> list:
        """Python: ast.parse + optional pyflakes."""
        import ast
        issues = []

        try:
            ast.parse(code)
        except SyntaxError as e:
            issues.append(f"{label} syntax error at line {e.lineno}: {e.msg}")
            return issues  # No point running pyflakes on broken syntax

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
            pyflakes_api.check(code, filename=f"<block{block_index}>", reporter=reporter)
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

    def _get_command_cache_path(self) -> str:
        """Global cache shared across all users/projects."""
        cache_dir = os.path.join(
            self.valves.STORAGE_BASE_PATH, "superpowers", "validation_cache"
        )
        os.makedirs(cache_dir, exist_ok=True)
        return os.path.join(cache_dir, "command_knowledge.json")

    def _load_command_cache(self) -> dict:
        """Load cached command knowledge."""
        cache_path = self._get_command_cache_path()
        if not os.path.exists(cache_path):
            return {}
        try:
            with open(cache_path, "r", encoding="utf-8") as f:
                return json.load(f)
        except (OSError, json.JSONDecodeError):
            return {}

    def _save_command_cache(self, cache: dict) -> None:
        """Atomically persist the command cache to disk. Non-fatal on failure."""
        try:
            self._atomic_write(self._get_command_cache_path(), json.dumps(cache, indent=2))
        except Exception:
            pass

    def _fetch_online_docs(self, command: str) -> dict:
        """
        Fetch from authoritative online sources.
        Priority: official project docs → man7.org → die.net (groff plaintext).
        tldr-pages are NOT used: they are simplified human tutorials, not
        authoritative flag references, and produce false positives in validation.
        """
        import urllib.request
        import urllib.error

        # Official project documentation (plaintext groff/troff where available)
        official_docs: dict = {
            "rsync": "https://download.samba.org/pub/rsync/rsync.1",
        }

        # Linux man-pages project (HTML — strip tags before parsing)
        man7_urls: dict = {
            "rsync": "https://man7.org/linux/man-pages/man1/rsync.1.html",
            "ssh":   "https://man7.org/linux/man-pages/man1/ssh.1.html",
            "sftp":  "https://man7.org/linux/man-pages/man1/sftp.1.html",
            "scp":   "https://man7.org/linux/man-pages/man1/scp.1.html",
            "curl":  "https://man7.org/linux/man-pages/man1/curl.1.html",
            "wget":  "https://man7.org/linux/man-pages/man1/wget.1.html",
            "tar":   "https://man7.org/linux/man-pages/man1/tar.1.html",
            "find":  "https://man7.org/linux/man-pages/man1/find.1.html",
            "grep":  "https://man7.org/linux/man-pages/man1/grep.1.html",
        }

        # die.net mirrors groff plaintext — cleaner flag extraction than HTML
        dienet_url = f"https://linux.die.net/man/1/{command}"

        candidates = []
        if command in official_docs:
            candidates.append(("official", official_docs[command]))
        if command in man7_urls:
            candidates.append(("man7.org", man7_urls[command]))
        candidates.append(("die.net", dienet_url))

        for source_label, url in candidates:
            try:
                req = urllib.request.Request(
                    url,
                    headers={"User-Agent": "superpowers-owui/1.0 (command validation)"},
                )
                with urllib.request.urlopen(req, timeout=self.valves.VALIDATION_TIMEOUT) as resp:
                    raw = resp.read().decode("utf-8", errors="replace")
            except Exception:
                continue

            # Strip HTML if needed
            text = re.sub(r"<[^>]+>", " ", raw) if "<" in raw else raw
            # Collapse whitespace so multi-line option blocks parse cleanly
            text = re.sub(r"[ \t]+", " ", text)

            # Extract single-char flags from OPTIONS / SYNOPSIS sections
            flags = list(set(re.findall(r"\s(-[a-zA-Z])\b", text)))
            # Long flags (--word)
            long_flags = list(set(re.findall(r"(--[a-z][a-z0-9-]+)", text)))
            subcommands = list(set(re.findall(
                r"\b(put|get|cd|lcd|ls|pwd|bye|quit|mput|mget|mkdir|rmdir)\b", text
            )))

            if flags or long_flags:
                return {
                    "valid_flags": sorted(flags + long_flags),
                    "valid_subcommands": sorted(subcommands),
                    "source": "online_docs",
                    "cached_at": date.today().isoformat(),
                    "note": f"Sourced from {source_label}: {url}",
                }

        return None

    def _fetch_from_url(self, command: str, url: str) -> dict:
        """Fetch and parse command documentation from a custom URL."""
        import urllib.request
        import urllib.error

        try:
            with urllib.request.urlopen(url, timeout=self.valves.VALIDATION_TIMEOUT) as resp:
                text = resp.read().decode("utf-8", errors="replace")
            # Strip HTML tags if present
            text = re.sub(r"<[^>]+>", " ", text)
            flags = list(set(re.findall(r"\s(-[a-zA-Z])\b", text)))
            subcommands = list(set(re.findall(
                r"\b(put|get|cd|lcd|ls|pwd|bye|quit|mput|mget|mkdir|rmdir)\b", text
            )))
            return {
                "valid_flags": sorted(flags),
                "valid_subcommands": sorted(subcommands),
                "source": "online_docs",
                "cached_at": date.today().isoformat(),
                "note": f"Sourced from {url}",
            }
        except Exception:
            return None

    def _update_cache_with_authority(self, cache: dict, command: str, new_data: dict) -> bool:
        """
        Update cache only if new source has higher or equal authority.
        Returns True if cache was updated.
        Trust hierarchy: curated_kb (1.0) > man_pages (0.9) > online_docs (0.8)
        """
        trust_levels = {
            "curated_kb": 1.0,
            "man_pages": 0.9,
            "online_docs": 0.8,
            "unknown": 0.0,
        }
        new_trust = trust_levels.get(new_data.get("source", "unknown"), 0.0)

        if command not in cache:
            cache[command] = new_data
            cache[command]["trust_level"] = new_trust
            return True

        existing_trust = cache[command].get("trust_level", 0.0)

        if new_trust > existing_trust:
            cache[command].update(new_data)
            cache[command]["trust_level"] = new_trust
            return True

        if new_trust == existing_trust:
            updated = False
            for key in ("valid_flags", "invalid_flags", "valid_subcommands", "invalid_subcommands"):
                if key in new_data:
                    existing_set = set(cache[command].get(key, []))
                    new_set = set(new_data.get(key, []))
                    merged = existing_set | new_set
                    if merged != existing_set:
                        cache[command][key] = sorted(merged)
                        updated = True
            return updated

        return False

    def _check_curated_kb(self, command: str) -> dict:
        """Check human-curated knowledge base."""
        kb_path = self.valves.CURATED_KB_PATH
        try:
            with open(kb_path, "r", encoding="utf-8") as f:
                kb = json.load(f)
            if command in kb:
                result = kb[command].copy()
                result["source"] = "curated_kb"
                result["cached_at"] = date.today().isoformat()
                return result
        except (OSError, json.JSONDecodeError):
            pass
        return None

    def _check_man_page(self, command: str, args: str) -> dict:
        """Parse man page to extract valid/invalid flags and subcommands."""
        import subprocess

        if not self.valves.ENABLE_MAN_PAGE_VALIDATION:
            return None

        try:
            result = subprocess.run(
                ["man", command],
                capture_output=True,
                text=True,
                timeout=3,
            )
            if result.returncode != 0:
                return None

            man_text = result.stdout

            flags_section = re.search(
                r"OPTIONS(.*?)(?:EXAMPLES|DESCRIPTION|SEE ALSO|$)",
                man_text,
                re.DOTALL | re.IGNORECASE,
            )
            valid_flags = []
            if flags_section:
                valid_flags = list(set(re.findall(r"\s-([a-zA-Z])\b", flags_section.group(1))))

            commands_section = re.search(
                r"(INTERACTIVE )?COMMANDS(.*?)(?:EXAMPLES|OPTIONS|SEE ALSO|$)",
                man_text,
                re.DOTALL | re.IGNORECASE,
            )
            valid_subcommands = []
            if commands_section:
                valid_subcommands = list(set(re.findall(
                    r"^\s+(put|get|cd|lcd|ls|pwd|bye|quit|mput|mget|mkdir|rmdir)\b",
                    commands_section.group(2),
                    re.MULTILINE,
                )))

            used_flags = re.findall(r"-([a-zA-Z])", args)
            used_subcommands = re.findall(
                r"\b(put|get|mput|mget|cd|lcd|ls|pwd|bye|quit|mkdir|rmdir)\b", args
            )

            return {
                "valid_flags": [f"-{f}" for f in valid_flags],
                "invalid_flags": [f"-{f}" for f in used_flags if f not in valid_flags],
                "valid_subcommands": valid_subcommands,
                "invalid_subcommands": [c for c in used_subcommands if c not in valid_subcommands],
                "source": "man_pages",
                "cached_at": date.today().isoformat(),
            }

        except (subprocess.TimeoutExpired, FileNotFoundError):
            return None

    def _validate_bash(self, code: str, label: str) -> list:
        """Bash/shell: cache-driven command validation + shellcheck + manual pattern checks."""
        import subprocess
        import json as _json
        issues = []

        # --- Cache-driven command validation ---
        cache = self._load_command_cache()
        cache_updated = False

        bash_keywords = {
            "if", "then", "else", "elif", "fi", "for", "while", "do", "done",
            "case", "esac", "function", "in", "return", "local", "export",
            "echo", "read", "exit", "true", "false", "set", "unset", "shift",
        }
        all_commands = set(re.findall(r'\b([a-z][a-z0-9_-]+)\b', code))
        external_commands = all_commands - bash_keywords

        for cmd in external_commands:
            pattern = rf'\b{re.escape(cmd)}\b\s+(.*?)(?:\n|;|&&|\|\||$)'
            match = re.search(pattern, code)
            if not match:
                continue
            args = match.group(1).strip()

            # Tier 1: curated KB
            new_validation = self._check_curated_kb(cmd)
            # Tier 2: man pages on cache miss
            if not new_validation:
                new_validation = self._check_man_page(cmd, args)

            if new_validation:
                if self._update_cache_with_authority(cache, cmd, new_validation):
                    cache_updated = True

            if cmd in cache:
                cmd_info = cache[cmd]
                if cmd_info.get("trust_level", 0.0) >= 0.8:
                    source = cmd_info.get("source", "unknown")
                    valid_flags = cmd_info.get("valid_flags", [])
                    if valid_flags:
                        # Allowlist: flag anything not in the known-good set
                        used_flags = re.findall(r'-([a-zA-Z])', args)
                        for flag in used_flags:
                            if f"-{flag}" not in valid_flags:
                                issues.append(
                                    f"{label}: {cmd} -{flag} not in known valid flags (source: {source})"
                                )
                    else:
                        # Blocklist fallback for entries that only have invalid_flags
                        for flag in cmd_info.get("invalid_flags", []):
                            if flag in args:
                                issues.append(
                                    f"{label}: {cmd} {flag} is not a valid flag (source: {source})"
                                )
                    for subcmd in cmd_info.get("invalid_subcommands", []):
                        if re.search(rf'\b{re.escape(subcmd)}\b', args):
                            note = cmd_info.get("note", "check man page")
                            issues.append(
                                f"{label}: '{cmd} {subcmd}' — {note} (source: {source})"
                            )

        if cache_updated:
            try:
                self._atomic_write(
                    self._get_command_cache_path(),
                    json.dumps(cache, indent=2)
                )
            except Exception:
                pass  # Non-fatal

        # --- shellcheck ---
        if self.valves.ENABLE_SHELLCHECK:
            try:
                result = subprocess.run(
                    ["shellcheck", "-f", "json", "-S", "warning", "-"],
                    input=code,
                    capture_output=True,
                    text=True,
                    timeout=self.valves.VALIDATION_TIMEOUT,
                )
                if result.stdout:
                    try:
                        for item in _json.loads(result.stdout):
                            issues.append(
                                f"{label} line {item['line']}: [{item['code']}] {item['message']}"
                            )
                    except (_json.JSONDecodeError, KeyError):
                        pass
            except FileNotFoundError:
                pass  # shellcheck not installed
            except subprocess.TimeoutExpired:
                issues.append(
                    f"{label}: shellcheck timed out (>{self.valves.VALIDATION_TIMEOUT}s)"
                )

        # --- Manual pattern checks ---
        if "sftp -r" in code:
            issues.append(
                f"{label}: sftp has no -r flag — use 'put -r' inside batch mode instead"
            )
        if re.search(r"\beval\s+", code):
            issues.append(f"{label}: uses eval (security risk and hard to debug)")
        unquoted = re.findall(r'(?<!["\'])\$\{?\w+\}?(?!["\'])', code)
        if unquoted:
            issues.append(
                f"{label}: {len(unquoted)} potentially unquoted variable(s) "
                f"(word splitting risk): {', '.join(unquoted[:5])}"
            )
        if re.search(r'(REMOTE|DEST|TARGET|SSH).*PATH.*=.*\$\((whoami|hostname|pwd|id)\)', code):
            issues.append(
                f"{label}: command substitution in remote path variable runs locally, not on remote host"
            )
        declared = set(re.findall(r'\b([A-Z_][A-Z0-9_]*)=', code))
        used = set(re.findall(r'\$\{?([A-Z_][A-Z0-9_]*)\}?', code))
        common_env = {"PATH", "HOME", "USER", "PWD", "SHELL", "TERM", "LANG", "LC_ALL"}
        undefined = (used - declared) - common_env
        if undefined:
            issues.append(
                f"{label}: variables used but not defined in this block: "
                f"{', '.join(sorted(undefined)[:5])}"
            )

        return issues

    def _validate_javascript(self, code: str, label: str) -> list:
        """JavaScript/TypeScript: eslint if available, plus manual patterns."""
        import subprocess
        import json as _json
        import tempfile
        issues = []

        if self.valves.ENABLE_ESLINT:
            fd, tmp_path = None, None
            try:
                fd, tmp_path = tempfile.mkstemp(suffix=".js")
                with os.fdopen(fd, "w", encoding="utf-8") as f:
                    f.write(code)
                fd = None  # fdopen took ownership
                result = subprocess.run(
                    [
                        "eslint", "--format=json", "--no-eslintrc",
                        "--rule", '{"no-undef": "warn", "no-unused-vars": "warn"}',
                        tmp_path,
                    ],
                    capture_output=True,
                    text=True,
                    timeout=self.valves.VALIDATION_TIMEOUT,
                )
                if result.stdout:
                    try:
                        for file_result in _json.loads(result.stdout):
                            for msg in file_result.get("messages", []):
                                issues.append(
                                    f"{label} line {msg['line']}: {msg['message']}"
                                )
                    except (_json.JSONDecodeError, KeyError):
                        pass
            except FileNotFoundError:
                pass  # eslint not installed
            except subprocess.TimeoutExpired:
                issues.append(
                    f"{label}: eslint timed out (>{self.valves.VALIDATION_TIMEOUT}s)"
                )
            finally:
                if fd is not None:
                    try:
                        os.close(fd)
                    except OSError:
                        pass
                if tmp_path is not None:
                    try:
                        os.unlink(tmp_path)
                    except OSError:
                        pass

        # Manual pattern checks (always run)
        if re.search(r'\.then\s*\([^)]*\)\s*(?!\.catch)', code):
            issues.append(f"{label}: .then() without .catch() — unhandled promise rejection")
        if "eval(" in code:
            issues.append(f"{label}: uses eval() (security risk)")

        return issues

    def _validate_generic_patterns(self, code: str, label: str, language: str) -> list:
        """Cross-language semantic pattern detection."""
        issues = []

        # Empty error handlers
        if re.search(r'except\s*:\s*\n\s*pass', code):
            issues.append(f"{label}: bare except:pass silently swallows all errors")
        if re.search(r'catch\s*\(\w*\)\s*\{\s*\}', code):
            issues.append(f"{label}: empty catch block silently swallows exception")

        # Hardcoded credentials
        cred_match = re.search(
            r'(?i)(password|passwd|secret|api_key|token)\s*=\s*["\'][^"\']{4,}["\']',
            code,
        )
        if cred_match:
            issues.append(
                f"{label}: possible hardcoded credential — {cred_match.group()[:50]}"
            )

        # Unreachable code after return/break/exit
        if re.search(r'\b(return|break|exit|sys\.exit)\b[^\n]*\n[ \t]+\S', code):
            issues.append(f"{label}: code appears after return/break/exit (unreachable)")

        # Self-comparisons that are always true or always false
        if re.search(r'\b(\w+)\s*[=!]=\s*\1\b', code):
            issues.append(
                f"{label}: self-comparison detected (condition always true or always false)"
            )

        # While-true with immediate break (loop body never repeats)
        if re.search(r'while\s+[Tt]rue\s*[:{][\s\S]{0,50}break', code):
            issues.append(f"{label}: while-true with immediate break (loop never repeats)")

        return issues

    def _validate_output(self, content: str, output_type: str) -> tuple:
        """
        Validate sub-agent output for refusals and required structure.
        Returns (is_valid: bool, cleaned_content: str).
        cleaned_content has markdown fences stripped on success.
        output_type: "spec", "plan", or "raw"
        """
        if not content or len(content.strip()) < 50:
            return False, content

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
        except OSError as e:
            return False, str(e)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                f.write(content)
            os.replace(tmp_path, path)
            return True, ""
        except Exception as e:
            try:
                os.close(fd)
            except OSError:
                pass
            try:
                os.unlink(tmp_path)
            except OSError:
                pass
            return False, str(e)

    def _get_scratch_path(self, feature_name: str, user_id: str) -> str:
        path, _, _ = self._resolve_path(
            "scratch", f"{feature_name}.scratch", user_id
        )
        return path

    async def execute_task(
        self,
        plan_path: str,
        task_number: int,
        run_tests: bool = False,
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
            existing = ""
            if os.path.exists(scratch_path):
                try:
                    with open(scratch_path, "r", encoding="utf-8") as f:
                        existing = f.read()
                except OSError as _e:
                    return (
                        f"[SUPERPOWERS:PHASE:FATAL_ERROR]\n\n"
                        f"Could not read scratch file `{scratch_path}`: {_e}"
                        f"\n\n[SUPERPOWERS:AUTO-CONTINUE] Proceed immediately to the next step without waiting for user input."
                    )
            ok, err = self._atomic_write(
                scratch_path,
                existing + f"## Task {task_number}\n\n{combined_result}\n\n"
            )
            if not ok:
                return (
                    f"[SUPERPOWERS:PHASE:FATAL_ERROR]\n\n"
                    f"Could not write scratch file `{scratch_path}`: {err}"
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
                f"\n\n[SUPERPOWERS:VALIDATION:PASSED]\n"
                f"  Syntax validation: Clean\n"
                f"  Static analysis: No issues\n"
                f"  Domain checks: Passed\n"
                f"  Pattern detection: No common errors found"
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

    def _extract_recent_code_from_context(self, messages: list) -> str:
        """
        Scan recent conversation messages (most-recent first) for bash/shell
        fenced code blocks. Returns the first non-empty block found, or "".
        """
        if not messages:
            return ""
        for msg in reversed(messages):
            content = ""
            if isinstance(msg, dict):
                c = msg.get("content", "")
                content = c if isinstance(c, str) else ""
            if not content:
                continue
            blocks = re.findall(
                r"^\s*```(?:bash|sh|shell)\s*\n(.*?)\n\s*```",
                content,
                re.DOTALL | re.MULTILINE,
            )
            for block in reversed(blocks):
                if block.strip():
                    return block
        return ""

    async def skillstack(
        self,
        action: str,
        command: str = None,
        source: str = None,
        url: str = None,
        mode: str = "merge",
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
        Interactive management of the bash command validation knowledge base.

        Args:
            action: Operation to perform: -l (learn), -d (delete), -r (refresh), -s (stats), -i (inspect)
            command: Command name to operate on (required for all actions except -s)
            source: Learning source: 1=curated_kb, 2=man_pages, 3=online_docs
            url: Custom documentation URL (used with -r to fetch from a specific page)

        Examples:
            skillstack -l rsync 2       -- learn rsync from man pages
            skillstack -l curl 3        -- learn curl from online docs
            skillstack -r rsync         -- auto-refresh rsync from best source
            skillstack -r rsync 2       -- force refresh from man pages
            skillstack -r rsync -u https://example.com/rsync.1  -- refresh from URL
            skillstack -i rsync         -- inspect cached data for rsync
            skillstack -s               -- show cache statistics
            skillstack -d rsync         -- delete rsync from cache
        """
        trust_levels = {
            "curated_kb": 1.0,
            "man_pages": 0.9,
            "online_docs": 0.8,
            "custom_url": 0.7,
            "unknown": 0.0,
        }
        source_names = {"1": "curated_kb", "2": "man_pages", "3": "online_docs"}

        # -l: learn a command from a specific source
        if action == "-l":
            if not command:
                return "Error: command name required for -l"
            if not source:
                return "Error: source required for -l (1=curated_kb, 2=man_pages, 3=online_docs)"
            try:
                source_int = int(source)
            except (ValueError, TypeError):
                return f"Error: invalid source '{source}'. Use 1=curated_kb, 2=man_pages, 3=online_docs"
            if source_int not in (1, 2, 3):
                return f"Error: source must be 1, 2, or 3 (got {source_int})"

            cache = self._load_command_cache()

            if source_int == 1:
                result = self._check_curated_kb(command)
                if not result:
                    return f"'{command}' not found in curated KB at `{self.valves.CURATED_KB_PATH}`"
            elif source_int == 2:
                result = self._check_man_page(command, "")
                if not result:
                    return f"Man page not found for '{command}' (is man installed?)"
            else:
                result = self._fetch_online_docs(command)
                if not result:
                    return f"No online documentation found for '{command}'"

            result["trust_level"] = trust_levels.get(result.get("source", "unknown"), 0.0)
            was_updated = self._update_cache_with_authority(cache, command, result)

            if was_updated:
                self._save_command_cache(cache)
                self._recently_learned.append(command)
                msg = (
                    f"Learned '{command}' from {result['source']} "
                    f"(trust={result['trust_level']:.1f})\n"
                    f"Valid flags: {', '.join(result.get('valid_flags', [])) or 'none recorded'}\n"
                    f"Valid subcommands: {', '.join(result.get('valid_subcommands', [])) or 'none recorded'}\n"
                )
                batch_active = self._batch_mode if self._batch_mode is not None else self.valves.BATCH_LEARN_MODE
                if self.valves.AUTO_REVALIDATE_AFTER_LEARN and not batch_active:
                    code = self._extract_recent_code_from_context(__messages__)
                    if code and command in code:
                        issues = self._validate_bash(code, f"post-learn:{command}")
                        if issues:
                            msg += f"\n[AUTO-REVALIDATE] Issues found after learning '{command}':\n"
                            for issue in issues:
                                msg += f"  - {issue}\n"
                            msg += "\nFix these before proceeding."
                        else:
                            msg += f"\n[AUTO-REVALIDATE] '{command}' usage in recent code looks clean."
                    else:
                        msg += f"\n[AUTO-REVALIDATE] '{command}' not found in recent code — no re-check needed."
                elif batch_active:
                    msg += "\nBatch mode active — auto-revalidation disabled. Run `skillstack -revalidate` when done."
                return msg
            else:
                existing_trust = cache.get(command, {}).get("trust_level", 0.0)
                new_trust = result.get("trust_level", 0.0)
                return (
                    f"Not updated: existing entry has higher authority "
                    f"({existing_trust:.1f} >= {new_trust:.1f}). "
                    f"Use -d to delete first, or -r to force refresh."
                )

        # -d: delete a command from cache
        if action == "-d":
            if not command:
                return "Error: command name required for -d"
            cache = self._load_command_cache()
            if command not in cache:
                return f"'{command}' not in cache"
            old = cache.pop(command)
            self._save_command_cache(cache)
            return f"Deleted '{command}' (was from {old.get('source', 'unknown')}, trust={old.get('trust_level', 0.0):.1f})"

        # -r: refresh command knowledge, optionally from a specific source or URL
        if action == "-r":
            if not command:
                return "Error: command name required for -r"
            cache = self._load_command_cache()

            if url:
                result = self._fetch_from_url(command, url)
                if not result:
                    return f"Failed to fetch documentation from {url}"
                result["source"] = "online_docs"
                result["trust_level"] = trust_levels["custom_url"]
                result["note"] = f"Sourced from {url}"
            elif source:
                try:
                    source_int = int(source)
                except (ValueError, TypeError):
                    return f"Error: invalid source '{source}'. Use 1=curated_kb, 2=man_pages, 3=online_docs"
                if source_int == 1:
                    result = self._check_curated_kb(command)
                elif source_int == 2:
                    result = self._check_man_page(command, "")
                elif source_int == 3:
                    result = self._fetch_online_docs(command)
                else:
                    return f"Error: source must be 1, 2, or 3 (got {source_int})"
                if not result:
                    return f"No documentation found for '{command}' from source {source_int} ({source_names.get(str(source_int), '?')})"
            else:
                # Auto: walk tiers
                result = (
                    self._check_curated_kb(command)
                    or self._check_man_page(command, "")
                    or self._fetch_online_docs(command)
                )
                if not result:
                    return f"No documentation found for '{command}' from any source"

            result.setdefault("trust_level", trust_levels.get(result.get("source", "unknown"), 0.0))
            # Force-replace regardless of existing trust
            cache[command] = result
            self._save_command_cache(cache)
            return (
                f"Refreshed '{command}' from {result['source']} "
                f"(trust={result['trust_level']:.1f})\n"
                f"Valid flags: {', '.join(result.get('valid_flags', [])) or 'none recorded'}\n"
                f"Valid subcommands: {', '.join(result.get('valid_subcommands', [])) or 'none recorded'}"
            )

        # -s: show cache statistics
        if action == "-s":
            cache = self._load_command_cache()
            total = len(cache)
            if total == 0:
                return "Cache is empty. Use `skillstack -l <command> <source>` to populate it."

            by_source: dict = {}
            by_trust = {"high (>=0.9)": 0, "medium (0.7-0.9)": 0, "low (<0.7)": 0}
            for data in cache.values():
                src = data.get("source", "unknown")
                by_source[src] = by_source.get(src, 0) + 1
                trust = data.get("trust_level", 0.0)
                if trust >= 0.9:
                    by_trust["high (>=0.9)"] += 1
                elif trust >= 0.7:
                    by_trust["medium (0.7-0.9)"] += 1
                else:
                    by_trust["low (<0.7)"] += 1

            lines = [
                f"## Command Knowledge Cache\n",
                f"**Total commands:** {total}\n",
                f"**Cache path:** `{self._get_command_cache_path()}`\n",
                f"\n**By source:**",
            ]
            for src, count in sorted(by_source.items(), key=lambda x: -x[1]):
                lines.append(f"  {src}: {count}")
            lines.append("\n**By trust level:**")
            for level, count in by_trust.items():
                lines.append(f"  {level}: {count}")
            lines.append(f"\n**Commands:** {', '.join(sorted(cache.keys()))}")
            return "\n".join(lines)

        # -i: inspect a specific command
        if action == "-i":
            if not command:
                return "Error: command name required for -i"
            cache = self._load_command_cache()
            if command not in cache:
                return (
                    f"No cached data for '{command}'. "
                    f"Use `skillstack -l {command} 2` to learn from man pages."
                )
            data = cache[command]
            lines = [
                f"## {command}\n",
                f"**Source:** {data.get('source', 'unknown')}  "
                f"**Trust:** {data.get('trust_level', 0.0):.1f}  "
                f"**Cached:** {data.get('cached_at', 'unknown')}\n",
            ]
            valid_flags = data.get("valid_flags", [])
            valid_subcmds = data.get("valid_subcommands", [])
            invalid_subcmds = data.get("invalid_subcommands", [])
            if valid_flags:
                lines.append(f"**Valid flags:** {', '.join(valid_flags)}")
            else:
                lines.append("**Valid flags:** (none recorded — all flags pass validation)")
            if valid_subcmds:
                lines.append(f"**Valid subcommands:** {', '.join(valid_subcmds)}")
            if invalid_subcmds:
                lines.append(f"**Invalid subcommands:** {', '.join(invalid_subcmds)}")
            if "note" in data:
                lines.append(f"\n**Note:** {data['note']}")
            return "\n".join(lines)

        # -export: write entire cache to a temp file for sharing
        if action == "-export":
            import time
            cache = self._load_command_cache()
            if not cache:
                return "Cache is empty — nothing to export."
            export_data = {
                "exported_at": date.today().isoformat(),
                "exported_by": (__user__ or {}).get("name", "unknown"),
                "command_count": len(cache),
                "commands": cache,
            }
            export_path = f"/tmp/skillstack_export_{int(time.time())}.json"
            try:
                self._atomic_write(export_path, json.dumps(export_data, indent=2))
            except Exception as e:
                return f"Export failed: {e}"
            return (
                f"Exported {len(cache)} commands to `{export_path}`\n\n"
                f"Share this file with others. They can import it with:\n"
                f"`skillstack -import merge` (respects authority) or\n"
                f"`skillstack -import replace` (force overwrite)"
            )

        # -import: merge or replace cache from an uploaded export file
        if action == "-import":
            uploaded_files = (__user__ or {}).get("files", [])
            if not uploaded_files:
                return "Error: no file uploaded. Attach a skillstack export JSON file to the message."
            import_file = uploaded_files[0].get("path")
            if not import_file:
                return "Error: could not read uploaded file path."
            try:
                with open(import_file, "r", encoding="utf-8") as f:
                    import_data = json.load(f)
            except (OSError, json.JSONDecodeError) as e:
                return f"Error: invalid JSON file: {e}"
            if "commands" not in import_data:
                return "Error: invalid export format (missing 'commands' key)."
            if mode not in ("merge", "replace"):
                return f"Error: invalid mode '{mode}'. Use 'merge' or 'replace'."

            imported_commands = import_data["commands"]
            cache = self._load_command_cache()
            stats = {"added": 0, "upgraded": 0, "skipped": 0, "rejected": 0}

            for cmd, new_data in imported_commands.items():
                new_trust = new_data.get("trust_level", 0.0)
                if mode == "replace":
                    existed = cmd in cache
                    cache[cmd] = new_data
                    stats["upgraded" if existed else "added"] += 1
                else:  # merge
                    if cmd not in cache:
                        cache[cmd] = new_data
                        stats["added"] += 1
                    else:
                        existing_trust = cache[cmd].get("trust_level", 0.0)
                        if new_trust > existing_trust:
                            cache[cmd] = new_data
                            stats["upgraded"] += 1
                        elif new_trust == existing_trust:
                            changed = False
                            for key in ("valid_flags", "valid_subcommands", "invalid_subcommands"):
                                if key in new_data:
                                    merged = sorted(
                                        set(cache[cmd].get(key, [])) | set(new_data[key])
                                    )
                                    if merged != cache[cmd].get(key, []):
                                        cache[cmd][key] = merged
                                        changed = True
                            stats["upgraded" if changed else "skipped"] += 1
                        else:
                            stats["rejected"] += 1

            self._save_command_cache(cache)
            return (
                f"## Import Complete ({mode} mode)\n\n"
                f"**Added:** {stats['added']} new commands\n"
                f"**Upgraded:** {stats['upgraded']} (higher or equal authority with new data)\n"
                f"**Skipped:** {stats['skipped']} (equal authority, no new data)\n"
                f"**Rejected:** {stats['rejected']} (lower authority than existing)\n\n"
                f"Imported from: {import_data.get('exported_by', 'unknown')} "
                f"({import_data.get('exported_at', 'unknown')})"
            )

        # -validate: audit cache for trust/source mismatches and missing fields
        if action == "-validate":
            cache = self._load_command_cache()
            if not cache:
                return "Cache is empty — nothing to validate."
            expected_trust = {
                "curated_kb": 1.0,
                "man_pages": 0.9,
                "online_docs": 0.8,
                "custom_url": 0.7,
            }
            issues = []
            for cmd, data in cache.items():
                src = data.get("source", "unknown")
                trust = data.get("trust_level", 0.0)
                exp = expected_trust.get(src)
                if exp is not None and trust != exp:
                    issues.append(
                        f"{cmd}: trust mismatch — has {trust}, expected {exp} for source '{src}'"
                    )
                if "cached_at" not in data:
                    issues.append(f"{cmd}: missing cached_at timestamp")
                if "source" not in data:
                    issues.append(f"{cmd}: missing source field")
            if issues:
                return "## Cache Validation Issues\n\n" + "\n".join(f"- {i}" for i in issues)
            return f"Cache validated: {len(cache)} commands, no issues."

        # -batch: runtime toggle for batch learn mode (overrides valve)
        if action == "-batch":
            if command in ("on", "1", "true"):
                self._batch_mode = True
                self._recently_learned.clear()
                return (
                    "Batch mode ON — auto-revalidation disabled.\n"
                    "Learn commands with `-l`, then run `skillstack -revalidate` when done."
                )
            if command in ("off", "0", "false"):
                self._batch_mode = False
                learned = list(self._recently_learned)
                return (
                    f"Batch mode OFF — auto-revalidation re-enabled.\n"
                    f"Commands learned this batch: {', '.join(learned) if learned else 'none'}.\n"
                    f"Run `skillstack -revalidate` to check recent code against all of them."
                )
            return "Usage: skillstack -batch on  |  skillstack -batch off"

        # -revalidate: manual scan of recent context against all cached knowledge
        if action == "-revalidate":
            code = self._extract_recent_code_from_context(__messages__)
            if not code:
                return "No bash/shell code found in recent messages — nothing to validate."
            issues = self._validate_bash(code, "revalidate")
            learned = list(self._recently_learned)
            header = ""
            if learned:
                header = f"Checking against recently learned: {', '.join(learned)}\n\n"
            if issues:
                return header + "## Validation Issues\n\n" + "\n".join(f"- {i}" for i in issues)
            return header + "No validation issues found."

        return (
            f"Unknown action '{action}'. Valid actions:\n"
            f"  -l (learn)      -d (delete)   -r (refresh)    -s (stats)\n"
            f"  -i (inspect)    -export       -import         -validate\n"
            f"  -batch on/off   -revalidate"
        )
