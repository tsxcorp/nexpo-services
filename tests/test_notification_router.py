"""
Tests for notification_router.py — FLOW_NOTIFICATION_MAP completeness.
Run: cd nexpo-services && python -m pytest tests/test_notification_router.py -v
"""

from app.services.notification_router import FLOW_NOTIFICATION_MAP


ALL_FLOWS = [
    "direct_visitor_exhibitor",
    "organizer_only",
    "visitor_organizer_exhibitor",
    "ai_organizer_exhibitor",
    "direct_exhibitor_visitor",
    "exhibitor_organizer_visitor",
]


def test_all_flows_present():
    for flow in ALL_FLOWS:
        assert flow in FLOW_NOTIFICATION_MAP, f"Missing flow: {flow}"


def test_all_flows_have_transitions():
    for flow in ALL_FLOWS:
        transitions = FLOW_NOTIFICATION_MAP[flow]
        assert len(transitions) >= 2, f"{flow} should have at least 2 transitions, got {len(transitions)}"


def test_visitor_organizer_exhibitor_transitions():
    flow = FLOW_NOTIFICATION_MAP["visitor_organizer_exhibitor"]
    assert "pending→organizer_approved" in flow
    assert "pending→organizer_rejected" in flow
    assert "organizer_approved→exhibitor_agreed" in flow
    assert "organizer_approved→exhibitor_declined" in flow


def test_direct_visitor_exhibitor_no_organizer_step():
    flow = FLOW_NOTIFICATION_MAP["direct_visitor_exhibitor"]
    assert "pending→organizer_approved" not in flow, "direct flow should not have organizer step"


def test_all_mappings_have_trigger_and_recipients():
    for flow_name, transitions in FLOW_NOTIFICATION_MAP.items():
        for key, mapping in transitions.items():
            assert "trigger" in mapping, f"{flow_name}/{key}: missing trigger"
            assert "recipients" in mapping, f"{flow_name}/{key}: missing recipients"
            assert len(mapping["recipients"]) > 0, f"{flow_name}/{key}: empty recipients"


def test_valid_recipient_roles():
    valid_roles = {"organizer", "exhibitor", "visitor"}
    for flow_name, transitions in FLOW_NOTIFICATION_MAP.items():
        for key, mapping in transitions.items():
            for recipient in mapping["recipients"]:
                assert recipient in valid_roles, f"{flow_name}/{key}: invalid recipient {recipient}"


def test_total_transition_count():
    total = sum(len(v) for v in FLOW_NOTIFICATION_MAP.values())
    assert total >= 16, f"Expected 16+ transitions total, got {total}"
