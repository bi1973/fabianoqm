def _iter_documents(max_items: int = DUPLICATE_SCAN_LIMIT) -> list[dict[str, Any]]:
    collected: list[dict[str, Any]] = []
    url = f"{MENDELEY_API_BASE}/documents"
    params: dict[str, Any] | None = {"view": "all", "limit": min(500, max_items)}
    while url and len(collected) < max_items:
        response = _mendeley_request("GET", url, kind="document", params=params, expected=(200,))
        params = None
        data = _json_response(response)
        if isinstance(data, list):
            collected.extend(item for item in data if isinstance(item, dict))
        next_url = ""
        link_header = response.headers.get("Link", "")
        if link_header:
            for link in requests.utils.parse_header_links(link_header.rstrip(">").replace(">,<", ">, <")):
                if link.get("rel") == "next" and isinstance(link.get("url"), str):
                    next_url = link["url"]
                    break
        url = next_url
    return collected[:max_items]


def _find_duplicates(candidate: dict[str, Any], *, exclude_id: str | None = None) -> list[dict[str, Any]]:
    candidate_ids = _document_identifiers(candidate)
    candidate_doi = _normalize_doi(candidate_ids.get("doi"))
    candidate_title = _normalize_title(candidate.get("title"))
    candidate_year = candidate.get("year")
    duplicates: list[dict[str, Any]] = []

    for document in _iter_documents():
        if exclude_id and document.get("id") == exclude_id:
            continue
        doc_doi = _normalize_doi(_document_identifiers(document).get("doi"))
        doc_title = _normalize_title(document.get("title"))
        doc_year = document.get("year")
        reason = ""
        if candidate_doi and doc_doi and candidate_doi == doc_doi:
            reason = "doi"
        elif candidate_title and doc_title == candidate_title and (
            candidate_year is None or doc_year is None or str(candidate_year) == str(doc_year)
        ):
            reason = "title_year"
        if reason:
            duplicates.append(
                {
                    "id": document.get("id"),
                    "title": document.get("title"),
                    "year": document.get("year"),
                    "doi": doc_doi or None,
                    "reason": reason,
                }
            )
        if len(duplicates) >= 20:
            break
    return duplicates


def _folder_exists(folder_id: str) -> bool:
    response = _mendeley_request("GET", "/folders", kind="folder", params={"limit": 500}, expected=(200,))
    data = _json_response(response)
    return isinstance(data, list) and any(isinstance(item, dict) and item.get("id") == folder_id for item in data)


def _add_to_folder(folder_id: str, document_id: str) -> dict[str, Any]:
    response = _mendeley_request(
        "POST",
        f"/folders/{folder_id}/documents",
        kind="folder",
        json_body={"id": document_id},
        expected=(200, 201, 204),
    )
    return {"folder_id": folder_id, "status": "added", "response": _json_response(response)}


def _make_preview(action: str, payload: dict[str, Any], *, file_action: bool = False) -> str:
    token = _access_token()
    envelope = {
        "action": action,
        "payload": payload,
        "token_fingerprint": _token_fingerprint(token),
        "nonce": secrets.token_urlsafe(12),
        "created_at": int(time.time()),
        "file_action": file_action,
    }
    return serializer.dumps(envelope)


def _load_preview(preview_token: str, expected_action: str) -> dict[str, Any]:
    if not isinstance(preview_token, str) or len(preview_token) < 20:
        raise BridgeError(400, "invalid_preview_token", "Token de prévia ausente ou inválido.")
    max_age = FILE_PREVIEW_TTL_SECONDS if expected_action in {"import_pdf", "attach_pdf"} else PREVIEW_TTL_SECONDS
    try:
        envelope = serializer.loads(preview_token, max_age=max_age)
    except SignatureExpired as exc:
        raise BridgeError(410, "preview_expired", "A prévia expirou. Gere uma nova prévia.") from exc
    except BadSignature as exc:
        raise BridgeError(400, "invalid_preview_token", "Token de prévia inválido.") from exc
    if envelope.get("action") != expected_action:
        raise BridgeError(400, "wrong_preview_action", "A prévia não corresponde a esta operação.")
    if envelope.get("token_fingerprint") != _token_fingerprint(_access_token()):
        raise BridgeError(403, "preview_user_mismatch", "A prévia pertence a outra sessão Mendeley.")
    payload = envelope.get("payload")
    if not isinstance(payload, dict):
        raise BridgeError(400, "invalid_preview_payload", "Conteúdo da prévia inválido.")
    return payload


def _require_confirmation(data: dict[str, Any]) -> None:
    if data.get("confirmation") != "CONFIRMO SALVAR":
        raise BridgeError(400, "confirmation_required", 'Envie confirmation exatamente como "CONFIRMO SALVAR".')


