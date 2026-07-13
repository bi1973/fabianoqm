"""Safe diagnostics for failed OAuth token exchanges.

This module intentionally overrides the helper from ``_bridge_oauth_fix.py`` so
provider errors are reduced to an allow-listed, redacted summary before they are
written to Render logs.
"""


def _sanitized_oauth_provider_error(response: requests.Response) -> dict[str, Any]:
    """Extract only safe, useful fields from a failed Mendeley token response."""

    result: dict[str, Any] = {
        "provider_content_type": str(response.headers.get("Content-Type", ""))[:120],
    }
    payload: Any = None
    try:
        payload = response.json()
    except ValueError:
        payload = None

    if isinstance(payload, dict):
        for key in ("error", "error_description", "message", "detail", "status"):
            value = payload.get(key)
            if value is not None and not isinstance(value, (dict, list)):
                safe_value = _sanitize_oauth_text(value)
                if safe_value:
                    result[f"provider_{key}"] = safe_value
    elif response.text:
        safe_excerpt = _sanitize_oauth_text(response.text)
        if safe_excerpt:
            result["provider_message"] = safe_excerpt

    if len(result) == 1:
        result["provider_message"] = "Resposta sem mensagem diagnóstica segura."
    return result
