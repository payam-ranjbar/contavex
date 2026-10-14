"""The consumer-facing HTTP API (v1)."""

import hashlib
import hmac
import json
import re
import time
import uuid
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from typing import Annotated, Any, Literal

import structlog
from fastapi import Depends, FastAPI, Header, Query, Request, Response
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from prometheus_client import CONTENT_TYPE_LATEST, generate_latest
from pydantic import BaseModel, Field
from starlette.exceptions import HTTPException as StarletteHTTPException

from contavex import __version__
from contavex.config import AccountConfig
from contavex.core.capabilities import Capabilities, UploadedMedia, check_request
from contavex.core.errors import ApiProblem, ErrorCategory, PublishError
from contavex.core.models import (
    ErrorInfo,
    Media,
    Publication,
    PublicationEdit,
    PublicationRequest,
    PublicationResult,
    PublicationStatus,
    PublishedPost,
    Resolution,
)
from contavex.observability import log
from contavex.providers.base import Provider
from contavex.secrets import redactor
from contavex.services import Services
from contavex.store.db import DuplicateIdempotencyKey, MediaRecord, PublicationRecord

PROBLEM_JSON = "application/problem+json"
EXPIRY_WARNING_SECONDS = 10 * 86400
_REQUEST_ID = re.compile(r"^[A-Za-z0-9._-]{1,64}$")
_TRACEPARENT = re.compile(r"^[0-9a-f]{2}-([0-9a-f]{32})-[0-9a-f]{16}-[0-9a-f]{2}$")
_IDEMPOTENCY_KEY = re.compile(r"^[\x21-\x7e]{1,255}$")


class Problem(BaseModel):
    """RFC 9457 problem details. Extra members depend on `type`."""

    type: str
    title: str
    status: int
    detail: str | None = None
    request_id: str | None = None


class Account(BaseModel):
    id: str
    platform: str
    capabilities: Capabilities
    credential_status: Literal["ok", "expiring", "reauthorization_required"] = Field(
        description=(
            "`expiring`: the credential lapses within 10 days and could not be renewed "
            "automatically yet. `reauthorization_required`: the platform rejected it or it "
            "has lapsed; the operator must supply a new one."
        )
    )
    credential_expires_at: datetime | None = None


def _ts(value: float) -> datetime:
    return datetime.fromtimestamp(value, UTC)


def _problem(
    status: int, code: str, title: str, detail: str | None = None, **extras: Any
) -> JSONResponse:
    body: dict[str, Any] = {
        "type": f"urn:contavex:problem:{code}",
        "title": title,
        "status": status,
    }
    if detail:
        body["detail"] = detail
    request_id = structlog.contextvars.get_contextvars().get("request_id")
    if request_id:
        body["request_id"] = request_id
    body.update(extras)
    return JSONResponse(body, status_code=status, media_type=PROBLEM_JSON)


def _publication_view(record: PublicationRecord) -> Publication:
    return Publication(
        id=record.id,
        account=record.account,
        platform=record.platform,
        status=record.status,
        client_reference=record.request.client_reference,
        attempts=record.attempts,
        next_attempt_at=_ts(record.run_after) if record.run_after is not None else None,
        result=record.result,
        error=record.error,
        created_at=_ts(record.created_at),
        updated_at=_ts(record.updated_at),
    )


def _media_view(record: MediaRecord) -> Media:
    return Media(
        id=record.id,
        content_type=record.content_type,
        size=record.size,
        sha256=record.sha256,
        filename=record.filename,
        created_at=_ts(record.created_at),
        expires_at=_ts(record.expires_at),
    )


def _fingerprint(request: PublicationRequest) -> str:
    canonical = json.dumps(request.model_dump(mode="json"), sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode()).hexdigest()


