"""Feedback files as they really arrive, turned into one plain record.

Two shapes are read:
- a GitHub issue as `gh issue view N --json number,title,body,author,authorAssociation,labels,createdAt,url,comments`
  prints it;
- a reply on X as the X API v2 returns a post (`data` + `includes.users`).

Nothing here is trusted except the shape: names, titles and bodies are all the sender's words.
"""

import json
from pathlib import Path


def load(path: Path) -> dict:
    doc = json.loads(path.read_text(encoding="utf-8"))
    if "data" in doc and isinstance(doc["data"], dict) and "text" in doc["data"]:
        return _x_post(doc)
    if "number" in doc and "body" in doc:
        return _github_issue(doc)
    raise ValueError(f"{path.name}: neither a GitHub issue nor an X post")


def _github_issue(doc: dict) -> dict:
    parts = [f"Issue title: {doc.get('title') or ''}", doc.get("body") or ""]
    for c in doc.get("comments") or []:
        who = (c.get("author") or {}).get("login", "?")
        parts.append(f"Comment by @{who}:\n{c.get('body') or ''}")
    return {
        "channel": "github-issue",
        "ref": f"#{doc['number']}",
        "url": doc.get("url"),
        "author": (doc.get("author") or {}).get("login"),
        "created_at": doc.get("createdAt"),
        "text": "\n\n".join(parts),
    }


def _x_post(doc: dict) -> dict:
    post = doc["data"]
    users = {u["id"]: u for u in (doc.get("includes") or {}).get("users", [])}
    author = users.get(post.get("author_id"), {})
    return {
        "channel": "x-reply",
        "ref": post["id"],
        "url": f"https://x.com/{author.get('username', 'i')}/status/{post['id']}",
        "author": author.get("username"),
        "created_at": post.get("created_at"),
        "text": post["text"],
    }
