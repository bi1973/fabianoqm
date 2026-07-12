from __future__ import annotations

import hashlib
import hmac
import json
import logging
import os
import re
import secrets
import time
import unicodedata
from collections import defaultdict, deque
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable
from urllib.parse import urlencode, urljoin, urlparse

import requests
from flask import Flask, Response, jsonify, redirect, request
from itsdangerous import BadSignature, SignatureExpired, URLSafeTimedSerializer


APP_NAME = "mendeley-controlled-read-write-bridge"
APP_VERSION = "1.0.0"
MENDELEY_API_BASE = os.getenv("MENDELEY_API_BASE", "https://api.mendeley.com").rstrip("/")
REQUEST_TIMEOUT_SECONDS = int(os.getenv("REQUEST_TIMEOUT_SECONDS", "35"))
PREVIEW_TTL_SECONDS = int(os.getenv("PREVIEW_TTL_SECONDS", "900"))
FILE_PREVIEW_TTL_SECONDS = int(os.getenv("FILE_PREVIEW_TTL_SECONDS", "240"))
MAX_PDF_BYTES = int(os.getenv("MAX_PDF_BYTES", str(10 * 1024 * 1024)))
DUPLICATE_SCAN_LIMIT = int(os.getenv("DUPLICATE_SCAN_LIMIT", "1000"))
AUDIT_LOG_PATH = Path(os.getenv("AUDIT_LOG_PATH", "/tmp/mendeley-bridge-audit.jsonl"))
FILE_CACHE_DIR = Path(os.getenv("FILE_CACHE_DIR", "/tmp/mendeley-bridge-file-cache"))
SIGNING_SECRET = os.getenv("SIGNING_SECRET", "")
RATE_LIMIT_PER_MINUTE = int(os.getenv("RATE_LIMIT_PER_MINUTE", "60"))

if not SIGNING_SECRET:
    # The app can boot for health checks, but write previews/confirms are disabled.
    SIGNING_SECRET = secrets.token_urlsafe(32)
    SIGNING_SECRET_EPHEMERAL = True
else:
    SIGNING_SECRET_EPHEMERAL = False

app = Flask(__name__)
app.config["JSON_SORT_KEYS"] = False

logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO").upper(),
    format="%(asctime)s %(levelname)s %(message)s",
)
logger = logging.getLogger(APP_NAME)
serializer = URLSafeTimedSerializer(SIGNING_SECRET, salt="mendeley-controlled-write-v1")

RECENT_AUDIT: deque[dict[str, Any]] = deque(maxlen=200)
RATE_BUCKETS: dict[str, deque[float]] = defaultdict(deque)

DOCUMENT_ALLOWED_FIELDS = {
    "title",
    "type",
    "authors",
    "editors",
    "translators",
    "year",
    "source",
    "abstract",
    "identifiers",
    "keywords",
    "tags",
    "websites",
    "pages",
    "volume",
    "issue",
    "publisher",
    "city",
    "edition",
    "institution",
    "series",
    "month",
    "day",
    "accessed",
}
DOCUMENT_UPDATE_FIELDS = DOCUMENT_ALLOWED_FIELDS - {"type"}

VENDOR_ACCEPT = {
    "document": "application/vnd.mendeley-document.1+json",
    "folder": "application/vnd.mendeley-folder.1+json",
    "file": "application/vnd.mendeley-file.1+json",
    "profile": "application/vnd.mendeley-profile.1+json",
}


class BridgeError(Exception):
    def __init__(self, status: int, code: str, message: str, details: Any = None):
        super().__init__(message)
        self.status = status
        self.code = code
        self.message = message
        self.details = details


@app.errorhandler(BridgeError)
def handle_bridge_error(exc: BridgeError):
    body: dict[str, Any] = {"error": exc.code, "message": exc.message}
    if exc.details is not None:
        body["details"] = exc.details
    return jsonify(body), exc.status


@app.errorhandler(404)
def handle_404(_exc):
    return jsonify({"error": "not_found", "message": "Rota não encontrada."}), 404


@app.errorhandler(405)
def handle_405(_exc):
    return jsonify({"error": "method_not_allowed", "message": "Método não permitido."}), 405


