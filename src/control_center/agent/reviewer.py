"""AI-assisted review suggestion using Claude Agent SDK."""

import asyncio
import json
import logging
import subprocess
from collections.abc import Callable
from datetime import datetime, timezone

from claude_agent_sdk import AssistantMessage, ClaudeAgentOptions, ResultMessage, TextBlock, query

from control_center.config import Settings
from control_center.models import PRStatus, ReviewComment, ReviewRequest, ReviewSuggestion

logger = logging.getLogger(__name__)

MAX_DIFF_CHARS = 80_000


def _get_pr_diff(repo: str, number: int) -> str:
    result = subprocess.run(
        ["gh", "pr", "diff", str(number), "--repo", repo],
        capture_output=True,
        text=True,
        timeout=30,
    )
    if result.returncode != 0:
        raise RuntimeError(f"Failed to get diff: {result.stderr}")
    return result.stdout[:MAX_DIFF_CHARS]


def _get_pr_info(repo: str, number: int) -> dict:
    result = subprocess.run(
        [
            "gh",
            "pr",
            "view",
            str(number),
            "--repo",
            repo,
            "--json",
            "title,body,author,baseRefName,headRefName,files,comments,reviews",
        ],
        capture_output=True,
        text=True,
        timeout=30,
    )
    if result.returncode != 0:
        raise RuntimeError(f"Failed to get PR info: {result.stderr}")
    return json.loads(result.stdout)


def _get_review_comments(repo: str, number: int) -> list[dict]:
    """Fetch inline review comments (file-level comments from other reviewers)."""
    result = subprocess.run(
        [
            "gh",
            "api",
            f"repos/{repo}/pulls/{number}/comments",
            "--jq",
            ".[] | {path, line, body, user: .user.login, diff_hunk: .diff_hunk}",
        ],
        capture_output=True,
        text=True,
        timeout=30,
    )
    if result.returncode != 0:
        return []
    comments = []
    for line in result.stdout.strip().split("\n"):
        if not line.strip():
            continue
        try:
            comments.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return comments


