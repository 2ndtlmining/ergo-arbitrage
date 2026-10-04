"""CLI help: every command says what it does, its defaults and an example (#83)."""
import argparse

import pytest

import arb
import main

COMMANDS = ["balance", "quote", "swap", "redeem", "send", "arb", "doctor", "config", "backup", "resume"]


def subparsers():
    action = next(a for a in arb.build_parser()._actions if isinstance(a, argparse._SubParsersAction))
    return action.choices


@pytest.mark.parametrize("name", COMMANDS)
def test_every_arb_command_has_a_description_and_an_example(name):
    p = subparsers()[name]
    text = p.format_help()
    assert p.description and "examples:" in text and f"python arb.py {name}" in text


@pytest.mark.parametrize("name", COMMANDS)
def test_every_option_has_help_text(name):
    for action in subparsers()[name]._actions:
        if action.option_strings and action.dest != "help":
            assert action.help, (name, action.option_strings)


def test_transaction_commands_say_dry_run_is_the_default():
    for name in ("swap", "redeem", "send", "arb"):
        assert "Dry run by default" in subparsers()[name].format_help()


def test_defaults_are_shown():
    assert "(default: 14)" in subparsers()["backup"].format_help()
    assert "(default: redeem)" in subparsers()["arb"].format_help()


def test_main_help_has_examples_and_pointers():
    text = main.build_parser().format_help()
    for needle in ("examples:", "--once --plain", "arb.py doctor", ".env", "--max-trade-erg", "(default:"):
        assert needle in text, needle
    for action in main.build_parser()._actions:
        if action.option_strings and action.dest != "help":
            assert action.help, action.option_strings
