@app.post("/api/write/folders/preview")
def preview_create_folder():
    data = _json_body()
    name = str(data.get("name", "")).strip()
    parent_id = str(data.get("parent_id", "")).strip() or None
    if len(name) < 2 or len(name) > 255:
        raise BridgeError(400, "invalid_folder_name", "O nome da coleção deve ter entre 2 e 255 caracteres.")
    if parent_id and parent_id != "root" and not _folder_exists(parent_id):
        raise BridgeError(404, "parent_folder_not_found", "A coleção-pai não existe.")
    response = _mendeley_request("GET", "/folders", kind="folder", params={"limit": 500}, expected=(200,))
    folders = _json_response(response)
    duplicates = [
        {"id": folder.get("id"), "name": folder.get("name"), "parent_id": folder.get("parent_id")}
        for folder in folders
        if isinstance(folder, dict)
        and _normalize_title(folder.get("name")) == _normalize_title(name)
        and (folder.get("parent_id") or "root") == (parent_id or "root")
    ] if isinstance(folders, list) else []
    if duplicates:
        return jsonify({"status": "duplicate_folder", "can_confirm": False, "duplicates": duplicates}), 409
    payload = {"name": name, "parent_id": parent_id}
    token = _make_preview("create_folder", payload)
    _audit("create_folder_preview", "ready", name=name, parent_id=parent_id)
    return jsonify(
        {
            "status": "preview_ready",
            "folder": payload,
            "confirmation_phrase": "CONFIRMO SALVAR",
            "expires_in_seconds": PREVIEW_TTL_SECONDS,
            "preview_token": token,
        }
    )


@app.post("/api/write/folders/confirm")
def confirm_create_folder():
    data = _json_body()
    _require_confirmation(data)
    payload = _load_preview(str(data.get("preview_token", "")), "create_folder")
    body = {"name": payload["name"]}
    if payload.get("parent_id"):
        body["parent_id"] = payload["parent_id"]
    response = _mendeley_request("POST", "/folders", kind="folder", json_body=body, expected=(200, 201))
    created = _json_response(response)
    _audit("create_folder_confirm", "created", folder_id=(created or {}).get("id"), name=payload.get("name"))
    return jsonify({"status": "created", "folder": created}), 201


@app.post("/api/write/pdf-import/preview")
def preview_import_pdf():
    data = _json_body()
    file_ref = _extract_file_ref(data)
    folder_id = str(data.get("folder_id", "")).strip() or None
    allow_duplicate = data.get("allow_duplicate") is True
    if folder_id and not _folder_exists(folder_id):
        raise BridgeError(404, "folder_not_found", "A coleção informada não existe.")
    content = _safe_download_pdf(file_ref["download_link"])
    filehash = hashlib.sha1(content).hexdigest()
    duplicates = _file_hash_exists(filehash)
    if duplicates and not allow_duplicate:
        _audit("import_pdf_preview", "blocked_duplicate", filehash=filehash, duplicate_count=len(duplicates))
        return jsonify({"status": "duplicate_file", "can_confirm": False, "duplicates": duplicates, "filehash": filehash}), 409
    cache_key = _cache_pdf(content)
    payload = {
        "file": {"name": file_ref["name"], "mime_type": "application/pdf"},
        "cache_key": cache_key,
        "filehash": filehash,
        "size": len(content),
        "folder_id": folder_id,
        "allow_duplicate": allow_duplicate,
    }
    token = _make_preview("import_pdf", payload, file_action=True)
    _audit("import_pdf_preview", "ready", file_name=file_ref["name"], size=len(content), filehash=filehash, folder_id=folder_id)
    return jsonify(
        {
            "status": "preview_ready",
            "file": {"name": file_ref["name"], "mime_type": "application/pdf", "size": len(content), "sha1": filehash},
            "folder_id": folder_id,
            "duplicates": duplicates,
            "confirmation_phrase": "CONFIRMO SALVAR",
            "expires_in_seconds": FILE_PREVIEW_TTL_SECONDS,
            "preview_token": token,
        }
    )


