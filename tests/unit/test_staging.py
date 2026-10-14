from __future__ import annotations

import hashlib
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx2
import pytest

from contavex.config import ConfigError, ContavexConfig, Settings
from contavex.core.errors import ErrorCategory, PublishError
from contavex.media import MediaStore
from contavex.secrets import Secret
from contavex.services import build_services
from contavex.staging.object_storage import ObjectStorageStager
from contavex.staging.sigv4 import EMPTY_SHA256, encode_key, presign_get, sign
from contavex.store.db import Database
from tests.conftest import FakeClock, write_secrets

pytestmark = pytest.mark.unit

# Example credentials published in the AWS documentation.
ACCESS_KEY = "AKIAIOSFODNN7EXAMPLE"
SECRET_KEY = "wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY"
WHEN = datetime(2013, 5, 24, tzinfo=UTC)


class TestSigV4:
    """Checked against the worked examples in the Amazon S3 API reference,
    "Signature Calculations for the Authorization Header: Transferring
    Payload in a Single Chunk"."""

    def test_get_object_example(self) -> None:
        signed = sign(
            method="GET",
            url="https://examplebucket.s3.amazonaws.com/test.txt",
            headers={"range": "bytes=0-9"},
            payload_sha256=EMPTY_SHA256,
            access_key=ACCESS_KEY,
            secret_key=SECRET_KEY,
            region="us-east-1",
            when=WHEN,
        )
        assert signed["authorization"] == (
            "AWS4-HMAC-SHA256 "
            "Credential=AKIAIOSFODNN7EXAMPLE/20130524/us-east-1/s3/aws4_request, "
            "SignedHeaders=host;range;x-amz-content-sha256;x-amz-date, "
            "Signature=f0e8bdb87c964420e857bd35b5d6ed310bd44f0170aba48dd91039c6036bdb41"
        )
        assert signed["x-amz-date"] == "20130524T000000Z"

    def test_put_object_example(self) -> None:
        body = b"Welcome to Amazon S3."
        signed = sign(
            method="PUT",
            url=f"https://examplebucket.s3.amazonaws.com/{encode_key('test$file.text')}",
            headers={
                "date": "Fri, 24 May 2013 00:00:00 GMT",
                "x-amz-storage-class": "REDUCED_REDUNDANCY",
            },
            payload_sha256=hashlib.sha256(body).hexdigest(),
            access_key=ACCESS_KEY,
            secret_key=SECRET_KEY,
            region="us-east-1",
            when=WHEN,
        )
        assert signed["authorization"].endswith(
            "SignedHeaders=date;host;x-amz-content-sha256;x-amz-date;x-amz-storage-class, "
            "Signature=98ad721746da40c64f1a55b78f14c238d841ea1380cd77a1b5971af0ece108bd"
        )

    def test_presigned_url_example(self) -> None:
        url = presign_get(
            url="https://examplebucket.s3.amazonaws.com/test.txt",
            access_key=ACCESS_KEY,
            secret_key=SECRET_KEY,
            region="us-east-1",
            when=WHEN,
            expires=86400,
        )
        assert url == (
            "https://examplebucket.s3.amazonaws.com/test.txt"
            "?X-Amz-Algorithm=AWS4-HMAC-SHA256"
            "&X-Amz-Credential=AKIAIOSFODNN7EXAMPLE%2F20130524%2Fus-east-1%2Fs3%2Faws4_request"
            "&X-Amz-Date=20130524T000000Z&X-Amz-Expires=86400&X-Amz-SignedHeaders=host"
            "&X-Amz-Signature=aeeed9bbccd4d02ee5c0109b86d86835f995330da4c265957d157751f604d404"
        )

    def test_keys_are_percent_encoded_but_slashes_kept(self) -> None:
        assert encode_key("staged/ab c/é.jpg") == "staged/ab%20c/%C3%A9.jpg"


SETTINGS = {
    "endpoint": "https://s3.example.com",
    "bucket": "contavex-media",
    "region": "auto",
    "public_base_url": "https://cdn.example.com",
}
CREDENTIALS = {"access_key_id": Secret(ACCESS_KEY), "secret_access_key": Secret(SECRET_KEY)}


class Bucket:
    def __init__(self) -> None:
        self.objects: dict[str, tuple[bytes, str]] = {}
        self.requests: list[httpx2.Request] = []
        self.replies: list[httpx2.Response | Exception] = []

    def transport(self) -> httpx2.MockTransport:
        def handle(request: httpx2.Request) -> httpx2.Response:
            self.requests.append(request)
            if self.replies:
                reply = self.replies.pop(0)
                if isinstance(reply, Exception):
                    raise reply
                return reply
            assert request.headers["authorization"].startswith(
                f"AWS4-HMAC-SHA256 Credential={ACCESS_KEY}/"
            )
            key = request.url.path
            if request.method == "PUT":
                digest = hashlib.sha256(request.content).hexdigest()
                assert request.headers["x-amz-content-sha256"] == digest
                self.objects[key] = (request.content, request.headers["content-type"])
                return httpx2.Response(200)
            assert request.method == "DELETE"
            return httpx2.Response(204 if self.objects.pop(key, None) else 404)

        return httpx2.MockTransport(handle)


@pytest.fixture
async def store(tmp_path: Path) -> Any:
    db = Database(tmp_path / "db.sqlite")
    clock = FakeClock()
    media = MediaStore(db, tmp_path / "media", max_bytes=10**6, retention_seconds=3600, clock=clock)

    async def chunks() -> Any:
        yield b"\xff\xd8 picture bytes"

    record = await media.save(
        chunks(), consumer="c", content_type="image/jpeg", filename="my photo.jpg"
    )
    yield media, record, clock
    db.close()


