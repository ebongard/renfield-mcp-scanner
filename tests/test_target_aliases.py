"""A target is reached by the name people SAY, kept in the private registry.

The spoken names used to live in the Renfield tool hint, i.e. in a public repo,
next to the target ids. They now live in `targets.yaml` (`aliases`), and the
scanner resolves them itself. A declared target is settled BEFORE the feeder
runs: before this, an unknown name only surfaced in the completion event.
"""
from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

from renfield_mcp_scanner import server
from renfield_mcp_scanner import tools as t
from renfield_mcp_scanner.config import Config, ScanTarget, TargetsFile
from renfield_mcp_scanner.router import route

REPO = Path(__file__).resolve().parent.parent


def _t(tid, label, aliases=()):
    return ScanTarget(id=tid, label=label, base_url="http://b:8000",
                      token_env=f"TOK_{tid.upper()}", scan_profile_id="d",
                      aliases=list(aliases))


def _cfg():
    return Config(targets=[
        _t("home", "Home", ["Daheim"]),
        _t("office", "Office instance", []),
        _t("granny", "Granny", ["Oma", "Omas Renfield"]),
    ], staging_dir="/tmp/x")


@pytest.mark.parametrize("spoken, expected", [
    ("home", "home"),                 # exact id, as every caller passes today
    ("Daheim", "home"),               # an alias
    ("daheim", "home"),               # case
    ("Office-Instance", "office"),    # the label, with any spacing
    ("Oma", "granny"),
    ("omas renfield", "granny"),      # the alias as said, any case
    ("Omas", "granny"),               # possessive
    ("Granny's Renfield", "granny"),  # apostrophe + trailing "Renfield"
])
def test_spoken_names_resolve(spoken, expected):
    target, why = _cfg().resolve_target(spoken)
    assert target is not None and target.id == expected, why


def test_an_unknown_name_is_refused_with_every_known_name():
    target, why = _cfg().resolve_target("Nachbar")
    assert target is None
    for name in ("home", "Daheim", "office", "Office instance", "granny", "Oma"):
        assert name in why


def test_two_targets_answering_to_one_name_is_refused_not_guessed():
    cfg = Config(targets=[_t("a", "A", ["Mutter"]), _t("b", "B", ["mutter"])],
                 staging_dir="/tmp/x")
    target, why = cfg.resolve_target("Mutter")
    assert target is None and "ambiguous" in why


def test_an_exact_id_wins_over_an_alias_of_another_target():
    cfg = Config(targets=[_t("oma", "Oma", []), _t("granny", "Granny", ["oma"])],
                 staging_dir="/tmp/x")
    assert cfg.resolve_target("oma")[0].id == "oma"


def test_a_registry_without_aliases_still_loads():
    """The rollback direction: old files carry no `aliases`; old code ignores
    the new key (pydantic's default `extra="ignore"`)."""
    old = TargetsFile(targets=[{"id": "a", "label": "A", "base_url": "http://b",
                                "token_env": "T", "scan_profile_id": "d"}])
    assert old.targets[0].aliases == []


def test_the_router_resolves_a_spoken_target():
    r = route(_cfg(), declared="Omas Renfield")
    assert r.settled and r.layer == "declared" and r.target.id == "granny"


async def test_route_scan_refuses_an_unknown_name_with_a_code(tmp_path):
    out = await t.route_scan(_cfg(), staging=None, assemble=None,
                             stage_id="s1", target="Nachbar")
    assert out["ok"] is False and out["error_code"] == "unknown_target"


@pytest.fixture
def jobs(monkeypatch):
    fake = AsyncMock()
    fake.start = AsyncMock(return_value={"ok": True, "job_id": "j1", "status": "running"})
    monkeypatch.setattr(server, "_config", _cfg())
    monkeypatch.setattr(server, "_staging", object())
    monkeypatch.setattr(server, "_jobs", fake)
    return fake


async def test_scan_document_refuses_an_unknown_target_before_the_feeder_runs(jobs):
    out = await server.scan_document(None, target="Nachbar")
    assert out["ok"] is False and out["error_code"] == "unknown_target"
    assert "Oma" in out["error"]
    jobs.start.assert_not_awaited()


