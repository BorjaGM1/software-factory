"""Event -> task, deterministically. Returns either a task or an early outcome (manual / blocked)."""

import json

from .core import matches, sh


def _trusted_text(event: dict) -> str:
    return event.get("title", "") + "\n" + json.dumps(event.get("evidence") or {}, ensure_ascii=False)


def triage(event: dict, target: dict) -> tuple[dict | None, dict | None]:
    """(task, None) to build, or (None, outcome) to stop here. Never reads `untrusted`."""
    rules = target["triage"]
    kind, evidence = event.get("kind", ""), event.get("evidence") or {}

    if kind in rules["manual_kinds"]:
        return None, {"state": "manual", "reason": "maintainer-kind",
                      "detail": f"'{kind}' is a maintainer decision; the factory doesn't build it"}

    for need in evidence.get("needs", []):
        probe = rules["capabilities"].get(need)
        if probe is None or sh(probe, timeout=30)[0] != 0:
            return None, {"state": "blocked", "reason": f"needs-{need}", "need": need,
                          "detail": f"needs {need}, which this machine can't provide"
                                    + ("" if probe else " (not a known capability)")}

    implicated = [p for p in evidence.get("paths", []) if matches(p, target["forbidden_paths"])]
    if implicated:
        return None, {"state": "manual", "reason": "protected-path", "paths": implicated,
                      "detail": f"the evidence points at protected paths: {', '.join(implicated)}"}

    text = _trusted_text(event)
    for intent in target.get("intent", []):
        if intent["marker"] in text:
            return None, {"state": "manual", "reason": "maintainer-intent", "intent": intent["id"],
                          "detail": f"touches a maintainer decision: {intent['why']}"}

    prebuilt = event.get("task") or evidence.get("task")  # a task the feedback quarantine already wrote
    if prebuilt:
        task = dict(prebuilt)
    else:
        type_ = rules["kinds"].get(kind)
        if not type_:
            return None, {"state": "manual", "reason": "unknown-kind",
                          "detail": f"no route for kind '{kind}'"}
        task = {"type": type_, "summary": _summary(event, type_),
                "acceptance": evidence.get("acceptance") or _acceptance(type_)}
    task = {"id": "task-" + event["id"].removeprefix("evt-"), "event_id": event["id"], **task}
    # The target's limits always win over whatever the event or the quarantine asked for.
    task["allowed_paths"] = [p for p in task.get("allowed_paths") or target["allowed_paths"]
                             if not matches(p, target["forbidden_paths"])]
    task["forbidden_paths"] = sorted(set(task.get("forbidden_paths", [])) | set(target["forbidden_paths"]))
    return task, None


def _summary(event: dict, type_: str) -> str:
    verb = {"fix": "Fix", "add": "Add", "investigate": "Investigate"}[type_]
    facts = {k: v for k, v in (event.get("evidence") or {}).items() if k not in ("needs", "acceptance", "task")}
    lines = [f"{verb}: {event['title']}"]
    if facts:
        lines.append("Evidence: " + json.dumps(facts, ensure_ascii=False))
    return "\n".join(lines)


def _acceptance(type_: str) -> list[str]:
    if type_ == "investigate":
        return ["a short written finding, with what was run to reach it"]
    return ["the behaviour described in the summary is fixed" if type_ == "fix" else "the feature works as described",
            "a new test covers it", "the full offline test suite passes"]
