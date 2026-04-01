"""
Tests for template_renderer.py — render + sanitize functions.
Run: cd nexpo-services && python -m pytest tests/test_template_renderer.py -v
"""

from app.services.template_renderer import render, sanitize_subject


def test_render_all_variables():
    html = "<p>Hello {{visitor_name}}, welcome to {{event_name}}!</p>"
    result = render(html, {"visitor_name": "Alice", "event_name": "NEXPO 2026"})
    assert result == "<p>Hello Alice, welcome to NEXPO 2026!</p>"


def test_render_legacy_dollar_syntax():
    html = "<p>Hello ${visitor_name}!</p>"
    result = render(html, {"visitor_name": "Bob"})
    assert result == "<p>Hello Bob!</p>"


def test_render_mixed_syntax():
    html = "<p>{{visitor_name}} at ${event_name}</p>"
    result = render(html, {"visitor_name": "Alice", "event_name": "NEXPO"})
    assert result == "<p>Alice at NEXPO</p>"


def test_render_missing_variable_preserved():
    html = "<p>Hello {{visitor_name}}, your job: {{job_title}}</p>"
    result = render(html, {"visitor_name": "Alice"})
    assert "Alice" in result
    assert "{{job_title}}" in result  # missing var preserved as-is


def test_render_xss_escaped():
    html = "<p>Hello {{visitor_name}}</p>"
    result = render(html, {"visitor_name": '<script>alert("xss")</script>'})
    assert "<script>" not in result
    assert "&lt;script&gt;" in result


def test_render_html_entities_escaped():
    html = "<p>{{message}}</p>"
    result = render(html, {"message": 'O\'Reilly & Sons <Corp>'})
    assert "&amp;" in result
    assert "&lt;Corp&gt;" in result


def test_render_empty_variable():
    html = "<p>Hello {{visitor_name}}</p>"
    result = render(html, {"visitor_name": ""})
    assert result == "<p>Hello </p>"


def test_render_none_variable_preserved():
    html = "<p>Hello {{visitor_name}}</p>"
    result = render(html, {"visitor_name": None})
    assert "{{visitor_name}}" in result


def test_sanitize_subject_clean():
    assert sanitize_subject("Hello World") == "Hello World"


def test_sanitize_subject_crlf_injection():
    malicious = "Subject\r\nBcc: attacker@evil.com"
    result = sanitize_subject(malicious)
    assert "\r" not in result
    assert "\n" not in result
    assert "SubjectBcc: attacker@evil.com" == result


def test_sanitize_subject_newline_only():
    assert sanitize_subject("Line1\nLine2") == "Line1Line2"
