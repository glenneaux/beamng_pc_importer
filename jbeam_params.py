def param_summary_lines(params, empty_text="none"):
    if not params:
        return [empty_text]
    return [f"{key}: {params[key]}" for key in sorted(params)]


def param_diff_lines(params, committed_params):
    params = params if isinstance(params, dict) else {}
    committed_params = committed_params if isinstance(committed_params, dict) else {}
    lines = []
    for key in sorted(set(params) | set(committed_params)):
        old_value = committed_params.get(key, "<unset>")
        new_value = params.get(key, "<unset>")
        if old_value != new_value:
            lines.append(f"{key}: {old_value} -> {new_value}")
    return lines


def param_state_label(params, committed_params):
    diffs = param_diff_lines(params, committed_params)
    if diffs:
        return f"{len(diffs)} staged param change(s)"
    if params:
        return "params inherited/stored, no staged change"
    return "no params found"
