"""Profile CLI backend: show and edit the stored pair profile."""

import json

import pytest
from typer.testing import CliRunner

from parlance import cli, profile, store
from parlance.config import Settings

DOC = {
    "pos_tags": ["sloveso"],
    "register_tags": ["neutrální"],
    "gender_values": [],
    "gloss_style": "",
    "sentence_style": "",
    "false_friend_policy": "",
    "notes": "",
}


def db_with_profile(tmp_path):
    conn = store.connect(tmp_path / "t.db")
    store.meta_set(conn, "pair_profile", DOC)
    return conn


def test_show_roundtrip(tmp_path):
    conn = db_with_profile(tmp_path)
    assert json.loads(profile.show(conn)) == DOC


def test_show_without_profile(tmp_path):
    conn = store.connect(tmp_path / "t.db")
    assert "no pair profile" in profile.show(conn)


def test_edit_saves_valid_changes(tmp_path, monkeypatch):
    conn = db_with_profile(tmp_path)
    edited = dict(DOC, notes="refined by hand")
    monkeypatch.setattr(
        profile.click, "edit", lambda text, extension: json.dumps(edited, ensure_ascii=False)
    )
    assert profile.edit(conn) == "saved"
    assert store.meta_get(conn, "pair_profile") == edited


def test_edit_rejects_bad_json_and_missing_fields(tmp_path, monkeypatch):
    conn = db_with_profile(tmp_path)
    monkeypatch.setattr(profile.click, "edit", lambda text, extension: "{not json")
    with pytest.raises(ValueError, match="not valid JSON"):
        profile.edit(conn)

    monkeypatch.setattr(
        profile.click, "edit", lambda text, extension: json.dumps({"notes": "dropped fields"})
    )
    with pytest.raises(ValueError):  # pydantic ValidationError is a ValueError
        profile.edit(conn)
    assert store.meta_get(conn, "pair_profile") == DOC  # rejected edits never land


def test_edit_no_change_is_a_no_op(tmp_path, monkeypatch):
    conn = db_with_profile(tmp_path)
    monkeypatch.setattr(profile.click, "edit", lambda text, extension: None)
    assert "unchanged" in profile.edit(conn)
    assert store.meta_get(conn, "pair_profile") == DOC


def test_profile_cli_show(tmp_path, monkeypatch):
    monkeypatch.setattr(cli, "get_settings", lambda: Settings(data_dir=tmp_path))
    conn = store.connect(tmp_path / "parlance.db")  # the path the CLI reads
    store.meta_set(conn, "pair_profile", DOC)
    result = CliRunner().invoke(cli.app, ["profile", "show"])
    assert result.exit_code == 0
    assert "sloveso" in result.stdout