def _build_review_prompt(pr_info: dict, diff: str, inline_comments: list[dict]) -> str:
    title = pr_info.get("title", "")
    body = pr_info.get("body", "") or ""
    author = pr_info.get("author", {}).get("login", "unknown")
    base = pr_info.get("baseRefName", "main")
    head = pr_info.get("headRefName", "")

    # Files changed
    files_list = ""
    for f in pr_info.get("files", []):
        path = f.get("path", "")
        adds = f.get("additions", 0)
        dels = f.get("deletions", 0)
        files_list += f"\n  - {path} (+{adds} -{dels})"

    # Existing review-level comments (body text from reviews)
    review_bodies = ""
    for review in pr_info.get("reviews", []):
        reviewer = review.get("author", {}).get("login", "?")
        state = review.get("state", "")
        review_body = review.get("body", "")
        if review_body:
            review_bodies += f"\n### {reviewer} ({state})\n{review_body[:1500]}\n"

    # Inline review comments from other reviewers (greptile, humans, etc.)
    inline_section = ""
    if inline_comments:
        inline_section = "\n## Inline Review Comments from Other Reviewers\n"
        inline_section += "These are comments already left by other reviewers. "
        inline_section += "Build on their feedback — agree, disagree, or add depth.\n"
        for c in inline_comments[:30]:  # cap at 30
            user = c.get("user", "?")
            path = c.get("path", "?")
            line_no = c.get("line", "?")
            comment_body = c.get("body", "")[:800]
            hunk = c.get("diff_hunk", "")[-300:]  # last part of hunk for context
            inline_section += f"\n**{user}** on `{path}:{line_no}`:\n"
            if hunk:
                inline_section += f"```diff\n{hunk}\n```\n"
            inline_section += f"{comment_body}\n"

    return f"""You're an experienced Senior Lead Software Engineer. Stop being agreeable \
and act as my brutally honest, high-level advisor.
Challenge my thinking. Question my assumptions. Expose blind spots.
Don't flatter me. Don't soften anything.
If my reasoning is weak, dissect it.
If I'm avoiding something uncomfortable, call it out.
Show me where I'm making excuses or underestimating risk.
Then give me a precise, prioritized plan to reach the next level.
Hold nothing back.
Analyze this PR diff and provide a structured review based on the current state of the review.


## PR Details
- **Title**: {title}
- **Author**: {author}
- **Branch**: {head} -> {base}

## PR Description
{body[:4000]}

## Files Changed{files_list}

{f"## Existing Review Comments{review_bodies}" if review_bodies else ""}
{inline_section}

## Diff
```diff
{diff}
```

## Instructions
Provide your review as valid JSON with this exact structure:
```json
{{
  "summary": "1-3 sentence overview of what this PR does and your overall assessment",
  "verdict": "approve|request_changes|comment",
  "comments": [
    {{
      "file": "full/path/to/file.ext",
      "line": 42,
      "severity": "critical|warning|info|nit",
      "body": "Clear explanation of the issue or observation",
      "snippet": "the relevant code lines from the diff",
      "suggestion": "proposed fix or improvement (empty string if just an observation)"
    }}
  ]
}}
```

Guidelines:
- **file**: Always use the full path as shown in the diff headers (e.g. `src/control_center/web/routes.py`)
- **line**: The line number in the new file where the comment applies. Use null if it's a general file comment
- **severity**: `critical` = bugs/security, `warning` = logic, `info` = suggestions, `nit` = style
- **snippet**: Copy the exact relevant code from the diff (a few lines, not the whole file)
- **suggestion**: If you have a concrete fix, show the corrected code. Leave empty for observations
- **body**: Be specific and actionable. Reference the code directly
- If other reviewers already flagged something, acknowledge it and add your perspective rather than repeating
- Focus on: bugs, security, logic errors, edge cases, performance, API design
- Skip trivial style nits unless they affect readability significantly
- Return ONLY the JSON object, no markdown fences, no extra text"""


def _parse_review_response(text: str) -> ReviewSuggestion:
    # Try to extract JSON from the response
    cleaned = text.strip()

    # Strip markdown fences if present
    if cleaned.startswith("```"):
        first_newline = cleaned.index("\n")
        cleaned = cleaned[first_newline + 1 :]
    if cleaned.endswith("```"):
        cleaned = cleaned[: cleaned.rindex("```")]
    cleaned = cleaned.strip()

    # Find the JSON object boundaries
    start = cleaned.find("{")
    end = cleaned.rfind("}") + 1
    if start == -1 or end == 0:
        # Fallback: return raw text as a single comment
        return ReviewSuggestion(
            summary=cleaned[:200],
            verdict="comment",
            comments=[ReviewComment(body=cleaned)],
            generated_at=datetime.now(timezone.utc),
        )

    try:
        data = json.loads(cleaned[start:end])
    except json.JSONDecodeError:
        return ReviewSuggestion(
            summary=cleaned[:200],
            verdict="comment",
            comments=[ReviewComment(body=cleaned)],
            generated_at=datetime.now(timezone.utc),
        )

    verdict_raw = data.get("verdict", "comment").lower()
    if "approve" in verdict_raw and "request" not in verdict_raw:
        verdict = "approve"
    elif "request" in verdict_raw or "change" in verdict_raw:
        verdict = "request_changes"
    else:
        verdict = "comment"

    _severity_order = {"critical": 0, "warning": 1, "info": 2, "nit": 3}
    comments = []
    for c in data.get("comments", []):
        comments.append(
            ReviewComment(
                file=c.get("file", ""),
                line=c.get("line"),
                body=c.get("body", ""),
                snippet=c.get("snippet", ""),
                suggestion=c.get("suggestion", ""),
                severity=c.get("severity", "info"),
            )
        )
    comments.sort(key=lambda c: _severity_order.get(c.severity, 9))

    return ReviewSuggestion(
        summary=data.get("summary", ""),
        verdict=verdict,
        comments=comments,
        generated_at=datetime.now(timezone.utc),
    )