def create_app(services: Services, *, start_worker: bool = True) -> FastAPI:
    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        if start_worker:
            services.worker.start()
        log.info("service_started", version=__version__, accounts=len(services.config.accounts))
        try:
            yield
        finally:
            await services.aclose()
            log.info("service_stopped")

    app = FastAPI(
        title="Contavex",
        version=__version__,
        summary="Publish content to social platforms through one contract.",
        lifespan=lifespan,
        docs_url=None,
        redoc_url=None,
    )
    bearer = HTTPBearer(auto_error=False, description="A consumer API key.")

    # ------------------------------------------------------------ plumbing

    @app.middleware("http")
    async def correlate(
        request: Request, call_next: Callable[[Request], Awaitable[Response]]
    ) -> Response:
        supplied = request.headers.get("x-request-id", "")
        request_id = supplied if _REQUEST_ID.fullmatch(supplied) else uuid.uuid4().hex
        context: dict[str, str] = {"request_id": request_id}
        trace = _TRACEPARENT.fullmatch(request.headers.get("traceparent", ""))
        if trace:
            context["trace_id"] = trace.group(1)
        started = time.perf_counter()
        with structlog.contextvars.bound_contextvars(**context):
            response = await call_next(request)
            if request.url.path not in ("/livez", "/readyz"):
                log.info(
                    "http_request",
                    method=request.method,
                    path=request.url.path,
                    status=response.status_code,
                    duration_ms=round((time.perf_counter() - started) * 1000, 1),
                )
        response.headers["x-request-id"] = request_id
        return response

    @app.exception_handler(ApiProblem)
    async def on_problem(_: Request, exc: ApiProblem) -> JSONResponse:
        return _problem(exc.status, exc.code, exc.title, exc.detail, **exc.extras)

    @app.exception_handler(RequestValidationError)
    async def on_invalid(_: Request, exc: RequestValidationError) -> JSONResponse:
        # Field locations and messages only; never echo the submitted values.
        errors = [
            {"field": ".".join(str(part) for part in err["loc"]), "message": err["msg"]}
            for err in exc.errors()
        ]
        return _problem(
            422,
            "invalid-request",
            "The request is not valid",
            f"{len(errors)} validation error(s)",
            errors=errors,
        )

    @app.exception_handler(StarletteHTTPException)
    async def on_http(_: Request, exc: StarletteHTTPException) -> JSONResponse:
        return _problem(exc.status_code, "http-error", str(exc.detail))

    @app.exception_handler(Exception)
    async def on_crash(_: Request, exc: Exception) -> JSONResponse:
        log.error("unhandled_error", exc_info=exc)
        return _problem(500, "internal", "Internal error")

    def consumer(
        credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(bearer)],
    ) -> str:
        if credentials is not None:
            presented = credentials.credentials.encode()
            matched: str | None = None
            # Compare against every key so timing does not reveal which matched.
            for name, key in services.consumer_keys.items():
                if hmac.compare_digest(presented, key.reveal().encode()):
                    matched = name
            if matched is not None:
                structlog.contextvars.bind_contextvars(consumer=matched)
                return matched
        raise ApiProblem(
            401,
            "unauthenticated",
            "A valid consumer API key is required",
            "Send 'Authorization: Bearer <key>'.",
        )

    Consumer = Annotated[str, Depends(consumer)]

    def resolve(account_id: str) -> tuple[AccountConfig, Provider]:
        account = services.config.accounts.get(account_id)
        if account is None:
            raise ApiProblem(
                422,
                "unknown-account",
                "The account is not configured",
                f"No account {account_id!r}; see GET /v1/accounts.",
            )
        provider = services.registry.get(account.platform, account.implementation)
        assert provider is not None  # checked at startup
        return account, provider

    def owned(publication_id: str, who: str) -> PublicationRecord:
        record = services.db.get_publication(publication_id)
        # Another consumer's publication is indistinguishable from a missing one.
        if record is None or record.consumer != who:
            raise ApiProblem(404, "not-found", "No such publication")
        return record

    problems: dict[int | str, dict[str, Any]] = {
        "4XX": {"model": Problem, "content": {PROBLEM_JSON: {}}},
    }

    # ------------------------------------------------------------ publications

    @app.post(
        "/v1/publications",
        status_code=202,
        response_model=Publication,
        responses=problems,
        tags=["publications"],
        summary="Submit content for publishing",
    )
    async def submit(
        body: PublicationRequest,
        response: Response,
        who: Consumer,
        idempotency_key: Annotated[
            str | None,
            Header(
                description=(
                    "Required. A unique value per logical publication, e.g. a UUID. "
                    "Repeating a request with the same key returns the original "
                    "publication instead of creating another."
                )
            ),
        ] = None,
    ) -> Publication:
        """Always asynchronous: the response says the request was accepted,
        not that it was published. Poll the returned resource for the outcome."""
        key = (idempotency_key or "").strip().strip('"')
        if not _IDEMPOTENCY_KEY.fullmatch(key):
            raise ApiProblem(
                400,
                "idempotency-key-required",
                "An Idempotency-Key header is required",
                "Send a unique key (1-255 visible ASCII characters) per logical publication.",
            )
        fingerprint = _fingerprint(body)

        def replay() -> Publication | None:
            existing = services.db.get_by_idempotency_key(who, key)
            if existing is None:
                return None
            if existing.fingerprint != fingerprint:
                raise ApiProblem(
                    422,
                    "idempotency-key-reused",
                    "This Idempotency-Key was already used with a different request",
                    publication_id=existing.id,
                )
            response.headers["location"] = f"/v1/publications/{existing.id}"
            response.headers["idempotent-replay"] = "true"
            return _publication_view(existing)

        # A replay answers before validation: the original was valid when it
        # was accepted, even if an upload has since expired.
        if (replayed := replay()) is not None:
            return replayed

        account, provider = resolve(body.account)
        if account.id in services.worker.rejected_credentials:
            # Known to fail; say so now instead of accepting work that cannot succeed.
            raise ApiProblem(
                409,
                "account-unavailable",
                "The platform rejected this account's credential",
                "The operator must supply a new credential and restart the service.",
            )
        uploads: dict[str, UploadedMedia] = {}
        for item in body.media:
            if item.media_id is None:
                continue
            media = services.media.get(item.media_id, consumer=who)
            if media is None:
                raise ApiProblem(
                    422,
                    "unknown-media",
                    "A referenced media item does not exist",
                    f"No media {item.media_id!r}; it may have expired. Upload it again.",
                )
            uploads[media.id] = UploadedMedia(
                id=media.id, content_type=media.content_type, size=media.size
            )
        check_request(provider.capabilities(account), body, uploads)
        try:
            provider.validate(account, body)
        except PublishError as exc:
            raise ApiProblem(
                422,
                "unsupported-capability"
                if exc.category is ErrorCategory.UNSUPPORTED
                else "invalid-content",
                "The target account cannot publish this request",
                exc.message,
            ) from None

        now = services.clock()
        deadline = now + services.settings.publication_deadline_seconds
        try:
            record = services.db.insert_publication(
                id=f"pub_{uuid.uuid7().hex}",
                consumer=who,
                idempotency_key=key,
                fingerprint=fingerprint,
                account=account.id,
                platform=account.platform,
                implementation=account.implementation,
                request=body,
                now=now,
                deadline=deadline,
            )
        except DuplicateIdempotencyKey:
            # Lost a race with an identical concurrent request.
            replayed = replay()
            assert replayed is not None
            return replayed
        services.media.keep_until(list(uploads), deadline)
        services.metrics.accepted.labels(account.platform).inc()
        services.worker.wake()
        log.info("publication_accepted", publication_id=record.id, account=account.id)
        response.headers["location"] = f"/v1/publications/{record.id}"
        return _publication_view(record)

    @app.get(
        "/v1/publications/{publication_id}",
        response_model=Publication,
        responses=problems,
        tags=["publications"],
        summary="Get the state of a publication",
    )
    async def get_publication(
        publication_id: str, response: Response, who: Consumer
    ) -> Publication:
        record = owned(publication_id, who)
        if not record.status.terminal:
            wait = (record.run_after or services.clock()) - services.clock()
            response.headers["retry-after"] = str(max(1, min(int(wait) + 1, 60)))
        return _publication_view(record)

    @app.post(
        "/v1/publications/{publication_id}/resolution",
        response_model=Publication,
        responses=problems,
        tags=["publications"],
        summary="Record how an outcome_unknown publication turned out",
    )
    async def resolve_unknown(publication_id: str, body: Resolution, who: Consumer) -> Publication:
        """After checking the platform by hand: mark the publication published,
        or not published (which makes it a failed, retryable publication)."""
        record = owned(publication_id, who)
        if record.status is not PublicationStatus.OUTCOME_UNKNOWN:
            raise ApiProblem(
                409,
                "not-unknown",
                "Only an outcome_unknown publication can be resolved",
                f"This publication is {record.status.value}.",
            )
        now = services.clock()
        if body.published:
            posts = [PublishedPost(id=body.post_id, url=body.post_url)] if body.post_id else []
            services.db.finish(
                record.id,
                now=now,
                status=PublicationStatus.PUBLISHED,
                result=PublicationResult(posts=posts),
            )
        else:
            services.db.finish(
                record.id,
                now=now,
                status=PublicationStatus.FAILED,
                error=ErrorInfo(
                    category=ErrorCategory.TIMEOUT,
                    retryable=True,
                    message="confirmed not published; submit again with a new Idempotency-Key",
                ),
            )
        log.info("publication_resolved", publication_id=record.id, published=body.published)
        return _publication_view(owned(publication_id, who))

    # Edit and delete are a single provider call on every platform that has
    # them, and both converge when repeated, so they run inside the request.

    def published(
        publication_id: str, who: str
    ) -> tuple[PublicationRecord, AccountConfig, Provider]:
        record = owned(publication_id, who)
        if record.status is not PublicationStatus.PUBLISHED or record.result is None:
            raise ApiProblem(
                409,
                "not-published",
                "Only a published publication can be changed",
                f"This publication is {record.status.value}.",
            )
        account, provider = resolve(record.account)
        return record, account, provider

    def provider_problem(exc: PublishError) -> ApiProblem:
        message = redactor.scrub(exc.message)
        if exc.category in (ErrorCategory.UNSUPPORTED, ErrorCategory.INVALID_REQUEST):
            code = (
                "unsupported-capability"
                if exc.category is ErrorCategory.UNSUPPORTED
                else "invalid-content"
            )
            return ApiProblem(422, code, "The platform cannot apply this change", message)
        extras: dict[str, Any] = {"category": exc.category.value, "retryable": exc.retryable}
        if exc.provider_code:
            extras["provider_code"] = exc.provider_code
        if exc.retry_after is not None:
            extras["retry_after"] = exc.retry_after
        # 503 when trying again can work, 502 when the platform refused.
        return ApiProblem(
            503 if exc.retryable else 502,
            "provider-error",
            "The platform did not apply the change",
            message,
            **extras,
        )

    @app.patch(
        "/v1/publications/{publication_id}",
        response_model=Publication,
        responses=problems,
        tags=["publications"],
        summary="Edit a published post",
    )
    async def edit_publication(
        publication_id: str, body: PublicationEdit, who: Consumer
    ) -> Publication:
        """Available when the account's capabilities list the field under `editable`."""
        record, account, provider = published(publication_id, who)
        caps = provider.capabilities(account)
        if "text" not in caps.editable:
            raise ApiProblem(
                422,
                "unsupported-capability",
                "The target account does not support this request",
                "Published posts on this account cannot be edited.",
                capability="edit:text",
            )
        text_format = body.text_format or record.request.text_format
        limit = caps.max_caption_length if record.request.media else caps.max_text_length
        if text_format not in caps.text_formats:
            raise ApiProblem(
                422,
                "unsupported-capability",
                "The target account does not support this request",
                f"text_format {text_format!r} is not supported; supported: {caps.text_formats}",
                capability=f"text_format:{text_format}",
            )
        if limit is not None and len(body.text) > limit:
            raise ApiProblem(
                422,
                "unsupported-capability",
                "The target account does not support this request",
                f"text is {len(body.text)} characters; the limit is {limit}",
                capability="text_length",
            )
        assert record.result is not None
        try:
            ctx = services.worker.context_for(account, record.id)
            await provider.edit(
                ctx, record.request, record.result, {"text": body.text, "text_format": text_format}
            )
        except PublishError as exc:
            raise provider_problem(exc) from None
        log.info("publication_edited", publication_id=record.id)
        return _publication_view(owned(publication_id, who))

    @app.delete(
        "/v1/publications/{publication_id}",
        response_model=Publication,
        responses=problems,
        tags=["publications"],
        summary="Delete a published post from the platform",
    )
    async def delete_publication(publication_id: str, who: Consumer) -> Publication:
        """Available when the account's capabilities say `deletable`. The
        publication record is kept, with status `deleted`."""
        record = owned(publication_id, who)
        if record.status is PublicationStatus.DELETED:
            return _publication_view(record)  # repeating a delete is not an error
        record, account, provider = published(publication_id, who)
        if not provider.capabilities(account).deletable:
            raise ApiProblem(
                422,
                "unsupported-capability",
                "The target account does not support this request",
                "Published posts on this account cannot be deleted through the API.",
                capability="delete",
            )
        assert record.result is not None
        try:
            ctx = services.worker.context_for(account, record.id)
            await provider.delete(ctx, record.result)
        except PublishError as exc:
            raise provider_problem(exc) from None
        services.db.finish(
            record.id,
            now=services.clock(),
            status=PublicationStatus.DELETED,
            result=record.result,
        )
        log.info("publication_deleted", publication_id=record.id)
        return _publication_view(owned(publication_id, who))

    # ------------------------------------------------------------ media

    @app.post(
        "/v1/media",
        status_code=201,
        response_model=Media,
        responses=problems,
        tags=["media"],
        summary="Upload a media file",
        openapi_extra={
            "requestBody": {
                "required": True,
                "description": "The raw file bytes. Set Content-Type to the file's MIME type.",
                "content": {"*/*": {"schema": {"type": "string", "format": "binary"}}},
            }
        },
    )
    async def upload_media(
        request: Request,
        who: Consumer,
        filename: Annotated[
            str | None,
            Query(max_length=200, description="Display name passed on to platforms that show one."),
        ] = None,
    ) -> Media:
        """The body is the file itself, not a multipart form."""
        declared = request.headers.get("content-length", "")
        if declared.isdigit() and int(declared) > services.settings.max_upload_bytes:
            raise ApiProblem(
                413,
                "media-too-large",
                "The upload exceeds the size limit",
                f"The limit is {services.settings.max_upload_bytes} bytes.",
            )
        if filename is not None:
            # A name for display only; it never becomes a path here.
            filename = re.sub(r"[\x00-\x1f/\\]", "_", filename).strip() or None
        record = await services.media.save(
            request.stream(),
            consumer=who,
            content_type=request.headers.get("content-type", ""),
            filename=filename,
        )
        log.info("media_uploaded", media_id=record.id, size=record.size)
        return _media_view(record)

    @app.get(
        "/v1/media/{media_id}",
        response_model=Media,
        responses=problems,
        tags=["media"],
        summary="Get media metadata",
    )
    async def get_media(media_id: str, who: Consumer) -> Media:
        record = services.media.get(media_id, consumer=who)
        if record is None:
            raise ApiProblem(404, "not-found", "No such media")
        return _media_view(record)

    # ------------------------------------------------------------ accounts

    def credential_health(
        account: AccountConfig,
    ) -> tuple[Literal["ok", "expiring", "reauthorization_required"], datetime | None]:
        if account.id in services.worker.rejected_credentials:
            return "reauthorization_required", None
        expiries = [
            state.expires_at
            for state in services.credentials.states(account).values()
            if state.expires_at is not None
        ]
        if not expiries:
            return "ok", None
        left = min(expiries) - services.clock()
        if left <= 0:
            return "reauthorization_required", _ts(min(expiries))
        return ("expiring" if left < EXPIRY_WARNING_SECONDS else "ok"), _ts(min(expiries))

    def account_view(account: AccountConfig) -> Account:
        _, provider = resolve(account.id)
        status, expires_at = credential_health(account)
        return Account(
            id=account.id,
            platform=account.platform,
            capabilities=provider.capabilities(account),
            credential_status=status,
            credential_expires_at=expires_at,
        )

    @app.get(
        "/v1/accounts",
        response_model=list[Account],
        responses=problems,
        tags=["accounts"],
        summary="List accounts and what each can publish",
    )
    async def list_accounts(who: Consumer) -> list[Account]:
        return [account_view(account) for account in services.config.accounts.values()]

    @app.get(
        "/v1/accounts/{account_id}",
        response_model=Account,
        responses=problems,
        tags=["accounts"],
        summary="Get one account's capabilities",
    )
    async def get_account(account_id: str, who: Consumer) -> Account:
        account = services.config.accounts.get(account_id)
        if account is None:
            raise ApiProblem(404, "not-found", "No such account")
        return account_view(account)

    # ------------------------------------------------------------ operations

    @app.get("/livez", include_in_schema=False)
    async def livez() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/readyz", include_in_schema=False)
    async def readyz() -> JSONResponse:
        # Local dependencies only. A vendor outage must not make this service
        # look down to the things that depend on it.
        checks = {
            "database": services.db.ping(),
            "worker": services.worker.running or not start_worker,
        }
        ready = all(checks.values())
        return JSONResponse(
            {"status": "ready" if ready else "not_ready", "checks": checks},
            status_code=200 if ready else 503,
        )

    @app.get("/metrics", include_in_schema=False)
    async def metrics() -> Response:
        return Response(generate_latest(services.metrics.registry), media_type=CONTENT_TYPE_LATEST)

    return app
