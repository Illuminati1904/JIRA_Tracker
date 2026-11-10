"""Atlassian Document Format (Jira Cloud REST v3 rich text) -> plain text with Jira-wiki-ish line structure.

The downstream differ is sentence/line based, so the goal is stable, readable lines, not fidelity.
"""
from __future__ import annotations


def _marks_text(node: dict) -> str:
    return node.get("text", "")


def to_text(node) -> str:
    if node is None:
        return ""
    if isinstance(node, str):
        return node
    if isinstance(node, list):
        return "\n".join(t for t in (to_text(n) for n in node) if t)
    if not isinstance(node, dict):
        return str(node)
    t = node.get("type")
    c = node.get("content") or []
    if t == "text":
        return _marks_text(node)
    if t == "hardBreak":
        return "\n"
    if t == "mention":
        return "[~accountid:%s]" % (node.get("attrs", {}).get("id", "unknown"))
    if t in ("inlineCard", "blockCard", "embedCard"):
        return node.get("attrs", {}).get("url", "")
    if t == "emoji":
        return node.get("attrs", {}).get("shortName", "")
    if t == "status":
        return "[%s]" % node.get("attrs", {}).get("text", "")
    if t == "date":
        return str(node.get("attrs", {}).get("timestamp", ""))
    if t in ("paragraph", "heading"):
        return "".join(to_text(x) for x in c)
    if t == "codeBlock":
        return "".join(to_text(x) for x in c)
    if t in ("bulletList", "orderedList"):
        lines = []
        for i, li in enumerate(c, 1):
            body = to_text(li).replace("\n", " ").strip()
            lines.append(("%d. %s" % (i, body)) if t == "orderedList" else ("* " + body))
        return "\n".join(lines)
    if t == "listItem":
        return "\n".join(to_text(x) for x in c)
    if t == "tableRow":
        return " | ".join(to_text(x).replace("\n", " ").strip() for x in c)
    if t in ("tableCell", "tableHeader"):
        return "\n".join(to_text(x) for x in c)
    # doc, table, panel, blockquote, expand, mediaSingle, ... -> recurse with line breaks
    return "\n".join(x for x in (to_text(n) for n in c) if x)