async def generate_review_suggestion(
    rr: ReviewRequest | PRStatus,
    settings: Settings,
    log_fn: Callable[[str], None] | None = None,
) -> ReviewSuggestion:
    """Fetch PR diff and generate a review suggestion using Claude."""
    pr_key = f"{rr.repo}#{rr.number}"

    def _log(msg: str) -> None:
        logger.info("[review %s] %s", pr_key, msg)
        if log_fn:
            log_fn(msg)

    _log(f"Starting review suggestion for {pr_key}")

    # Fetch diff, PR info, and inline comments in parallel
    _log("Fetching PR diff...")
    diff_task = asyncio.to_thread(_get_pr_diff, rr.repo, rr.number)
    info_task = asyncio.to_thread(_get_pr_info, rr.repo, rr.number)
    comments_task = asyncio.to_thread(_get_review_comments, rr.repo, rr.number)
    diff, pr_info, inline_comments = await asyncio.gather(
        diff_task,
        info_task,
        comments_task,
    )
    n_files = len(pr_info.get("files", []))
    files_summary = ", ".join(f.get("path", "?").split("/")[-1] for f in pr_info.get("files", [])[:6])
    if n_files > 6:
        files_summary += f" +{n_files - 6} more"
    _log(f"Diff: {len(diff):,} chars, {n_files} files ({files_summary})")

    n_inline = len(inline_comments)
    if n_inline:
        reviewers = sorted({c.get("user", "?") for c in inline_comments})
        _log(f"{n_inline} inline comments from: {', '.join(reviewers)}")
    else:
        _log("No inline comments from other reviewers")

    n_reviews = len(pr_info.get("reviews", []))
    if n_reviews:
        review_states = {}
        for r in pr_info.get("reviews", []):
            state = r.get("state", "?")
            review_states[state] = review_states.get(state, 0) + 1
        states_str = ", ".join(f"{v} {k.lower()}" for k, v in review_states.items())
        _log(f"{n_reviews} reviews ({states_str})")

    prompt = _build_review_prompt(pr_info, diff, inline_comments)
    prompt_tokens = len(prompt) // 4  # rough estimate
    _log(f"Sending to Claude ({settings.autofix_model}) ~{prompt_tokens:,} tokens...")

    result_text = ""
    chunk_count = 0
    last_log_len = 0
    async with asyncio.timeout(120):
        async for message in query(
            prompt=prompt,
            options=ClaudeAgentOptions(
                allowed_tools=[],
                permission_mode="bypassPermissions",
                max_turns=1,
                max_budget_usd=0.5,
                model=settings.autofix_model,
            ),
        ):
            if isinstance(message, AssistantMessage):
                for block in message.content:
                    if isinstance(block, TextBlock) and block.text.strip():
                        result_text += block.text
                        chunk_count += 1
                        # Log progress every ~2k chars
                        if len(result_text) - last_log_len >= 2000:
                            _log(f"Receiving response... {len(result_text):,} chars")
                            last_log_len = len(result_text)
            elif isinstance(message, ResultMessage):
                cost = getattr(message, "total_cost_usd", 0.0) or 0.0
                _log(f"Response complete: {len(result_text):,} chars{f', cost ${cost:.3f}' if cost else ''}")
                if getattr(message, "is_error", False):
                    err = getattr(message, "result", "")
                    _log(f"Agent error: {err}")
                    raise RuntimeError(f"Agent error: {err}")

    if not result_text:
        _log("No response from review agent")
        raise RuntimeError("No response from review agent")

    _log("Parsing review response...")
    suggestion = _parse_review_response(result_text)

    severities = {}
    for c in suggestion.comments:
        severities[c.severity] = severities.get(c.severity, 0) + 1
    sev_str = ", ".join(f"{v} {k}" for k, v in sorted(severities.items())) if severities else "none"
    _log(f"Review ready: {suggestion.verdict} — {len(suggestion.comments)} comments ({sev_str})")
    return suggestion