def _document_digest(document: dict[str, Any]) -> str:
    relevant = {key: document.get(key) for key in sorted(DOCUMENT_ALLOWED_FIELDS) if key in document}
    return hashlib.sha256(json.dumps(relevant, sort_keys=True, ensure_ascii=False, default=str).encode()).hexdigest()


def _safe_filename(value: str) -> str:
    value = value.replace("\r", "").replace("\n", "").replace(chr(34), "").strip()
    value = re.sub(r"[^A-Za-z0-9._() \-À-ÿ]", "_", value)
    if not value.lower().endswith(".pdf"):
        value += ".pdf"
    return value[:255] or "document.pdf"


def _cleanup_file_cache() -> None:
    FILE_CACHE_DIR.mkdir(parents=True, exist_ok=True)
    cutoff = time.time() - FILE_PREVIEW_TTL_SECONDS
    for path in FILE_CACHE_DIR.glob("*.pdf"):
        try:
            if path.stat().st_mtime < cutoff:
                path.unlink(missing_ok=True)
        except OSError:
            continue


def _cache_pdf(content: bytes) -> str:
    _cleanup_file_cache()
    key = secrets.token_urlsafe(24)
    path = FILE_CACHE_DIR / f"{key}.pdf"
    path.write_bytes(content)
    try:
        path.chmod(0o600)
    except OSError:
        pass
    return key


def _load_cached_pdf(key: str) -> bytes:
    _cleanup_file_cache()
    if not re.fullmatch(r"[A-Za-z0-9_-]{20,80}", key or ""):
        raise BridgeError(400, "invalid_file_cache_key", "Referência temporária do PDF inválida.")
    path = FILE_CACHE_DIR / f"{key}.pdf"
    try:
        content = path.read_bytes()
    except FileNotFoundError as exc:
        raise BridgeError(410, "file_preview_expired", "O PDF temporário expirou. Gere uma nova prévia.") from exc
    if len(content) > MAX_PDF_BYTES or not content.startswith(b"%PDF-"):
        raise BridgeError(400, "invalid_cached_pdf", "O PDF temporário é inválido.")
    return content


def _delete_cached_pdf(key: str) -> None:
    try:
        (FILE_CACHE_DIR / f"{key}.pdf").unlink(missing_ok=True)
    except OSError:
        logger.warning("file_cache_delete_failed")


def _extract_file_ref(data: dict[str, Any]) -> dict[str, Any]:
    refs = data.get("openaiFileIdRefs")
    if not isinstance(refs, list) or len(refs) != 1:
        raise BridgeError(400, "one_pdf_required", "Envie exatamente um PDF por operação.")
    ref = refs[0]
    if isinstance(ref, str):
        raise BridgeError(400, "missing_file_metadata", "A referência do arquivo não contém os metadados esperados.")
    if not isinstance(ref, dict):
        raise BridgeError(400, "invalid_file_reference", "Referência de arquivo inválida.")
    mime_type = str(ref.get("mime_type", "")).lower()
    name = _safe_filename(str(ref.get("name", "document.pdf")))
    link = str(ref.get("download_link", "")).strip()
    if mime_type != "application/pdf" and not name.lower().endswith(".pdf"):
        raise BridgeError(400, "pdf_only", "Somente arquivos PDF são permitidos.")
    if not link:
        raise BridgeError(400, "missing_download_link", "O link temporário do PDF não foi fornecido.")
    return {"name": name[:255], "mime_type": "application/pdf", "download_link": link}


def _validate_download_url(url: str) -> None:
    parsed = urlparse(url)
    host = (parsed.hostname or "").lower()
    if parsed.scheme != "https" or not (
        host == "files.oaiusercontent.com"
        or host.endswith(".oaiusercontent.com")
        or host.endswith(".openaiusercontent.com")
    ):
        raise BridgeError(400, "unsafe_file_url", "O PDF deve vir de um link temporário de arquivo do ChatGPT.")


