"""Skill files: short playbooks (markdown in pashtoon_site/skills/) that are added to the assistant's instructions.
Edit or add a file there to teach the assistant something; nothing else needs to change. The same text works for any
AI provider."""
import os
import re

DIR = os.path.join(os.path.dirname(__file__), "skills")


def load(role: str) -> str:
    out = []
    for fn in sorted(os.listdir(DIR)) if os.path.isdir(DIR) else []:
        if not fn.endswith(".md"):
            continue
        text = open(os.path.join(DIR, fn), encoding="utf-8").read()
        m = re.match(r"---\n(.*?)\n---\n(.*)", text, re.S)
        meta, body = (m.group(1), m.group(2)) if m else ("", text)
        roles = re.search(r"^for:\s*(.+)$", meta, re.M)
        if roles and role not in [r.strip() for r in roles.group(1).split(",")] and "all" not in roles.group(1):
            continue
        name = re.search(r"^name:\s*(.+)$", meta, re.M)
        out.append(f"## Skill: {name.group(1).strip() if name else fn[:-3]}\n{body.strip()}")
    return "\n\n".join(out)
