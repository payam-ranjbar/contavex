"""Docker smoke tests. Opt-in: `uv run pytest -m docker` (needs a Docker daemon).

They build the real image and run it the way compose.yaml does, with a fake
Telegram and a consumer in their own containers. No real credential is used.
"""

from __future__ import annotations

import json
import os
import subprocess
import tarfile
from collections.abc import Iterator
from pathlib import Path

import pytest

pytestmark = pytest.mark.docker

ROOT = Path(__file__).resolve().parents[2]
HERE = Path(__file__).resolve().parent
IMAGE = "content-fleet:test"
CANARY = "CANARY-secret-that-must-never-be-in-an-image-7f3a9c"
BOT_TOKEN = "424242424:DOCKERsmokeDOCKERsmokeDOCKERsmokeDOCKER"
CONSUMER_KEY = "smoke-consumer-key-0123456789abcdef"


def run(*args: str, env: dict[str, str] | None = None, check: bool = True) -> str:
    result = subprocess.run(  # noqa: S603
        args, cwd=ROOT, env=env, capture_output=True, text=True, encoding="utf-8", errors="replace"
    )
    if check and result.returncode != 0:
        raise AssertionError(f"{' '.join(args)}\n{result.stdout}\n{result.stderr}")
    return result.stdout + result.stderr


@pytest.fixture(scope="module")
def image() -> Iterator[str]:
    """A clean build, with a canary secret sitting in the build context."""
    canaries = [ROOT / "secrets" / "canary_token", ROOT / ".env"]
    for path in canaries:
        assert not path.exists(), f"{path} already exists; refusing to overwrite it"
        path.write_text(CANARY, encoding="utf-8")
    try:
        run("docker", "build", "--pull", "--no-cache", "-t", IMAGE, ".")
    finally:
        for path in canaries:
            path.unlink()
    yield IMAGE


class Stack:
    def __init__(self, fixtures: Path) -> None:
        self.env = {
            **os.environ,
            "FIXTURES": fixtures.as_posix(),
            "TESTS_DIR": HERE.as_posix(),
        }
        self.compose = ("docker", "compose", "-f", str(HERE / "compose.test.yaml"))

    def __call__(self, *args: str, check: bool = True) -> str:
        return run(*self.compose, *args, env=self.env, check=check)

    def consumer(self, *args: str) -> str:
        return self("run", "--rm", "--no-deps", "consumer", *args)


@pytest.fixture(scope="module")
def stack(image: str, tmp_path_factory: pytest.TempPathFactory) -> Iterator[Stack]:
    # Under the user's home: Docker Desktop shares that path with its VM.
    fixtures = tmp_path_factory.mktemp("smoke", numbered=True)
    (fixtures / "secrets").mkdir()
    (fixtures / "secrets" / "consumer_api_key_demo").write_text(CONSUMER_KEY + "\n")
    (fixtures / "secrets" / "telegram_news_bot_token").write_text(BOT_TOKEN)
    (fixtures / "fleet.toml").write_text(
        """
[consumers.demo-app]
api_key = "consumer_api_key_demo"

[accounts.news-channel]
platform = "telegram"
implementation = "bot-api"
credentials = { token = "telegram_news_bot_token" }
settings = { chat_id = "-1009876543210", api_base = "http://fake-telegram:8443" }
""",
        encoding="utf-8",
    )
    stack = Stack(fixtures)
    stack("down", "--volumes", "--remove-orphans", check=False)
    try:
        # --wait blocks until the image's HEALTHCHECK reports healthy.
        stack("up", "--detach", "--wait", "--wait-timeout", "90")
        yield stack
    finally:
        print(stack("logs", "--no-color", check=False))
        stack("down", "--volumes", "--remove-orphans", check=False)


