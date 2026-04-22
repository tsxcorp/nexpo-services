"""
Unit tests for `safe_substitute` — whitelist enforcement, HTML escaping,
legacy ${uuid} shim, null handling.
Pure-function tests — no Directus or network calls.
"""
from __future__ import annotations

import pytest

from app.services.handlers.template_render import safe_substitute


BASE_CTX = {
    "event": {"name": "Meet Australia 2026", "location": "Binh Duong"},
    "recipient": {"full_name": "Nguyễn Văn A", "email": "a@example.com"},
    "meeting": {"scheduled_at": "2026-05-19 09:00", "location": "Booth A12"},
    "exhibitor": {"name": "BHP Group", "booth": "A12"},
    "form_answers": {
        "a1b2c3d4-e5f6-4789-abcd-ef0123456789": "ACME Corp",
    },
}


def test_valid_key_substituted():
    out = safe_substitute("Hello {{recipient.full_name}}!", BASE_CTX, module="meeting")
    assert "Nguyễn Văn A" in out
    assert "{{recipient.full_name}}" not in out


def test_unknown_key_left_literal_and_logged(caplog):
    template = "Secret: {{hacker.password}}"
    with caplog.at_level("WARNING"):
        out = safe_substitute(template, BASE_CTX, module="meeting")
    assert "{{hacker.password}}" in out
    assert any("not in whitelist" in rec.message for rec in caplog.records)


def test_legacy_form_field_resolved():
    uuid = "a1b2c3d4-e5f6-4789-abcd-ef0123456789"
    out = safe_substitute(f"Company: ${{{uuid}}}", BASE_CTX, module="form")
    assert "ACME Corp" in out
    assert f"${{{uuid}}}" not in out


def test_legacy_syntax_outside_form_module_preserved():
    uuid = "a1b2c3d4-e5f6-4789-abcd-ef0123456789"
    template = f"Company: ${{{uuid}}}"
    out = safe_substitute(template, BASE_CTX, module="meeting")
    # Legacy ${uuid} only active in form module
    assert f"${{{uuid}}}" in out


def test_null_value_becomes_empty_string():
    ctx = {**BASE_CTX, "recipient": {"full_name": None, "email": "a@example.com"}}
    out = safe_substitute("Hi {{recipient.full_name}}!", ctx, module="meeting")
    assert out == "Hi !"


def test_html_in_value_escaped_by_default():
    ctx = {"event": {"name": "<script>alert('xss')</script>"}, "recipient": None}
    out = safe_substitute("Event: {{event.name}}", ctx, module="broadcast")
    assert "<script>" not in out
    assert "&lt;script&gt;" in out


def test_html_escape_disabled_flag():
    ctx = {"event": {"name": "<b>Bold</b>"}, "recipient": None}
    out = safe_substitute("{{event.name}}", ctx, module="broadcast", escape_html=False)
    assert "<b>Bold</b>" in out


def test_module_scope_enforcement():
    # meeting.* not allowed in broadcast module
    out = safe_substitute("At {{meeting.location}}", BASE_CTX, module="broadcast")
    assert "{{meeting.location}}" in out  # left literal


def test_missing_nested_returns_empty():
    ctx = {"event": {"name": "X"}, "recipient": None}
    out = safe_substitute("Hi {{recipient.full_name}}", ctx, module="meeting")
    assert out == "Hi "


def test_form_dynamic_field_unknown_id_left_literal():
    # Unknown uuid in context → becomes empty (not literal) — form.* route treats as dynamic
    unknown = "99999999-9999-4999-9999-999999999999"
    out = safe_substitute(f"{{{{form.{unknown}}}}}", BASE_CTX, module="form")
    # Unknown form field id → looked up but missing → empty string
    assert out == ""


def test_unknown_module_leaves_template_untouched(caplog):
    with caplog.at_level("WARNING"):
        out = safe_substitute("{{event.name}}", BASE_CTX, module="garbage")
    assert out == "{{event.name}}"


def test_whitespace_in_braces_tolerated():
    out = safe_substitute("{{  event.name  }}", BASE_CTX, module="meeting")
    assert "Meet Australia 2026" in out
