"""OAuth callback relay for ChatGPT Actions.

ChatGPT requires the authorization URL, token URL, and API hostname to share a
root domain. Mendeley, however, requires the redirect URI used during token
exchange to match the redirect URI used during authorization. This module keeps
ChatGPT talking only to the Render domain while safely relaying the Mendeley
callback back to ChatGPT.

Some ChatGPT clients still send the legacy ``chat.openai.com`` callback. That
host can redirect to the new ``chatgpt.com`` frontend without preserving the
OAuth callback route, producing ``/undefined``. The relay therefore validates
both official hosts but returns the browser to a configurable canonical host,
which defaults to ``chatgpt.com``.

OAuth provider failures are logged only as a small allow-listed and redacted
summary. Tokens, secrets, authorization codes, state values and raw response
bodies are never written to the audit log.
"""

import html as html_lib


APP_VERSION = "1.0.3"
OAUTH_PUBLIC_BASE_URL = os.getenv(
    "PUBLIC_BASE_URL", "https://mendeley-controlled-writer.onrender.com"
).rstrip("/")
OAUTH_CALLBACK_URL = f"{OAUTH_PUBLIC_BASE_URL}/oauth/callback"
OAUTH_RELAY_TTL_SECONDS = int(os.getenv("OAUTH_RELAY_TTL_SECONDS", "600"))
CHATGPT_CALLBACK_HOST = os.getenv("CHATGPT_CALLBACK_HOST", "chatgpt.com").strip().lower()
if CHATGPT_CALLBACK_HOST not in {"chat.openai.com", "chatgpt.com"}:
    CHATGPT_CALLBACK_HOST = "chatgpt.com"

oauth_relay_serializer = URLSafeTimedSerializer(
    SIGNING_SECRET, salt="mendeley-oauth-callback-relay-v1"
)


def _validated_chatgpt_callback(value: str) -> str:
    parsed = urlparse(value)
    allowed_hosts = {"chat.openai.com", "chatgpt.com"}
    valid_path = re.fullmatch(r"/aip/g-[A-Za-z0-9_-]+/oauth/callback", parsed.path or "")
    if (
        parsed.scheme != "https"
        or (parsed.hostname or "").lower() not in allowed_hosts
        or not valid_path
        or parsed.query
        or parsed.fragment
        or parsed.username
        or parsed.password
        or parsed.port not in {None, 443}
    ):
        raise BridgeError(
            400,
            "invalid_chatgpt_callback",
            "A URL de retorno do ChatGPT é inválida ou não autorizada.",
        )
    return value


def _canonical_chatgpt_callback(value: str) -> str:
    """Return the validated callback on the canonical ChatGPT frontend host."""

    validated = _validated_chatgpt_callback(value)
    parsed = urlparse(validated)
    return f"https://{CHATGPT_CALLBACK_HOST}{parsed.path}"


def _sanitize_oauth_text(value: Any, *, limit: int = 500) -> str:
    """Return a compact diagnostic string with credential-like values removed."""

    text = html_lib.unescape(str(value or ""))
    text = re.sub(r"<[^>]+>", " ", text)
    text = re.sub(
        r"(?i)\b(Bearer|Basic)\s+[A-Za-z0-9._~+/=-]+",
        r"\1 [REDACTED]",
        text,
    )
    text = re.sub(
        r"(?i)\b(access_token|refresh_token|client_secret|authorization|code|state)\b"
        r"\s*[:=]\s*[\"']?[^\s,;&\"']+",
        r"\1=[REDACTED]",
        text,
    )
    text = re.sub(
        r"(?i)([?&](?:access_token|refresh_token|client_secret|code|state)=)[^&\s]+",
        r"\1[REDACTED]",
        text,
    )
    # Long opaque strings are likely tokens, secrets, state values or codes.
    text = re.sub(r"\b[A-Za-z0-9._~-]{32,}\b", "[REDACTED]", text)
    text = re.sub(r"\s+", " ", text).strip()
    return text[:limit]


def _sanitized_oauth_provider_error(response: requests.Response) -> dict[str, Any]:
    """Extract only safe, useful fields from a failed Mendeley token response."""

    result: dict[str, Any] = {
        "provider_content_type": str(response.headers.get("Content-Type", ""))[:120],
    }
    payload: Any = None
    try:
        payload = response.json()
    except (ValueError, requests.JSONDecodeError):
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


def oauth_authorize_relay():
    client_id = str(request.args.get("client_id", "")).strip()
    original_redirect_uri = str(request.args.get("redirect_uri", "")).strip()
    original_state = str(request.args.get("state", "")).strip()
    response_type = str(request.args.get("response_type", "code")).strip()

    if not client_id or not original_redirect_uri or not original_state:
        raise BridgeError(
            400,
            "invalid_oauth_request",
            "Parâmetros OAuth obrigatórios ausentes.",
        )
    if response_type != "code":
        raise BridgeError(
            400,
            "unsupported_response_type",
            "Somente o fluxo OAuth authorization code é permitido.",
        )

    original_redirect_uri = _validated_chatgpt_callback(original_redirect_uri)
    return_redirect_uri = _canonical_chatgpt_callback(original_redirect_uri)
    relay_state = oauth_relay_serializer.dumps(
        {
            "client_id": client_id,
            "chatgpt_redirect_uri": original_redirect_uri,
            "chatgpt_return_uri": return_redirect_uri,
            "chatgpt_state": original_state,
            "issued_at": int(time.time()),
        }
    )
    upstream_params = {
        "response_type": "code",
        "client_id": client_id,
        "redirect_uri": OAUTH_CALLBACK_URL,
        "state": relay_state,
        "scope": "all",
    }
    _audit(
        "oauth_authorize_relay",
        "redirected",
        client_id=client_id,
        callback_host=urlparse(original_redirect_uri).hostname,
        return_host=urlparse(return_redirect_uri).hostname,
    )
    return redirect(
        f"{MENDELEY_API_BASE}/oauth/authorize?{urlencode(upstream_params)}",
        code=302,
    )