@app.post("/api/write/pdf-import/confirm")
def confirm_import_pdf():
    data = _json_body()
    _require_confirmation(data)
    payload = _load_preview(str(data.get("preview_token", "")), "import_pdf")
    file_ref = payload["file"]
    cache_key = str(payload.get("cache_key", ""))
    content = _load_cached_pdf(cache_key)
    filehash = hashlib.sha1(content).hexdigest()
    if filehash != payload.get("filehash"):
        raise BridgeError(409, "file_changed", "O PDF mudou depois da prévia.")
    duplicates = _file_hash_exists(filehash)
    if duplicates and payload.get("allow_duplicate") is not True:
        raise BridgeError(409, "duplicate_file", "O PDF já existe na biblioteca.", duplicates)
    headers = {
        "Content-Type": "application/pdf",
        "Content-Disposition": f'attachment; filename="{_safe_filename(file_ref["name"])}"',
    }
    response = _mendeley_request(
        "POST",
        "/documents",
        kind="document",
        data=content,
        extra_headers=headers,
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
    _delete_cached_pdf(cache_key)
    _audit("import_pdf_confirm", "created", document_id=document_id, filehash=filehash, folder_id=folder_id)
    return jsonify({"status": "created_from_pdf", "document": created, "folder_assignment": folder_result}), 201


@app.post("/api/write/documents/<document_id>/files/preview")
def preview_attach_pdf(document_id: str):
    data = _json_body()
    _mendeley_request("GET", f"/documents/{document_id}", kind="document", expected=(200,))
    file_ref = _extract_file_ref(data)
    content = _safe_download_pdf(file_ref["download_link"])
    filehash = hashlib.sha1(content).hexdigest()
    duplicates = _file_hash_exists(filehash, document_id=document_id)
    if duplicates:
        return jsonify({"status": "duplicate_file", "can_confirm": False, "duplicates": duplicates, "filehash": filehash}), 409
    cache_key = _cache_pdf(content)
    payload = {
        "document_id": document_id,
        "file": {"name": file_ref["name"], "mime_type": "application/pdf"},
        "cache_key": cache_key,
        "filehash": filehash,
        "size": len(content),
    }
    token = _make_preview("attach_pdf", payload, file_action=True)
    _audit("attach_pdf_preview", "ready", document_id=document_id, file_name=file_ref["name"], size=len(content), filehash=filehash)
    return jsonify(
        {
            "status": "preview_ready",
            "document_id": document_id,
            "file": {"name": file_ref["name"], "mime_type": "application/pdf", "size": len(content), "sha1": filehash},
            "confirmation_phrase": "CONFIRMO SALVAR",
            "expires_in_seconds": FILE_PREVIEW_TTL_SECONDS,
            "preview_token": token,
        }
    )


@app.post("/api/write/documents/<document_id>/files/confirm")
def confirm_attach_pdf(document_id: str):
    data = _json_body()
    _require_confirmation(data)
    payload = _load_preview(str(data.get("preview_token", "")), "attach_pdf")
    if payload.get("document_id") != document_id:
        raise BridgeError(400, "document_mismatch", "A prévia pertence a outra referência.")
    file_ref = payload["file"]
    cache_key = str(payload.get("cache_key", ""))
    content = _load_cached_pdf(cache_key)
    filehash = hashlib.sha1(content).hexdigest()
    if filehash != payload.get("filehash"):
        raise BridgeError(409, "file_changed", "O PDF mudou depois da prévia.")
    duplicates = _file_hash_exists(filehash, document_id=document_id)
    if duplicates:
        raise BridgeError(409, "duplicate_file", "Este PDF já está anexado à referência.", duplicates)
    filename = _safe_filename(file_ref["name"])
    response = _mendeley_request(
        "POST",
        "/files",
        kind="file",
        data=content,
        extra_headers={
            "Content-Type": "application/pdf",
            "Content-Disposition": f'attachment; filename="{filename}"',
            "Link": f'<{MENDELEY_API_BASE}/documents/{document_id}>; rel="document"',
            "Content-Length": str(len(content)),
        },
        expected=(200, 201),
    )
    created = _json_response(response)
    _delete_cached_pdf(cache_key)
    _audit("attach_pdf_confirm", "created", document_id=document_id, file_id=(created or {}).get("id"), filehash=filehash)
    return jsonify({"status": "file_attached", "file": created}), 201


if __name__ == "__main__":
    port = int(os.getenv("PORT", "10000"))
    app.run(host="0.0.0.0", port=port)