@pytest.mark.anyio
class TestObjectStorageStager:
    async def test_stage_uploads_and_returns_an_unguessable_public_url(self, store: Any) -> None:
        media, record, clock = store
        bucket = Bucket()
        async with httpx2.AsyncClient(transport=bucket.transport()) as http:
            stager = ObjectStorageStager(http, media, clock, SETTINGS, CREDENTIALS)
            url = await stager.stage(record)
            again = await stager.stage(record)

        assert url == again  # staging twice is harmless and stable
        assert url.startswith("https://cdn.example.com/staged/") and url.endswith("/my_photo.jpg")
        assert len(url.split("/")[-2]) == 40 and record.id not in url
        ((content, content_type),) = bucket.objects.values()
        assert content == b"\xff\xd8 picture bytes" and content_type == "image/jpeg"
        assert bucket.requests[0].url.host == "s3.example.com"
        assert bucket.requests[0].url.path.startswith("/contavex-media/staged/")

    async def test_private_bucket_hands_out_an_expiring_presigned_url(self, store: Any) -> None:
        media, record, clock = store
        bucket = Bucket()
        private = {k: v for k, v in SETTINGS.items() if k != "public_base_url"}
        async with httpx2.AsyncClient(transport=bucket.transport()) as http:
            stager = ObjectStorageStager(
                http, media, clock, private | {"url_ttl_seconds": 600}, CREDENTIALS
            )
            url = await stager.stage(record)

        assert url.startswith("https://s3.example.com/contavex-media/staged/")
        assert "X-Amz-Expires=600" in url and "X-Amz-Signature=" in url
        assert url.isascii() and SECRET_KEY not in url
        assert len(bucket.objects) == 1

    async def test_unstage_deletes_and_tolerates_missing_objects(self, store: Any) -> None:
        media, record, clock = store
        bucket = Bucket()
        async with httpx2.AsyncClient(transport=bucket.transport()) as http:
            stager = ObjectStorageStager(http, media, clock, SETTINGS, CREDENTIALS)
            await stager.stage(record)
            await stager.unstage(record.id)
            assert bucket.objects == {}
            await stager.unstage(record.id)  # already gone: 404 is fine
            await stager.unstage("med_never_existed")

    @pytest.mark.parametrize(
        ("reply", "retryable"),
        [
            (httpx2.Response(503), True),
            (httpx2.ConnectError("down"), True),
            (httpx2.Response(403, text="<Error><Code>AccessDenied</Code></Error>"), False),
        ],
    )
    async def test_failures_are_classified(
        self, store: Any, reply: httpx2.Response | Exception, retryable: bool
    ) -> None:
        media, record, clock = store
        bucket = Bucket()
        bucket.replies.append(reply)
        async with httpx2.AsyncClient(transport=bucket.transport()) as http:
            stager = ObjectStorageStager(http, media, clock, SETTINGS, CREDENTIALS)
            with pytest.raises(PublishError) as raised:
                await stager.stage(record)
        assert raised.value.category is ErrorCategory.MEDIA_UNREACHABLE
        assert raised.value.retryable is retryable
        assert SECRET_KEY not in raised.value.message

    @pytest.mark.parametrize(
        "settings",
        [
            {k: v for k, v in SETTINGS.items() if k != "bucket"},
            SETTINGS | {"endpoint": "http://insecure.example.com"},
            SETTINGS | {"typo": 1},
            SETTINGS | {"url_ttl_seconds": 7 * 86400},
        ],
    )
    async def test_bad_settings_are_rejected(self, store: Any, settings: dict[str, Any]) -> None:
        media, _, clock = store
        async with httpx2.AsyncClient() as http:
            with pytest.raises(ValueError, match="staging settings"):
                ObjectStorageStager(http, media, clock, settings, CREDENTIALS)


class TestStagingConfiguration:
    def _build(self, tmp_path: Path, staging: dict[str, Any]) -> None:
        write_secrets(tmp_path / "secrets", consumer_key="k" * 16, s3_id="AKIA", s3_secret="s" * 20)
        config = ContavexConfig.parse(
            {"consumers": {"a": {"api_key": "consumer_key"}}, "staging": staging}
        )
        settings = Settings(data_dir=tmp_path / "data", secrets_dir=tmp_path / "secrets")
        build_services(settings, config=config).db.close()

    def test_object_storage_from_config(self, tmp_path: Path) -> None:
        self._build(
            tmp_path,
            {
                "strategy": "object-storage",
                "settings": SETTINGS,
                "credentials": {"access_key_id": "s3_id", "secret_access_key": "s3_secret"},
            },
        )

    @pytest.mark.parametrize(
        "staging",
        [
            {"strategy": "carrier-pigeon"},
            {"strategy": "self-hosted", "settings": {}},
            {"strategy": "self-hosted", "settings": {"public_base_url": "http://plain.example"}},
            {"strategy": "object-storage", "settings": SETTINGS},
            {"strategy": "self-hosted", "credentials": {"x": "s3_id"}, "settings": {}},
        ],
    )
    def test_misconfigured_staging_stops_startup(
        self, tmp_path: Path, staging: dict[str, Any]
    ) -> None:
        with pytest.raises(ConfigError, match=r"\[staging\]"):
            self._build(tmp_path, staging)