async def test_scan_document_hands_the_job_the_canonical_id(jobs):
    await server.scan_document(None, target="Omas Renfield")
    assert jobs.start.await_args.kwargs["target"] == "granny"


async def test_scan_document_without_a_target_is_unchanged(jobs):
    await server.scan_document(None)
    assert jobs.start.await_args.kwargs["target"] == ""


# --- launchd entrypoint: push tokens come from targets.yaml, not from the script


@pytest.fixture
def entrypoint(tmp_path):
    """A copy of the launchd layout with a fake Keychain and a fake server.

    The script pins its own PATH; the copy prepends the fake `security`, which is
    the only change made to it."""
    root = tmp_path / "scanner"
    (root / "launchd").mkdir(parents=True)
    (root / ".venv" / "bin").mkdir(parents=True)
    script = (REPO / "launchd" / "run-scanner-mcp.sh").read_text()
    fakebin = tmp_path / "fakebin"
    fakebin.mkdir()
    assert 'export PATH="' in script
    script = script.replace('export PATH="', f'export PATH="{fakebin}:', 1)
    (root / "launchd" / "run-scanner-mcp.sh").write_text(script)
    python = root / ".venv" / "bin" / "python"
    python.write_text(f'#!/bin/sh\nexec "{sys.executable}" "$@"\n')
    server_stub = root / ".venv" / "bin" / "renfield-mcp-scanner"
    server_stub.write_text("#!/bin/sh\nenv | grep -E '^(SCANNER_TOKEN_|TOK_)' | sort\n")
    security = fakebin / "security"
    # Every secret exists except TOK_MISSING; reads only ever ask with `-s <name> -w`.
    security.write_text(
        "#!/bin/sh\nwhile [ $# -gt 0 ]; do [ \"$1\" = -s ] && n=$2; shift; done\n"
        "[ \"$n\" = TOK_MISSING ] && exit 44\necho \"secret-of-$n\"\n")
    for f in (python, server_stub, security):
        f.chmod(0o755)
    (root / "targets.yaml").write_text(
        "targets:\n"
        "  - {id: a, label: A, base_url: x, scan_profile_id: d, token_env: TOK_ALPHA}\n"
        "  - {id: b, label: B, base_url: x, scan_profile_id: d, token_env: TOK_MISSING}\n"
        "  - {id: c, label: C, base_url: x, scan_profile_id: d, token_env: 'lower;id'}\n")
    return root


def _run(root):
    return subprocess.run(["/bin/bash", str(root / "launchd" / "run-scanner-mcp.sh")],
                          capture_output=True, text=True, timeout=30,
                          env={"HOME": str(root)})


@pytest.mark.skipif(shutil.which("bash") is None, reason="needs bash")
def test_every_token_named_in_the_registry_is_loaded(entrypoint):
    out = _run(entrypoint)
    assert out.returncode == 0, out.stderr
    assert "TOK_ALPHA=secret-of-TOK_ALPHA" in out.stdout
    # A missing Keychain entry disables only that target (empty, not absent),
    # exactly as the per-name lines did; the preflight names it.
    assert "TOK_MISSING=\n" in out.stdout


@pytest.mark.skipif(shutil.which("bash") is None, reason="needs bash")
def test_an_unreadable_registry_stops_the_start(entrypoint):
    """Not a server without target tokens: that looks healthy and fails each push."""
    (entrypoint / "targets.yaml").write_text("targets: [unclosed\n")
    out = _run(entrypoint)
    assert out.returncode != 0
    assert "TOK_" not in out.stdout


@pytest.mark.skipif(shutil.which("bash") is None, reason="needs bash")
def test_a_malformed_token_name_is_never_exported(entrypoint):
    out = _run(entrypoint)
    assert "lower" not in out.stdout
    assert "skipping token_env" in out.stderr


def test_the_entrypoint_names_no_target():
    script = (REPO / "launchd" / "run-scanner-mcp.sh").read_text()
    loaded = [line for line in script.splitlines()
              if line.startswith("export SCANNER_TOKEN_") and "PRIMARY" not in line]
    assert loaded == [], loaded