def _safe_download_pdf(url: str) -> bytes:
    current = url
    for _ in range(4):
        _validate_download_url(current)
        try:
            response = requests.get(current, stream=True, timeout=REQUEST_TIMEOUT_SECONDS, allow_redirects=False)
        except requests.RequestException as exc:
            raise BridgeError(502, "file_download_failed", "Não foi possível baixar o PDF temporário.", str(exc)) from exc
        if response.status_code in {301, 302, 303, 307, 308}:
            location = response.headers.get("Location")
            if not location:
                raise BridgeError(502, "file_redirect_failed", "Redirecionamento do PDF sem destino.")
            current = urljoin(current, location)
            continue
        if response.status_code != 200:
            raise BridgeError(response.status_code, "file_download_failed", "O link temporário do PDF não pôde ser lido.")
        length = response.headers.get("Content-Length")
        if length:
            try:
                if int(length) > MAX_PDF_BYTES:
                    raise BridgeError(413, "file_too_large", "O PDF ultrapassa o limite de 10 MB.")
            except ValueError:
                pass
        chunks: list[bytes] = []
        total = 0
        for chunk in response.iter_content(64 * 1024):
            if not chunk:
                continue
            total += len(chunk)
            if total > MAX_PDF_BYTES:
                raise BridgeError(413, "file_too_large", "O PDF ultrapassa o limite de 10 MB.")
            chunks.append(chunk)
        content = b"".join(chunks)
        if not content.startswith(b"%PDF-"):
            raise BridgeError(400, "invalid_pdf", "O arquivo não possui uma assinatura PDF válida.")
        return content
    raise BridgeError(502, "too_many_redirects", "Muitos redirecionamentos ao baixar o PDF.")


def _file_hash_exists(filehash: str, *, document_id: str | None = None) -> list[dict[str, Any]]:
    params = {"document_id": document_id} if document_id else None
    response = _mendeley_request("GET", "/files", kind="file", params=params, expected=(200,))
    data = _json_response(response)
    if not isinstance(data, list):
        return []
    matches = []
    for item in data:
        if isinstance(item, dict) and str(item.get("filehash", "")).lower() == filehash.lower():
            matches.append(
                {
                    "id": item.get("id"),
                    "document_id": item.get("document_id"),
                    "file_name": item.get("file_name"),
                    "size": item.get("size"),
                    "filehash": item.get("filehash"),
                }
            )
    return matches


@app.before_request
def apply_rate_limit():
    if request.path.startswith("/api/") and request.method != "OPTIONS":
        _rate_limit()


@app.get("/")
def root():
    return jsonify(
        {
            "status": "ok",
            "service": APP_NAME,
            "version": APP_VERSION,
            "mode": "controlled-read-write",
            "write_requires_preview": True,
            "write_requires_confirmation": True,
            "delete_enabled": False,
            "signing_secret_ephemeral": SIGNING_SECRET_EPHEMERAL,
        }
    )


@app.get("/health")
def health():
    status = 200 if not SIGNING_SECRET_EPHEMERAL else 503
    return (
        jsonify(
            {
                "status": "ok" if status == 200 else "configuration_required",
                "service": APP_NAME,
                "signing_secret_configured": not SIGNING_SECRET_EPHEMERAL,
            }
        ),
        status,
    )


@app.get("/oauth/authorize")
def oauth_authorize():
    allowed = {"client_id", "redirect_uri", "response_type", "scope", "state"}
    params = {key: value for key, value in request.args.items() if key in allowed}
    params["response_type"] = "code"
    params["scope"] = "all"
    required = {"client_id", "redirect_uri", "state"}
    if not required.issubset(params):
        raise BridgeError(400, "invalid_oauth_request", "Parâmetros OAuth obrigatórios ausentes.")
    return redirect(f"{MENDELEY_API_BASE}/oauth/authorize?{urlencode(params)}", code=302)


@app.post("/oauth/token")
def oauth_token():
    form = request.form.to_dict(flat=True)
    if not form:
        json_payload = request.get_json(silent=True)
        if isinstance(json_payload, dict):
            form = {str(k): str(v) for k, v in json_payload.items() if v is not None}
    auth_header = request.headers.get("Authorization", "")
    client_id = form.pop("client_id", None)
    client_secret = form.pop("client_secret", None)
    headers = {"Content-Type": "application/x-www-form-urlencoded", "Accept": "application/json"}
    auth = None
    if auth_header.lower().startswith("basic "):
        headers["Authorization"] = auth_header
    elif client_id and client_secret:
        auth = (client_id, client_secret)
    else:
        raise BridgeError(401, "missing_client_credentials", "Credenciais OAuth do aplicativo ausentes.")
    if form.get("grant_type") not in {"authorization_code", "refresh_token"}:
        raise BridgeError(400, "unsupported_grant_type", "grant_type OAuth não suportado.")
    try:
        response = requests.post(
            f"{MENDELEY_API_BASE}/oauth/token",
            headers=headers,
            data=form,
            auth=auth,
            timeout=REQUEST_TIMEOUT_SECONDS,
        )
    except requests.RequestException as exc:
        raise BridgeError(502, "oauth_upstream_error", "Falha ao acessar o OAuth do Mendeley.") from exc
    return Response(response.content, status=response.status_code, content_type=response.headers.get("Content-Type", "application/json"))