@app.errorhandler(Exception)
def handle_unexpected(exc: Exception):
    logger.exception("unexpected_error")
    return jsonify({"error": "internal_error", "message": "Erro interno no bridge."}), 500


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _json_body() -> dict[str, Any]:
    data = request.get_json(silent=True)
    if not isinstance(data, dict):
        raise BridgeError(400, "invalid_json", "Envie um objeto JSON válido.")
    return data


def _authorization_header() -> str:
    value = request.headers.get("Authorization", "").strip()
    if not value.lower().startswith("bearer ") or len(value.split(" ", 1)[1].strip()) < 8:
        raise BridgeError(401, "missing_authorization", "Token OAuth do Mendeley ausente ou inválido.")
    return value


def _access_token() -> str:
    return _authorization_header().split(" ", 1)[1].strip()


def _token_fingerprint(token: str) -> str:
    return hmac.new(SIGNING_SECRET.encode(), token.encode(), hashlib.sha256).hexdigest()[:24]


def _rate_limit() -> None:
    fingerprint = _token_fingerprint(_access_token())
    now = time.time()
    bucket = RATE_BUCKETS[fingerprint]
    while bucket and bucket[0] < now - 60:
        bucket.popleft()
    if len(bucket) >= RATE_LIMIT_PER_MINUTE:
        raise BridgeError(429, "rate_limited", "Muitas solicitações. Aguarde um minuto.")
    bucket.append(now)


def _audit(event: str, status: str, **fields: Any) -> None:
    safe_fields = {
        key: value
        for key, value in fields.items()
        if key not in {"access_token", "refresh_token", "client_secret", "preview_token"}
    }
    record = {
        "timestamp": _utc_now(),
        "event": event,
        "status": status,
        **safe_fields,
    }
    RECENT_AUDIT.append(record)
    logger.info("audit %s", json.dumps(record, ensure_ascii=False, default=str))
    try:
        AUDIT_LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
        with AUDIT_LOG_PATH.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(record, ensure_ascii=False, default=str) + "\n")
    except OSError:
        logger.warning("audit_file_write_failed")


def _mendeley_headers(kind: str = "document", *, content_type: str | None = None) -> dict[str, str]:
    headers = {
        "Authorization": _authorization_header(),
        "Accept": VENDOR_ACCEPT.get(kind, "application/json"),
        "User-Agent": f"{APP_NAME}/{APP_VERSION}",
    }
    if content_type:
        headers["Content-Type"] = content_type
    return headers


def _mendeley_request(
    method: str,
    path_or_url: str,
    *,
    kind: str = "document",
    params: dict[str, Any] | None = None,
    json_body: dict[str, Any] | None = None,
    data: bytes | None = None,
    extra_headers: dict[str, str] | None = None,
    allow_redirects: bool = True,
    expected: Iterable[int] = (200,),
) -> requests.Response:
    url = path_or_url if path_or_url.startswith("https://") else f"{MENDELEY_API_BASE}{path_or_url}"
    headers = _mendeley_headers(kind, content_type=(VENDOR_ACCEPT.get(kind, "application/json") if json_body is not None else None))
    if extra_headers:
        headers.update(extra_headers)
    try:
        response = requests.request(
            method,
            url,
            headers=headers,
            params=params,
            json=json_body,
            data=data,
            timeout=REQUEST_TIMEOUT_SECONDS,
            allow_redirects=allow_redirects,
        )
    except requests.RequestException as exc:
        raise BridgeError(502, "mendeley_unreachable", "Não foi possível acessar o Mendeley.", str(exc)) from exc

    if response.status_code not in set(expected):
        details: Any
        try:
            details = response.json()
        except ValueError:
            details = response.text[:1000]
        mapped_status = 401 if response.status_code in {401, 403} else min(response.status_code, 599)
        raise BridgeError(mapped_status, "mendeley_error", "O Mendeley recusou a operação.", details)
    return response


def _json_response(response: requests.Response) -> Any:
    if response.status_code == 204 or not response.content:
        return None
    try:
        return response.json()
    except ValueError as exc:
        raise BridgeError(502, "invalid_mendeley_response", "Resposta inválida recebida do Mendeley.") from exc


