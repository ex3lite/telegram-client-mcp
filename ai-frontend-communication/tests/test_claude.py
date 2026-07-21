from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path
from uuid import uuid4

import pytest

from dca.claude import ClaudeCode, ClaudeError, RepositorySnapshots, parse_claude_output
from dca.config import Settings
from dca.db import Repository


def run_git(repository: Path, *arguments: str) -> str:
    git = shutil.which("git")
    assert git is not None
    result = subprocess.run(  # noqa: S603 - fixed executable, test-owned arguments
        [git, *arguments],
        cwd=repository,
        check=True,
        capture_output=True,
        text=True,
        env={
            **os.environ,
            "GIT_AUTHOR_NAME": "DCA test",
            "GIT_AUTHOR_EMAIL": "dca@example.invalid",
            "GIT_COMMITTER_NAME": "DCA test",
            "GIT_COMMITTER_EMAIL": "dca@example.invalid",
        },
    )
    return result.stdout.strip()


def test_parse_claude_structured_output() -> None:
    raw = json.dumps(
        {
            "type": "result",
            "structured_output": {
                "answer_markdown": "Confirmed.",
                "citations": [{"path": "src/api.py", "start_line": 10, "end_line": 12}],
                "uncertainty": [],
            },
        }
    ).encode()
    answer = parse_claude_output(raw)
    assert answer.citations[0].path == "src/api.py"


def test_parse_claude_rejects_unstructured_text() -> None:
    with pytest.raises(ClaudeError) as error:
        parse_claude_output(json.dumps({"result": "not-json"}).encode())
    assert error.value.code == "model_provider_invalid_output"


def test_claude_environment_adds_only_configured_proxy(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", "oauth-secret")
    monkeypatch.setenv("DCA_DATABASE_URL", "must-not-leak")
    claude = ClaudeCode(
        Settings(
            database_url="postgresql+psycopg://dca:dca@localhost/dca",
            outbound_proxy_url="http://proxy.example:8080",
        )
    )

    environment = claude._environment(tmp_path)

    assert environment["HTTP_PROXY"] == "http://proxy.example:8080/"
    assert environment["HTTPS_PROXY"] == "http://proxy.example:8080/"
    assert "DCA_DATABASE_URL" not in environment


@pytest.mark.asyncio
async def test_materialize_enforces_allowed_paths_and_policy_cache(tmp_path: Path) -> None:
    working = tmp_path / "working"
    working.mkdir()
    run_git(working, "init", "--initial-branch=main")
    (working / "src").mkdir()
    (working / "src" / "public.py").write_text("VISIBLE = True\n")
    (working / "secret.txt").write_text("must not leak\n")
    run_git(working, "add", ".")
    run_git(working, "commit", "-m", "fixture")
    commit = run_git(working, "rev-parse", "HEAD")

    repository_id = uuid4()
    mirror = tmp_path / "mirrors" / f"{repository_id}.git"
    mirror.parent.mkdir()
    run_git(tmp_path, "clone", "--mirror", str(working), str(mirror))
    repository = Repository(
        id=repository_id,
        project_id=uuid4(),
        name="fixture",
        ssh_url="git@example.invalid:fixture.git",
        allowed_paths=[],
    )
    snapshots = RepositorySnapshots(
        Settings(repository_root=mirror.parent, snapshot_root=tmp_path / "snapshots")
    )

    unrestricted = await snapshots.materialize(repository, commit)
    assert (unrestricted / "secret.txt").is_file()

    repository.allowed_paths = ["src"]
    restricted = await snapshots.materialize(repository, commit)
    assert restricted != unrestricted
    assert (restricted / "src" / "public.py").is_file()
    assert not (restricted / "secret.txt").exists()

    for invalid_path in ("/etc", "../secret.txt", "src/../../secret.txt", "C:/secret.txt"):
        repository.allowed_paths = [invalid_path]
        with pytest.raises(ClaudeError) as error:
            await snapshots.materialize(repository, commit)
        assert error.value.code == "repository_invalid_allowed_path"