class TestImage:
    def test_no_secret_from_the_build_context_is_in_any_layer(
        self, image: str, tmp_path: Path
    ) -> None:
        archive = tmp_path / "image.tar"
        run("docker", "save", image, "-o", str(archive))
        needle = CANARY.encode()
        scanned = 0
        with tarfile.open(archive) as outer:
            for member in outer.getmembers():
                handle = outer.extractfile(member)
                if handle is None:
                    continue
                data = handle.read()
                scanned += len(data)
                assert needle not in data, f"canary found in image file {member.name}"
        assert scanned > 10_000_000  # the layers really were read

    def test_history_and_config_carry_no_credentials(self, image: str) -> None:
        history = run("docker", "image", "history", "--no-trunc", image)
        config = json.loads(run("docker", "image", "inspect", image))[0]["Config"]
        for text in (history, json.dumps(config)):
            assert CANARY not in text
            for word in ("TOKEN", "SECRET", "PASSWORD", "API_KEY"):
                assert word not in text.upper().replace("/RUN/SECRETS", "")
        assert config["User"] == "10001:10001"
        assert config["Healthcheck"]["Test"][:2] == ["CMD", "python"]
        assert config["Entrypoint"] == ["python", "-m", "content_fleet"]

    def test_image_has_no_tests_docs_or_local_files(self, image: str) -> None:
        listing = run(
            "docker", "run", "--rm", "--entrypoint", "python", image, "-c",
            "import os;print(sorted(os.listdir('/app')), os.path.exists('/app/.venv/bin/pytest'))",
        )  # fmt: skip
        assert listing.strip() == "['.venv'] False"

    def test_missing_configuration_fails_fast_with_a_clear_message(self, image: str) -> None:
        result = subprocess.run(  # noqa: S603
            ["docker", "run", "--rm", image],  # noqa: S607
            capture_output=True,
            text=True,
            encoding="utf-8",
        )
        assert result.returncode == 78
        assert "startup_failed" in result.stdout and "config file not found" in result.stdout


class TestRunningService:
    def test_container_is_healthy_and_hardened(self, stack: Stack) -> None:
        state = json.loads(stack("ps", "--format", "json", "publisher").splitlines()[0])
        assert state["Health"] == "healthy"
        assert stack("exec", "-T", "publisher", "id", "-u").strip() == "10001"
        # The root filesystem is read-only; only the data volume is writable.
        denied = stack("exec", "-T", "publisher", "touch", "/app/x", check=False)
        assert "Read-only file system" in denied
        stack("exec", "-T", "publisher", "touch", "/data/writable")

    def test_secrets_are_files_not_environment(self, stack: Stack) -> None:
        container = stack("ps", "--quiet", "publisher").strip()
        inspected = run("docker", "inspect", container)
        assert BOT_TOKEN not in inspected and CONSUMER_KEY not in inspected
        environment = stack("exec", "-T", "publisher", "env")
        assert BOT_TOKEN not in environment and CONSUMER_KEY not in environment
        mounted = stack("exec", "-T", "publisher", "ls", "/run/secrets")
        assert mounted.split() == ["consumer_api_key_demo", "telegram_news_bot_token"]

    def test_no_port_is_published_to_the_host(self, stack: Stack) -> None:
        assert stack("port", "publisher", "8080", check=False).strip() in ("", ":0")

    def test_another_container_publishes_through_the_service(self, stack: Stack) -> None:
        assert stack.consumer("anonymous").strip() == "401"

        result = json.loads(stack.consumer("publish").strip().splitlines()[-1])
        assert result["text"]["status"] == "published"
        assert result["photo"]["status"] == "published"
        assert result["text"]["result"]["posts"][0]["id"].startswith("-1009876543210:")

        vendor = stack("logs", "--no-color", "fake-telegram")
        assert vendor.count("FAKE-TELEGRAM sendMessage") == 1  # the retry was deduplicated
        assert '"text":"hello from a container"' in vendor
        assert "FAKE-TELEGRAM sendPhoto multipart/form-data" in vendor

        service_logs = stack("logs", "--no-color", "publisher")
        assert '"event": "published"' in service_logs
        assert BOT_TOKEN not in service_logs and BOT_TOKEN.split(":")[1] not in service_logs
        assert CONSUMER_KEY not in service_logs

        # State lives on the volume: it survives the container being replaced.
        publication_id = result["text"]["id"]
        stack("up", "--detach", "--wait", "--force-recreate", "publisher")
        after = json.loads(stack.consumer("status", publication_id).strip().splitlines()[-1])
        assert after["status"] == "published"
