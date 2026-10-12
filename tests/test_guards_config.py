import pytest
import yaml

from satin_agent.config import ConfigError
from satin_agent.guards.config import GuardsConfig

YAML = """
filters:
  teams: [T1]
  users: [U1, U2]
  deny_guests: true
  max_chars: 4000
input_guard:
  model: gemini-x
  project: p
  criteria_file: /tmp/c.md
  fail_open: true
output_inspection:
  template: projects/p/locations/us-central1/templates/t
  ignored_filters: []
  on_sdp_match: block
"""


def test_defaults():
    cfg = GuardsConfig.from_dict({"output_inspection": {"template": "t"}})
    assert cfg.filters.max_chars == 8000 and cfg.filters.rate_limit_per_minute == 20
    assert cfg.filters.users_cache_seconds == 600
    assert cfg.input_guard.model == "gemini-3.8-flash"
    assert cfg.input_guard.fail_open is False and cfg.input_guard.history_size == 5
    assert cfg.output_inspection.ignored_filters == ("pi_and_jailbreak",)
    assert cfg.output_inspection.on_sdp_match == "mask"


def test_load_from_yaml_dict():
    cfg = GuardsConfig.from_dict(yaml.safe_load(YAML))
    assert cfg.filters.teams == ("T1",) and cfg.filters.users == ("U1", "U2")
    assert cfg.filters.deny_guests and cfg.filters.max_chars == 4000
    assert cfg.input_guard.model == "gemini-x" and cfg.input_guard.fail_open
    assert cfg.input_guard.criteria_file == "/tmp/c.md"
    assert cfg.output_inspection.ignored_filters == ()
    assert cfg.output_inspection.on_sdp_match == "block"


@pytest.mark.parametrize(
    "raw",
    [
        {"bogus": {}},
        {"filters": {"bogus": 1}},
        {"filters": {"users": "U1"}},
        {"filters": []},
        {},  # テンプレートなしでModel Armorが有効
        {"output_inspection": {"template": "t", "on_sdp_match": "x"}},
    ],
)
def test_invalid(raw):
    with pytest.raises(ConfigError):
        GuardsConfig.from_dict(raw)


def test_disabled_output_inspection_needs_no_template():
    assert GuardsConfig.from_dict({"output_inspection": {"enabled": False}})
