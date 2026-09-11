from __future__ import annotations

import asyncio
import json

import pytest

from subs_pool.cli_client import CLIClient, CLIError


class FakeStream:
    """Fakes readline()/read() for an asyncio subprocess stream."""

    def __init__(self, chunks: list[bytes]) -> None:
        self._chunks = list(chunks)

    async def readline(self) -> bytes:
        if not self._chunks:
            return b""
        return self._chunks.pop(0)

    async def read(self) -> bytes:
        data = b"".join(self._chunks)
        self._chunks = []
        return data


class FakeProcess:
    """Fakes the subset of asyncio.subprocess.Process used by CLIClient."""

    def __init__(
        self,
        stdout_lines: list[bytes] | None = None,
        stderr: bytes = b"",
        returncode: int = 0,
    ) -> None:
        self.stdout = FakeStream(list(stdout_lines or []))
        self.stderr = FakeStream([stderr] if stderr else [])
        self.returncode: int | None = None
        self._final_returncode = returncode
        self.terminated = False
        self.killed = False

    async def communicate(self) -> tuple[bytes, bytes]:
        stdout = await self.stdout.read()
        stderr = await self.stderr.read()
        self.returncode = self._final_returncode
        return stdout, stderr

    async def wait(self) -> int:
        if self.returncode is None:
            self.returncode = self._final_returncode
        return self.returncode

    def terminate(self) -> None:
        self.terminated = True
        self.returncode = 143

    def kill(self) -> None:
        self.killed = True
        self.returncode = 137


def make_spawner(proc: FakeProcess, recorded: list[list[str]] | None = None):
    async def spawn(args):
        if recorded is not None:
            recorded.append(list(args))
        return proc

    return spawn


async def test_accounts_list_parses_json_and_builds_argv():
    recorded: list[list[str]] = []
    proc = FakeProcess(stdout_lines=[json.dumps({"accounts": []}).encode() + b"\n"])
    client = CLIClient("codex", spawn=make_spawner(proc, recorded))

    result = await client.accounts_list()

    assert result == {"accounts": []}
    assert recorded == [["codex", "account", "list"]]


async def test_accounts_import_builds_argv_with_explicit_path_and_weight():
    recorded: list[list[str]] = []
    proc = FakeProcess(stdout_lines=[json.dumps({"ref": "a", "enabled": True}).encode()])
    client = CLIClient("codex", spawn=make_spawner(proc, recorded))

    result = await client.accounts_import("a", "/tmp/auth.json", weight=3)

    assert result["ref"] == "a"
    assert recorded == [["codex", "account", "import", "a", "--path", "/tmp/auth.json", "--weight", "3"]]


async def test_pool_enable_disable_weight_argv():
    for coro_name, args, expected in [
        ("pool_enable", ("a",), ["account", "enable", "a"]),
        ("pool_disable", ("a",), ["account", "disable", "a"]),
        ("pool_weight", ("a", 5), ["account", "weight", "a", "5"]),
    ]:
        recorded: list[list[str]] = []
        proc = FakeProcess(stdout_lines=[json.dumps({"ref": "a"}).encode()])
        client = CLIClient("codex", spawn=make_spawner(proc, recorded))
        await getattr(client, coro_name)(*args)
        assert recorded == [["codex", *expected]]


async def test_status_and_quota_argv():
    proc = FakeProcess(stdout_lines=[json.dumps({"accounts": [], "eligible_count": 0}).encode()])
    recorded: list[list[str]] = []
    client = CLIClient("codex", spawn=make_spawner(proc, recorded))
    await client.status()
    assert recorded == [["codex", "status"]]

    proc2 = FakeProcess(stdout_lines=[json.dumps({"accounts": []}).encode()])
    recorded2: list[list[str]] = []
    client2 = CLIClient("codex", spawn=make_spawner(proc2, recorded2))
    await client2.quota()
    assert recorded2 == [["codex", "quota"]]


async def test_nonzero_exit_with_json_error_raises_cli_error():
    proc = FakeProcess(stderr=json.dumps({"error": {"message": "unknown account ref: x"}}).encode(), returncode=1)
    client = CLIClient("codex", spawn=make_spawner(proc))

    with pytest.raises(CLIError) as excinfo:
        await client.accounts_list()

    assert excinfo.value.message == "unknown account ref: x"
    assert excinfo.value.returncode == 1


