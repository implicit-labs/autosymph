#!/usr/bin/env python3
"""Collect permission denials from autosymph agent logs.

Scans all .ndjson log files, extracts permission_denials from result events,
and writes a summary to ~/.autosymph/permission-denials.md.

Run manually or via cron:
    python3 scripts/collect-permission-denials.py

Output format (permission-denials.md):
    ## Permission Denials
    | Tool | Command | Issue | Run | Date |
    Each unique command pattern appears once with count.
"""

import json
from collections import Counter
from datetime import datetime
from pathlib import Path

LOG_ROOT = Path.home() / ".autosymph" / "logs"
OUTPUT = LOG_ROOT / "permission-denials.md"

# Commands that should NEVER be whitelisted
DANGEROUS_PATTERNS = [
    "rm -rf /",
    "rm -rf ~",
    "rm -rf .",
    "rm -rf *",
    "sudo ",
    "chmod 777",
    "curl | bash",
    "curl | sh",
    "wget | bash",
    "> /dev/sd",
    "mkfs",
    "dd if=",
    ":(){ :|:& };:",
]


def is_dangerous(command: str) -> bool:
    """Check if a command matches known dangerous patterns."""
    cmd_lower = command.lower()
    return any(p in cmd_lower for p in DANGEROUS_PATTERNS)


def extract_denials() -> list[dict]:
    """Extract all permission denials from ndjson logs."""
    denials = []

    for ndjson_path in sorted(LOG_ROOT.rglob("*.ndjson")):
        issue_dir = ndjson_path.parent.name  # e.g. "issue-123"
        run_name = ndjson_path.stem  # e.g. "verify-run5"

        with open(ndjson_path) as f:
            for line in f:
                try:
                    d = json.loads(line)
                except json.JSONDecodeError:
                    continue

                if d.get("type") != "result":
                    continue

                for denial in d.get("permission_denials", []):
                    tool_name = denial.get("tool_name", "unknown")
                    tool_input = denial.get("tool_input", {})
                    command = ""
                    if tool_name == "Bash":
                        command = tool_input.get("command", "")
                    elif tool_name in ("Read", "Write", "Edit"):
                        command = tool_input.get("file_path", "")
                    else:
                        command = json.dumps(tool_input)[:200]

                    denials.append({
                        "tool": tool_name,
                        "command": command[:200],
                        "issue": issue_dir,
                        "run": run_name,
                        "dangerous": is_dangerous(command),
                    })

    return denials


def write_report(denials: list[dict]) -> None:
    """Write the permission denials report."""
    if not denials:
        OUTPUT.write_text("# Permission Denials\n\nNo denials found.\n")
        print("No denials found.")
        return

    # Count unique command patterns
    counter = Counter()
    for d in denials:
        # Normalize: strip paths, keep just the command prefix
        cmd = d["command"]
        if d["tool"] == "Bash":
            # Keep first 80 chars as the pattern
            pattern = cmd[:80]
        else:
            pattern = f"{d['tool']}: {cmd[:60]}"
        counter[(d["tool"], pattern, d["dangerous"])] += 1

    lines = [
        "# Permission Denials",
        "",
        f"**Last updated:** {datetime.now().strftime('%Y-%m-%d %H:%M')}",
        f"**Total denials:** {len(denials)}",
        f"**Unique patterns:** {len(counter)}",
        "",
        "## Safe to Whitelist",
        "",
        "These commands were denied but appear safe. Review and add to `allowed_tools` in workflow config.",
        "",
        "| Count | Tool | Command Pattern |",
        "|-------|------|----------------|",
    ]

    dangerous_lines = [
        "",
        "## Dangerous — Do NOT Whitelist",
        "",
        "| Count | Tool | Command Pattern |",
        "|-------|------|----------------|",
    ]

    safe_count = 0
    danger_count = 0

    for (tool, pattern, dangerous), count in counter.most_common():
        escaped = pattern.replace("|", "\\|")
        row = f"| {count} | {tool} | `{escaped}` |"
        if dangerous:
            dangerous_lines.append(row)
            danger_count += 1
        else:
            lines.append(row)
            safe_count += 1

    if danger_count > 0:
        lines.extend(dangerous_lines)
    else:
        lines.extend(["", "## Dangerous — Do NOT Whitelist", "", "None found."])

    # Recent denials (last 20)
    lines.extend([
        "",
        "## Recent Denials",
        "",
        "| Issue | Run | Tool | Command |",
        "|-------|-----|------|---------|",
    ])
    for d in denials[-20:]:
        escaped = d["command"][:100].replace("|", "\\|")
        flag = " ⚠️" if d["dangerous"] else ""
        lines.append(f"| {d['issue']} | {d['run']} | {d['tool']} | `{escaped}`{flag} |")

    OUTPUT.write_text("\n".join(lines) + "\n")
    print(f"Wrote {OUTPUT}")
    print(f"  {safe_count} safe patterns, {danger_count} dangerous patterns")
    print(f"  {len(denials)} total denials")


if __name__ == "__main__":
    denials = extract_denials()
    write_report(denials)
