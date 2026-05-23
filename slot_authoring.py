from datetime import datetime
import json


def slot_option_count(options_json):
    try:
        return len(json.loads(options_json or "[]"))
    except (TypeError, ValueError):
        return 0


def slot_row_report_line(row):
    indent = "  " * min(int(row.get("depth", 0) or 0), 8)
    return (
        f"- {indent}{row.get('slot_name') or '(unnamed slot)'} | "
        f"parent={row.get('parent_part') or '(none)'} | "
        f"selected={row.get('selected_part') or '(empty)'} | "
        f"options={int(row.get('option_count', 0) or 0)} | "
        f"{'core' if row.get('is_core') else 'optional'}"
    )


def slot_authoring_report_lines(
    *,
    root_part,
    vehicle_model,
    dirty,
    slot_rows,
    active_part_metadata=None,
):
    issues = validate_slot_authoring(slot_rows, active_part_metadata)
    lines = [
        "[BeamNG Slot Authoring Report]",
        f"Generated: {datetime.now().isoformat(timespec='seconds')}",
        f"Root part: {root_part or '(none)'}",
        f"Vehicle/model: {vehicle_model or '(unknown)'}",
        f"Rows loaded: {len(slot_rows)}",
        f"Dirty: {bool(dirty)}",
        f"Issues: {len(issues)}",
        "",
        "Configuration slots:",
    ]
    if slot_rows:
        lines.extend(slot_row_report_line(row) for row in slot_rows)
    else:
        lines.append("- none loaded")

    lines.extend(["", "Active part slot metadata:"])
    active_part_metadata = active_part_metadata or {}
    if active_part_metadata:
        lines.append(f"- Part: {active_part_metadata.get('part_name') or '(unknown)'}")
        lines.append(f"- slotType: {active_part_metadata.get('slot_type') or '(unset)'}")
        child_slots = active_part_metadata.get("child_slots") or []
        if child_slots:
            lines.append("- Child slots:")
            for row in child_slots:
                lines.append(f"  - {row}")
        else:
            lines.append("- Child slots: none")
    else:
        lines.append("- No active topology part slot metadata found.")

    lines.extend(["", "Validation:"])
    if issues:
        lines.extend(f"- {issue}" for issue in issues)
    else:
        lines.append("- no slot authoring issues found")

    lines.extend(
        [
            "",
            "Remaining slot authoring work:",
            "- Dedicated editor for slotType defaults, descriptions, availability, and safe rename/migration.",
            "- Validation that new parts are reachable from at least one slot tree path.",
            "- Slot authoring UX that can edit existing part slot tables, not only staged new-part metadata.",
        ]
    )
    return lines


def validate_slot_authoring(slot_rows, active_part_metadata=None):
    issues = []
    seen_paths = set()
    for row in slot_rows:
        slot_name = row.get("slot_name") or ""
        path = row.get("path") or slot_name
        if path in seen_paths:
            issues.append(f"Duplicate slot row path/name in configuration tree: {path}")
        seen_paths.add(path)
        if row.get("is_core") and not row.get("selected_part"):
            issues.append(f"Core slot has no selected part: {slot_name or '(unnamed slot)'}")

    active_part_metadata = active_part_metadata or {}
    if active_part_metadata:
        if not active_part_metadata.get("slot_type"):
            issues.append(f"Active part has no slotType: {active_part_metadata.get('part_name') or '(unknown part)'}")
        child_types = []
        for row in active_part_metadata.get("child_slots") or []:
            child_type = str(row[0] if row else "")
            if not child_type:
                issues.append("Active part has a child slot row with an empty type")
                continue
            if child_type in child_types:
                issues.append(f"Active part has duplicate child slot type: {child_type}")
            child_types.append(child_type)
    return issues