async def test_nonzero_exit_with_non_json_stderr_shows_bounded_text():
    proc = FakeProcess(stderr=b"Traceback: boom", returncode=1)
    client = CLIClient("codex", spawn=make_spawner(proc))

    with pytest.raises(CLIError) as excinfo:
        await client.status()

    assert "Traceback: boom" in excinfo.value.message


async def test_malformed_stdout_raises_cli_error():
    proc = FakeProcess(stdout_lines=[b"not json"])
    client = CLIClient("codex", spawn=make_spawner(proc))

    with pytest.raises(CLIError):
        await client.accounts_list()


async def test_missing_cli_executable_raises_clear_cli_error():
    async def spawn(args):
        raise FileNotFoundError("no such file")

    client = CLIClient("codex", spawn=spawn)

    with pytest.raises(CLIError) as excinfo:
        await client.status()

    assert "not found" in excinfo.value.message


async def test_login_streams_authorization_then_completed_events():
    lines = [
        json.dumps({
            "event": "authorization_required",
            "verification_uri": "https://example.com/device",
            "user_code": "ABCD-EFGH",
            "expires_in": 900,
            "interval": 5,
        }).encode() + b"\n",
        json.dumps({"event": "completed", "account": {"ref": "a", "enabled": True}}).encode() + b"\n",
    ]
    recorded: list[list[str]] = []
    proc = FakeProcess(stdout_lines=lines, returncode=0)
    client = CLIClient("codex", spawn=make_spawner(proc, recorded))

    events = [event async for event in client.login("a")]

    assert [event["event"] for event in events] == ["authorization_required", "completed"]
    assert events[0]["verification_uri"] == "https://example.com/device"
    assert recorded == [["codex", "account", "login", "a", "--device", "--events-jsonl"]]


async def test_login_nonzero_exit_raises_cli_error_with_stderr_message():
    proc = FakeProcess(stderr=json.dumps({"error": "device flow expired"}).encode(), returncode=1)
    client = CLIClient("codex", spawn=make_spawner(proc))

    with pytest.raises(CLIError) as excinfo:
        async for _ in client.login("a"):
            pass

    assert excinfo.value.message == "device flow expired"


async def test_login_malformed_jsonl_line_raises_cli_error():
    proc = FakeProcess(stdout_lines=[b"not json\n"])
    client = CLIClient("codex", spawn=make_spawner(proc))

    with pytest.raises(CLIError):
        async for _ in client.login("a"):
            pass


async def test_login_cancel_terminates_still_running_process():
    line = json.dumps({
        "event": "authorization_required",
        "verification_uri": "https://example.com/device",
        "user_code": "ABCD-EFGH",
        "expires_in": 900,
        "interval": 5,
    }).encode() + b"\n"
    proc = FakeProcess(stdout_lines=[line])
    client = CLIClient("codex", spawn=make_spawner(proc))
    stream = client.login("a")

    first = await stream.__anext__()
    assert first["event"] == "authorization_required"
    assert proc.returncode is None

    await stream.cancel()

    assert proc.terminated is True
    with pytest.raises(StopAsyncIteration):
        await stream.__anext__()


async def test_login_cancel_on_already_exited_process_is_a_noop():
    proc = FakeProcess(stdout_lines=[json.dumps({"event": "completed", "account": {}}).encode() + b"\n"], returncode=0)
    client = CLIClient("codex", spawn=make_spawner(proc))
    stream = client.login("a")

    async for _ in stream:
        pass
    await stream.cancel()
    assert proc.terminated is False


async def test_login_completed_event_then_nonzero_exit_raises_cli_error():
    lines = [json.dumps({"event": "completed", "account": {"ref": "a"}}).encode() + b"\n"]
    proc = FakeProcess(
        stdout_lines=lines,
        stderr=json.dumps({"error": "provider revoked session after completion"}).encode(),
        returncode=1,
    )
    client = CLIClient("codex", spawn=make_spawner(proc))

    with pytest.raises(CLIError) as excinfo:
        async for _ in client.login("a"):
            pass

    assert excinfo.value.message == "provider revoked session after completion"
    assert excinfo.value.returncode == 1