def _normalize_doi(value: Any) -> str:
    if not isinstance(value, str):
        return ""
    cleaned = value.strip().lower()
    cleaned = re.sub(r"^https?://(dx\.)?doi\.org/", "", cleaned)
    cleaned = re.sub(r"^doi:\s*", "", cleaned)
    return cleaned.strip()


def _normalize_title(value: Any) -> str:
    if not isinstance(value, str):
        return ""
    value = unicodedata.normalize("NFKD", value)
    value = "".join(ch for ch in value if not unicodedata.combining(ch))
    value = re.sub(r"[^a-zA-Z0-9]+", " ", value.lower())
    return " ".join(value.split())


def _document_identifiers(document: dict[str, Any]) -> dict[str, str]:
    identifiers = document.get("identifiers")
    return identifiers if isinstance(identifiers, dict) else {}


def _clean_document_payload(raw: dict[str, Any], *, update: bool = False) -> dict[str, Any]:
    allowed = DOCUMENT_UPDATE_FIELDS if update else DOCUMENT_ALLOWED_FIELDS
    payload = {key: value for key, value in raw.items() if key in allowed and value is not None}

    title = payload.get("title")
    if not update and (not isinstance(title, str) or len(title.strip()) < 3):
        raise BridgeError(400, "invalid_title", "O título deve ter pelo menos três caracteres.")
    if isinstance(title, str):
        payload["title"] = title.strip()[:1000]

    if not update:
        doc_type = payload.get("type")
        if not isinstance(doc_type, str) or not doc_type.strip():
            raise BridgeError(400, "invalid_type", "Informe o tipo da referência, por exemplo journal, book ou thesis.")
        payload["type"] = doc_type.strip()[:100]

    year = payload.get("year")
    if year is not None:
        try:
            year_int = int(year)
        except (TypeError, ValueError) as exc:
            raise BridgeError(400, "invalid_year", "O ano deve ser numérico.") from exc
        if year_int < 1000 or year_int > datetime.now().year + 2:
            raise BridgeError(400, "invalid_year", "Ano fora do intervalo permitido.")
        payload["year"] = year_int

    for people_key in ("authors", "editors", "translators"):
        people = payload.get(people_key)
        if people is None:
            continue
        if not isinstance(people, list) or len(people) > 200:
            raise BridgeError(400, "invalid_people", f"O campo {people_key} deve ser uma lista.")
        cleaned_people = []
        for person in people:
            if not isinstance(person, dict):
                raise BridgeError(400, "invalid_person", f"Cada item de {people_key} deve ser um objeto.")
            first = str(person.get("first_name", "")).strip()[:200]
            last = str(person.get("last_name", "")).strip()[:200]
            if not first and not last:
                continue
            cleaned_people.append({"first_name": first, "last_name": last})
        payload[people_key] = cleaned_people

    identifiers = payload.get("identifiers")
    if identifiers is not None:
        if not isinstance(identifiers, dict):
            raise BridgeError(400, "invalid_identifiers", "identifiers deve ser um objeto.")
        cleaned_identifiers: dict[str, str] = {}
        for key, value in identifiers.items():
            if not isinstance(key, str) or not isinstance(value, (str, int)):
                continue
            clean_value = str(value).strip()[:500]
            if key.lower() == "doi":
                clean_value = _normalize_doi(clean_value)
            if clean_value:
                cleaned_identifiers[key.lower()] = clean_value
        payload["identifiers"] = cleaned_identifiers

    for list_key in ("keywords", "tags", "websites"):
        values = payload.get(list_key)
        if values is None:
            continue
        if not isinstance(values, list):
            raise BridgeError(400, "invalid_list", f"{list_key} deve ser uma lista.")
        payload[list_key] = [str(v).strip()[:500] for v in values[:200] if str(v).strip()]

    for text_key in (
        "source",
        "abstract",
        "pages",
        "volume",
        "issue",
        "publisher",
        "city",
        "edition",
        "institution",
        "series",
    ):
        if text_key in payload:
            payload[text_key] = str(payload[text_key]).strip()[:20000]

    if update and not payload:
        raise BridgeError(400, "empty_changes", "Nenhuma alteração válida foi informada.")
    return payload

