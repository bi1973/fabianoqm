@app.get("/api/profile")
def get_profile():
    response = _mendeley_request("GET", "/profiles/me", kind="profile", expected=(200,))
    return jsonify(_json_response(response))


@app.get("/api/folders")
def list_folders():
    response = _mendeley_request("GET", "/folders", kind="folder", params={"limit": 500}, expected=(200,))
    return jsonify(_json_response(response))


@app.get("/api/documents")
def list_documents():
    q = str(request.args.get("q", "")).strip()
    folder_id = str(request.args.get("folder_id", "")).strip()
    limit = min(max(int(request.args.get("limit", 20)), 1), 50)
    if folder_id:
        path = f"/folders/{folder_id}/documents"
        response = _mendeley_request("GET", path, kind="document", params={"limit": 500}, expected=(200,))
    else:
        response = _mendeley_request("GET", "/documents", kind="document", params={"view": "all", "limit": 500}, expected=(200,))
    data = _json_response(response)
    documents = data if isinstance(data, list) else []
    if q:
        qn = _normalize_title(q)
        filtered = []
        for doc in documents:
            if not isinstance(doc, dict):
                continue
            haystack = " ".join(
                str(doc.get(key, ""))
                for key in ("title", "source", "abstract", "year", "keywords", "tags", "identifiers", "authors")
            )
            if qn in _normalize_title(haystack):
                filtered.append(doc)
        documents = filtered
    return jsonify(documents[:limit])


@app.get("/api/documents/<document_id>")
def get_document(document_id: str):
    response = _mendeley_request("GET", f"/documents/{document_id}", kind="document", params={"view": "all"}, expected=(200,))
    return jsonify(_json_response(response))


@app.get("/api/documents/<document_id>/files")
def list_document_files(document_id: str):
    response = _mendeley_request("GET", "/files", kind="file", params={"document_id": document_id}, expected=(200,))
    return jsonify(_json_response(response))


@app.get("/api/documents/<document_id>/pdfs")
def get_document_pdfs(document_id: str):
    response = _mendeley_request("GET", "/files", kind="file", params={"document_id": document_id}, expected=(200,))
    files = _json_response(response)
    urls: list[str] = []
    metadata: list[dict[str, Any]] = []
    if isinstance(files, list):
        for item in files[:10]:
            if not isinstance(item, dict) or item.get("mime_type") != "application/pdf":
                continue
            file_id = item.get("id")
            if not file_id or int(item.get("size") or 0) > MAX_PDF_BYTES:
                continue
            download = _mendeley_request(
                "GET",
                f"/files/{file_id}",
                kind="file",
                allow_redirects=False,
                expected=(302, 303, 307, 308),
            )
            location = download.headers.get("Location")
            if location:
                urls.append(location)
                metadata.append({key: item.get(key) for key in ("id", "file_name", "mime_type", "size", "filehash")})
    return jsonify({"openaiFileResponse": urls, "files": metadata})


@app.get("/api/audit")
def get_audit():
    limit = min(max(int(request.args.get("limit", 50)), 1), 200)
    return jsonify(list(RECENT_AUDIT)[-limit:])


@app.post("/api/write/documents/preview")
def preview_create_document():
    data = _json_body()
    payload = _clean_document_payload(data.get("document") if isinstance(data.get("document"), dict) else data)
    folder_id = str(data.get("folder_id", "")).strip() or None
    allow_duplicate = data.get("allow_duplicate") is True
    if folder_id and not _folder_exists(folder_id):
        raise BridgeError(404, "folder_not_found", "A coleção informada não existe.")
    duplicates = _find_duplicates(payload)
    if duplicates and not allow_duplicate:
        _audit("create_document_preview", "blocked_duplicate", duplicate_count=len(duplicates))
        return jsonify(
            {
                "status": "duplicate_found",
                "can_confirm": False,
                "duplicates": duplicates,
                "message": "A referência não será salva enquanto allow_duplicate não for explicitamente true.",
            }
        ), 409
    preview_payload = {
        "document": payload,
        "folder_id": folder_id,
        "allow_duplicate": allow_duplicate,
    }
    token = _make_preview("create_document", preview_payload)
    _audit("create_document_preview", "ready", title=payload.get("title"), duplicate_count=len(duplicates), folder_id=folder_id)
    return jsonify(
        {
            "status": "preview_ready",
            "can_confirm": True,
            "document": payload,
            "folder_id": folder_id,
            "duplicates": duplicates,
            "confirmation_phrase": "CONFIRMO SALVAR",
            "expires_in_seconds": PREVIEW_TTL_SECONDS,
            "preview_token": token,
        }
    )


