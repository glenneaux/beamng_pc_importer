from collections import defaultdict


def jbeam_override_export_plan_lines(plan):
    lines = [
        "[BeamNG JBeam Override Export Plan]",
        f"Generated: {plan['generated_at']}",
        f"Operations: {plan.get('operation_count', plan['node_update_count'])}",
        f"New files: {plan.get('file_create_count', 0)}",
        f"Node edits: {plan['node_update_count']}",
        f"Topology updates: {plan.get('topology_update_count', 0)}",
        f"Source files: {plan['source_file_count']}",
        f"Stageable files: {plan['stageable_file_count']}",
        f"User current folder: {plan['user_current_folder'] or '(not configured)'}",
        f"JBeam export mod: {plan.get('export_mod_folder', '') or '(not configured)'}",
        f"JBeam export root: {plan.get('export_root', '') or '(not configured)'}",
        "Cache only: yes",
        "",
    ]
    for warning in plan["warnings"]:
        lines.append(f"Warning: {warning}")
    lines.append("")

    for file_group in plan["files"]:
        lines.append(f"Source: {file_group['source_file']}")
        lines.append(f"Virtual: {file_group['virtual_path'] or '(unknown)'}")
        lines.append(f"Planned target: {file_group['planned_target_path'] or '(not stageable)'}")
        lines.append(f"Can stage override: {'yes' if file_group['can_stage_override'] else 'no'}")
        if file_group.get("is_new_file"):
            lines.append("File status: new staged JBeam file")
        lines.append(f"Operations: {file_group.get('operation_count', file_group['node_update_count'])}")
        lines.append(f"Node edits: {file_group['node_update_count']}")
        lines.append(f"Topology updates: {file_group.get('topology_update_count', 0)}")
        for warning in file_group["warnings"]:
            lines.append(f"File warning: {warning}")
        for part_group in file_group["parts"]:
            lines.append(
                f"  Part: {part_group['part']} "
                f"({part_group.get('operation_count', part_group['node_update_count'])} operation(s))"
            )
            for update in part_group.get("node_inserts", []):
                lines.append(f"    insert node {update.get('node', '')}: {update.get('new_position', '')}")
            for update in part_group["node_updates"]:
                lines.append(
                    "    "
                    f"{update.get('node', '')}: "
                    f"{update.get('old_position', '')} -> {update.get('new_position', '')}"
                )
            for update in part_group.get("node_deletes", []):
                lines.append(f"    delete node {update.get('node', '')}: {update.get('old_position', '')}")
            for update in part_group.get("beam_inserts", []):
                lines.append(f"    insert beam: {update.get('nodes', '')}")
            for update in part_group.get("beam_deletes", []):
                lines.append(f"    delete beam: {update.get('nodes', '')}")
            for update in part_group.get("triangle_inserts", []):
                lines.append(f"    insert triangle: {update.get('nodes', '')}")
            for update in part_group.get("triangle_deletes", []):
                lines.append(f"    delete triangle: {update.get('nodes', '')}")
        lines.append("")
    if not plan["files"]:
        lines.append("No accepted JBeam edits are recorded.")
    return lines


def filter_plan_files_for_selected_virtual_paths(plan, selected_virtual_paths, normalize_virtual_path):
    if selected_virtual_paths is None:
        return plan
    selected = {normalize_virtual_path(path) for path in selected_virtual_paths if path}
    filtered = dict(plan)
    filtered_files = [
        file_group
        for file_group in plan.get("files", [])
        if normalize_virtual_path(file_group.get("virtual_path", "")) in selected
    ]
    filtered["files"] = filtered_files
    filtered["source_file_count"] = len(filtered_files)
    filtered["stageable_file_count"] = sum(1 for item in filtered_files if item.get("can_stage_override"))
    filtered["operation_count"] = sum(
        int(item.get("operation_count", item.get("node_update_count", 0))) for item in filtered_files
    )
    filtered["file_create_count"] = sum(int(item.get("file_create_count", 0)) for item in filtered_files)
    filtered["node_update_count"] = sum(int(item.get("node_update_count", 0)) for item in filtered_files)
    filtered["topology_update_count"] = sum(int(item.get("topology_update_count", 0)) for item in filtered_files)
    if selected and not filtered_files:
        warnings = set(filtered.get("warnings", []))
        warnings.add("Selected export files did not match any accepted JBeam edit files.")
        filtered["warnings"] = sorted(warnings)
    return filtered


def jbeam_export_preflight_counts(plan):
    counts = defaultdict(int)
    for file_group in plan.get("files", []):
        counts["file_creates"] += int(file_group.get("file_create_count", 0))
        for part_group in file_group.get("parts", []):
            counts["node_inserts"] += len(part_group.get("node_inserts", []))
            counts["node_updates"] += len(part_group.get("node_updates", []))
            counts["node_deletes"] += len(part_group.get("node_deletes", []))
            counts["beam_inserts"] += len(part_group.get("beam_inserts", []))
            counts["beam_deletes"] += len(part_group.get("beam_deletes", []))
            counts["beam_param_updates"] += len(part_group.get("beam_param_updates", []))
            counts["triangle_inserts"] += len(part_group.get("triangle_inserts", []))
            counts["triangle_deletes"] += len(part_group.get("triangle_deletes", []))
            counts["triangle_param_updates"] += len(part_group.get("triangle_param_updates", []))
    return counts