async def test_login_malformed_jsonl_terminates_still_running_process():
    proc = FakeProcess(stdout_lines=[b"not json\n"])
    client = CLIClient("codex", spawn=make_spawner(proc))
    stream = client.login("a")

    with pytest.raises(CLIError):
        await stream.__anext__()

    assert proc.terminated is True
    await stream.cancel()
    assert proc.killed is False


async def test_login_cancel_while_spawn_pending_terminates_new_process_without_leak():
    proc = FakeProcess(stdout_lines=[])
    release = asyncio.Event()

    async def spawn(args):
        await release.wait()
        return proc

    client = CLIClient("codex", spawn=spawn)
    stream = client.login("a")
    next_task = asyncio.ensure_future(stream.__anext__())
    await asyncio.sleep(0)

    await stream.cancel()
    release.set()

    with pytest.raises(StopAsyncIteration):
        await next_task
    assert proc.terminated is True


async def test_login_malformed_jsonl_does_not_leak_line_contents_in_error():
    secret_line = b'{"token": "sk-super-secret-value", not-json\n'
    proc = FakeProcess(stdout_lines=[secret_line])
    client = CLIClient("codex", spawn=make_spawner(proc))

    with pytest.raises(CLIError) as excinfo:
        async for _ in client.login("a"):
            pass

    assert "sk-super-secret-value" not in excinfo.value.message
    assert "sk-super-secret-value" not in str(excinfo.value)


async def test_run_json_cancelled_terminates_owned_process():
    proc = FakeProcess()
    hang = asyncio.Event()

    async def hanging_communicate():
        await hang.wait()
        return b"{}", b""

    proc.communicate = hanging_communicate  # type: ignore[method-assign]
    client = CLIClient("codex", spawn=make_spawner(proc))

    task = asyncio.ensure_future(client.status())
    await asyncio.sleep(0)
    task.cancel()

    with pytest.raises(asyncio.CancelledError):
        await task
    assert proc.terminated is True


async def test_run_json_timeout_terminates_process_and_raises_cli_error():
    proc = FakeProcess()
    hang = asyncio.Event()

    async def hanging_communicate():
        await hang.wait()
        return b"{}", b""

    proc.communicate = hanging_communicate  # type: ignore[method-assign]
    client = CLIClient("codex", spawn=make_spawner(proc), timeout=0.01)

    with pytest.raises(CLIError) as excinfo:
        await client.status()

    assert "timed out" in excinfo.value.message
    assert proc.terminated is True


async def test_malformed_stdout_does_not_leak_payload_contents_in_error():
    secret_stdout = b'{"password": "hunter2-should-not-leak" not-json'
    proc = FakeProcess(stdout_lines=[secret_stdout])
    client = CLIClient("codex", spawn=make_spawner(proc))

    with pytest.raises(CLIError) as excinfo:
        await client.accounts_list()

    assert "hunter2-should-not-leak" not in excinfo.value.message


async def test_nonzero_exit_stderr_fallback_is_clipped_when_very_long():
    proc = FakeProcess(stderr=b"x" * 5000, returncode=1)
    client = CLIClient("codex", spawn=make_spawner(proc))

    with pytest.raises(CLIError) as excinfo:
        await client.status()

    assert len(excinfo.value.message) < 3000


async def test_structured_malformed_stderr_does_not_forward_nested_secret():
    proc = FakeProcess(
        stderr=json.dumps({"error": {"message": "sk-embedded-secret-value"}}).encode(),
        returncode=1,
    )
    client = CLIClient("codex", spawn=make_spawner(proc))
    with pytest.raises(CLIError) as excinfo:
        await client.status()
    assert "sk-embedded-secret-value" not in excinfo.value.message


async def test_login_event_projection_does_not_forward_secret_fields():
    lines = [
        json.dumps({
            "event": "completed",
            "account": {
                "ref": "a",
                "enabled": True,
                "weight": 1,
                "auth_present": True,
                "quota": "unknown",
                "access_token": "sk-never-forward",
                "refresh_token": "rt-never-forward",
            },
        }).encode() + b"\n"
    ]
    proc = FakeProcess(stdout_lines=lines)
    client = CLIClient("codex", spawn=make_spawner(proc))
    events = [event async for event in client.login("a")]
    dumped = json.dumps(events)
    assert "sk-never-forward" not in dumped
    assert "rt-never-forward" not in dumped