@app.post("/api/write/documents/confirm")
def confirm_create_document():
    data = _json_body()
    _require_confirmation(data)
    payload = _load_preview(str(data.get("preview_token", "")), "create_document")
    document = _clean_document_payload(payload["document"])
    allow_duplicate = payload.get("allow_duplicate") is True
    duplicates = _find_duplicates(document)
    if duplicates and not allow_duplicate:
        raise BridgeError(409, "duplicate_found", "Foi encontrada uma duplicata antes da gravação.", duplicates)
    response = _mendeley_request(
        "POST",
        "/documents",
        kind="document",
        json_body=document,
        expected=(200, 201),
    )
    created = _json_response(response)
    document_id = created.get("id") if isinstance(created, dict) else None
    folder_result = None
    folder_id = payload.get("folder_id")
    if folder_id and document_id:
        try:
            folder_result = _add_to_folder(folder_id, document_id)
        except BridgeError as exc:
            folder_result = {"folder_id": folder_id, "status": "failed", "error": exc.details or exc.message}
    _audit("create_document_confirm", "created", document_id=document_id, folder_id=folder_id, folder_status=(folder_result or {}).get("status"))
    return jsonify({"status": "created", "document": created, "folder_assignment": folder_result}), 201


@app.post("/api/write/documents/<document_id>/update-preview")
def preview_update_document(document_id: str):
    data = _json_body()
    changes_raw = data.get("changes") if isinstance(data.get("changes"), dict) else data
    changes = _clean_document_payload(changes_raw, update=True)
    current_response = _mendeley_request("GET", f"/documents/{document_id}", kind="document", params={"view": "all"}, expected=(200,))
    current = _json_response(current_response)
    if not isinstance(current, dict):
        raise BridgeError(502, "invalid_document", "O Mendeley não devolveu a referência esperada.")
    after = dict(current)
    after.update(changes)
    duplicates = _find_duplicates(after, exclude_id=document_id)
    preview_payload = {
        "document_id": document_id,
        "changes": changes,
        "before_digest": _document_digest(current),
    }
    token = _make_preview("update_document", preview_payload)
    _audit("update_document_preview", "ready", document_id=document_id, fields=sorted(changes.keys()), duplicate_count=len(duplicates))
    return jsonify(
        {
            "status": "preview_ready",
            "document_id": document_id,
            "before": {key: current.get(key) for key in changes},
            "after": {key: after.get(key) for key in changes},
            "duplicate_warnings": duplicates,
            "confirmation_phrase": "CONFIRMO SALVAR",
            "expires_in_seconds": PREVIEW_TTL_SECONDS,
            "preview_token": token,
        }
    )


@app.post("/api/write/documents/<document_id>/update-confirm")
def confirm_update_document(document_id: str):
    data = _json_body()
    _require_confirmation(data)
    payload = _load_preview(str(data.get("preview_token", "")), "update_document")
    if payload.get("document_id") != document_id:
        raise BridgeError(400, "document_mismatch", "A prévia pertence a outra referência.")
    current_response = _mendeley_request("GET", f"/documents/{document_id}", kind="document", params={"view": "all"}, expected=(200,))
    current = _json_response(current_response)
    if not isinstance(current, dict) or _document_digest(current) != payload.get("before_digest"):
        raise BridgeError(409, "document_changed", "A referência mudou após a prévia. Gere outra prévia.")
    changes = _clean_document_payload(payload["changes"], update=True)
    response = _mendeley_request(
        "PATCH",
        f"/documents/{document_id}",
        kind="document",
        json_body=changes,
        expected=(200,),
    )
    updated = _json_response(response)
    _audit("update_document_confirm", "updated", document_id=document_id, fields=sorted(changes.keys()))
    return jsonify({"status": "updated", "document": updated})

