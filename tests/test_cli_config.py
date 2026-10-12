import pytest

from satin_agent.cli import build_parser, main
from satin_agent.config import (
    MissingSecretError,
    anthropic_api_key,
    load_config,
    load_dotenv,
)


@pytest.mark.parametrize(
    "argv",
    [
        [],
        ["publish"],
        ["sandbox"],
        ["sandbox", "gc"],
        ["cma-orchestrator"],
        ["cc"],
        ["cc", "spawn"],
        ["cc", "reconcile"],
        ["slack"],
    ],
)
def test_help_for_every_subcommand(argv, capsys):
    with pytest.raises(SystemExit) as e:
        build_parser().parse_args([*argv, "--help"])
    assert e.value.code == 0
    assert "usage: satin" in capsys.readouterr().out


def test_missing_config_file_is_error(capsys):
    assert main(["slack", "--config", "/nonexistent.yaml"]) == 2
    assert "設定ファイル" in capsys.readouterr().err


def test_relative_paths_resolve_against_config_file(tmp_path):
    sub = tmp_path / "conf"
    sub.mkdir()
    (sub / "satin.yaml").write_text("assets:\n  dir: ../assets\nnamespace: sandboxes\n")
    cfg = load_config(sub / "satin.yaml")
    assert cfg.path("assets.dir") == (tmp_path / "assets").resolve()
    assert cfg.get("namespace") == "sandboxes"
    assert cfg.get("nope.x", 3) == 3


def test_api_key_precedence_and_errors():
    assert anthropic_api_key({"CLAUDE_API_KEY": "a", "ANTHROPIC_API_KEY": "b"}) == "a"
    assert anthropic_api_key({"ANTHROPIC_API_KEY": "b"}) == "b"
    with pytest.raises(MissingSecretError) as e:
        anthropic_api_key({})
    assert "CLAUDE_API_KEY" in str(e.value)


def test_secret_value_not_in_error_or_dotenv_parse(tmp_path):
    secret = "sk-ant-SECRETVALUE"
    env: dict[str, str] = {}
    (tmp_path / ".env").write_text(f"CLAUDE_API_KEY={secret}\n# c\n")
    load_dotenv(tmp_path / ".env", env)
    assert anthropic_api_key(env) == secret
    with pytest.raises(MissingSecretError) as e:
        anthropic_api_key({"OTHER": secret})
    assert secret not in str(e.value)
