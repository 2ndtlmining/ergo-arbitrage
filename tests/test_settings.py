"""Settings: placeholders count as unset, `arb.py config`, and docs that cover every setting (#84 #88)."""
import os
import re
import subprocess
import sys
from pathlib import Path

import pytest

import config
import config_check

REPO = Path(__file__).resolve().parent.parent


def run_config(env: dict) -> str:
    """Import config in a fresh interpreter with these environment values (they win over .env)."""
    code = "import config; print(config.DISCORD_ENABLED, repr(config.DISCORD_USER_ID), config.PLACEHOLDERS, " \
           "config.MIN_PROFIT_PERCENT)"
    out = subprocess.run([sys.executable, "-c", code], cwd=REPO, env={**os.environ, **env},
                         capture_output=True, text=True, check=True)
    return out.stdout.strip()


@pytest.mark.parametrize("value", ["your_api_key_here", "YOUR_KEY", "https://discord.com/api/webhooks/your_webhook_here"])
def test_placeholder_values_are_recognised(value):
    assert config.is_placeholder(value)


@pytest.mark.parametrize("value", ["", "abc123", "https://discord.com/api/webhooks/123/AbC-your", "http://127.0.0.1:9053"])
def test_real_values_are_not_placeholders(value):
    assert not config.is_placeholder(value)


def test_template_webhook_and_user_id_leave_discord_off():
    out = run_config({"DISCORD_WEBHOOK_URL": "https://discord.com/api/webhooks/your_webhook_here",
                      "DISCORD_USER_ID": "your_discord_user_id_here"})
    assert out.startswith("False '' ")
    assert "DISCORD_WEBHOOK_URL" in out and "DISCORD_USER_ID" in out


def test_blank_numeric_setting_uses_its_default():
    assert run_config({"MIN_PROFIT_PERCENT": "", "DISCORD_WEBHOOK_URL": ""}).endswith("[] 0.5")


# ---------- arb.py config ----------

def test_report_masks_secrets_and_names_sources():
    env = {"ERGO_NODE_API_KEY": "supersecretkey1234", "DISCORD_WEBHOOK_URL": "https://discord.com/api/webhooks/1/tok3n"}
    text = "\n".join(config_check.report(file_values={"ERGO_NODE_API_KEY": "supersecretkey1234"}, environ=env))
    assert "supersecretkey" not in text and "tok3n" not in text
    assert re.search(r"ERGO_NODE_API_KEY\s+\*+1234\s+\.env", text)
    assert re.search(r"DISCORD_WEBHOOK_URL\s+\*+\S*\s+environment", text)
    assert re.search(r"MIN_PROFIT_PERCENT\s+0\.5\s+default", text)


def test_report_flags_unknown_keys_with_a_suggestion():
    text = "\n".join(config_check.report(file_values={"DISCORD_WEBHOOK": "x", "MIN_PROFT_PERCENT": "1"}, environ={}))
    assert "DISCORD_WEBHOOK " in text and "DISCORD_WEBHOOK_URL?" in text
    assert "MIN_PROFIT_PERCENT?" in text


def test_report_flags_placeholders_and_deprecated_names():
    env = {"KUCOIN_API_KEY": "your_kucoin_api_key_here", "LIVE_CONFIRM_SCANS": "3"}
    text = "\n".join(config_check.report(file_values=env, environ=env))
    assert re.search(r"KUCOIN_API_KEY.*placeholder", text)
    assert re.search(r"LIVE_CONFIRM_SCANS.*LIVE_CONFIRM_POLLS", text)


def test_report_without_problems_says_so():
    text = "\n".join(config_check.report(file_values={"MIN_PROFIT_PERCENT": "1"}, environ={"MIN_PROFIT_PERCENT": "1"}))
    assert "No unknown, placeholder or deprecated settings" in text


def test_cli_config_command_runs(capsys):
    import arb
    arb.main(["config"])                                     # no node needed, exits normally
    assert "ERGO_NODE_URL" in capsys.readouterr().out


# ---------- docs cover every setting (#88) ----------

def test_every_setting_has_a_group():
    grouped = [name for names in config_check.GROUPS.values() for name in names]
    assert sorted(grouped) == sorted(config.SETTINGS), set(grouped) ^ set(config.SETTINGS)


def test_readme_settings_reference_covers_every_setting():
    readme = (REPO / "README.md").read_text(encoding="utf-8")
    missing = [name for name in config.SETTINGS if f"`{name}`" not in readme]
    assert not missing


def test_env_example_lists_every_setting_and_no_dead_ones():
    text = (REPO / ".env.example").read_text(encoding="utf-8")
    listed = set(re.findall(r"^#?\s*([A-Z][A-Z0-9_]+)=", text, re.MULTILINE))
    assert set(config.SETTINGS) - listed == set()
    assert listed - set(config.SETTINGS) == set()


def test_env_example_has_no_values_that_switch_features_on_with_junk():
    for line in (REPO / ".env.example").read_text(encoding="utf-8").splitlines():
        assert "your_" not in line.lower(), line