# Replace the original authorization handler registered in _bridge_part2.py.
app.view_functions["oauth_authorize"] = oauth_authorize_relay


@app.get("/oauth/callback")
def oauth_callback_relay():
    relay_state = str(request.args.get("state", "")).strip()
    if not relay_state:
        raise BridgeError(400, "missing_oauth_state", "O estado OAuth está ausente.")

    try:
        relay = oauth_relay_serializer.loads(
            relay_state, max_age=OAUTH_RELAY_TTL_SECONDS
        )
    except SignatureExpired as exc:
        raise BridgeError(
            410,
            "oauth_state_expired",
            "A autorização expirou. Inicie a conexão novamente.",
        ) from exc
    except BadSignature as exc:
        raise BridgeError(
            400,
            "invalid_oauth_state",
            "O estado OAuth é inválido.",
        ) from exc

    stored_return_uri = str(relay.get("chatgpt_return_uri", "")).strip()
    if stored_return_uri:
        target = _canonical_chatgpt_callback(stored_return_uri)
    else:
        # Backward compatibility for authorization attempts started immediately
        # before this deployment.
        target = _canonical_chatgpt_callback(
            str(relay.get("chatgpt_redirect_uri", "")).strip()
        )

    original_state = str(relay.get("chatgpt_state", "")).strip()
    if not original_state:
        raise BridgeError(400, "invalid_oauth_state", "O estado OAuth original está ausente.")

    outgoing = {"state": original_state}
    for key in ("code", "error", "error_description", "error_uri"):
        value = request.args.get(key)
        if value is not None:
            outgoing[key] = value

    if "code" not in outgoing and "error" not in outgoing:
        raise BridgeError(
            400,
            "invalid_oauth_callback",
            "O Mendeley não devolveu código nem erro OAuth.",
        )

    _audit(
        "oauth_callback_relay",
        "forwarded" if "code" in outgoing else "provider_error",
        callback_host=urlparse(target).hostname,
        provider_error=_sanitize_oauth_text(outgoing.get("error")),
    )
    response = redirect(f"{target}?{urlencode(outgoing)}", code=303)
    response.headers["Cache-Control"] = "no-store"
    response.headers["Pragma"] = "no-cache"
    return response


def oauth_token_relay():
    form = request.form.to_dict(flat=True)
    if not form:
        json_payload = request.get_json(silent=True)
        if isinstance(json_payload, dict):
            form = {str(k): str(v) for k, v in json_payload.items() if v is not None}

    auth_header = request.headers.get("Authorization", "")
    client_id = form.pop("client_id", None)
    client_secret = form.pop("client_secret", None)
    headers = {
        "Content-Type": "application/x-www-form-urlencoded",
        "Accept": "application/json",
    }
    auth = None
    if auth_header.lower().startswith("basic "):
        headers["Authorization"] = auth_header
    elif client_id and client_secret:
        auth = (client_id, client_secret)
    else:
        raise BridgeError(
            401,
            "missing_client_credentials",
            "Credenciais OAuth do aplicativo ausentes.",
        )

    grant_type = form.get("grant_type")
    if grant_type not in {"authorization_code", "refresh_token"}:
        raise BridgeError(
            400,
            "unsupported_grant_type",
            "grant_type OAuth não suportado.",
        )

    if grant_type == "authorization_code":
        if not form.get("code"):
            raise BridgeError(400, "missing_authorization_code", "Código OAuth ausente.")
        # Mendeley must receive the same redirect URI used in its authorization
        # request, which is the bridge callback rather than ChatGPT's callback.
        form["redirect_uri"] = OAUTH_CALLBACK_URL
    else:
        form.pop("redirect_uri", None)

    try:
        response = requests.post(
            f"{MENDELEY_API_BASE}/oauth/token",
            headers=headers,
            data=form,
            auth=auth,
            timeout=REQUEST_TIMEOUT_SECONDS,
        )
    except requests.RequestException as exc:
        raise BridgeError(
            502,
            "oauth_upstream_error",
            "Falha ao acessar o OAuth do Mendeley.",
        ) from exc

    audit_fields: dict[str, Any] = {
        "grant_type": grant_type,
        "upstream_status": response.status_code,
    }
    if response.status_code != 200:
        audit_fields.update(_sanitized_oauth_provider_error(response))
    _audit(
        "oauth_token_relay",
        "success" if response.status_code == 200 else "upstream_error",
        **audit_fields,
    )

    token_response = Response(
        response.content,
        status=response.status_code,
        content_type=response.headers.get("Content-Type", "application/json"),
    )
    token_response.headers["Cache-Control"] = "no-store"
    token_response.headers["Pragma"] = "no-cache"
    return token_response


# Replace the original token handler registered in _bridge_part2.py.
app.view_functions["oauth_token"] = oauth_token_relay
