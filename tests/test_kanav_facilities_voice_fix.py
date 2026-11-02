import pytest
from unittest.mock import patch, MagicMock
from facilities.auth import resolve_facilities_user, is_all_buildings_user
from facilities.flows.router import is_facilities_user, route_facilities_message
from elara.team_router import route_incoming_message, is_dual_access_user, clear_all_team_contexts
from elara.config import KANAV_PHONE, DEVELOPER_PHONE, KANAV_PHONES


class TestKanavFacilitiesVoiceFix:
    """Verify that Kanav is recognized and authorized for Facilities voice and text tasks."""

    @pytest.mark.parametrize("phone", [
        "919811867829",
        "+919811867829",
        "9811867829",
    ])
    def test_kanav_identification_and_all_buildings_access(self, phone):
        """Kanav must resolve with is_facilities_user=True and access to all buildings."""
        user = resolve_facilities_user(phone)
        assert user is not None
        assert user.get("name") == "Kanav"
        assert user.get("role") == "Director"
        assert user.get("is_facilities_user") is True
        assert is_facilities_user(phone) is True
        assert is_all_buildings_user(user) is True
        assert is_dual_access_user(phone) is True
        assert "Common" in user.get("permitted_buildings", [])

    @pytest.mark.parametrize("non_kanav_phone", [
        "918527907829",
        "919711187829",
    ])
    def test_unrelated_numbers_are_not_kanav(self, non_kanav_phone):
        """Verify that 918527907829 and 919711187829 are NOT recognized as Kanav."""
        from elara.config import is_kanav_phone
        assert is_kanav_phone(non_kanav_phone) is False
        user = resolve_facilities_user(non_kanav_phone)
        assert user is None or user.get("name") != "Kanav"

    def test_voice_routing_does_not_send_unregistered_facilities_error(self):
        """When Kanav sends a facilities voice note, bot must not deny access."""
        from facilities.flows.router import clear_session as clear_fac_session
        sender = KANAV_PHONE
        clear_all_team_contexts()
        clear_fac_session(sender)
        transcript = (
            "Create a facilities task for me to have the C check, D check, "
            "and B check data ready for all three buildings. Actually, sorry, it's for common."
        )

        sent_messages = []

        def mock_send_text(to, body, *args, **kwargs):
            sent_messages.append(body)
            return True

        def mock_send_ib(to, body, buttons, *args, **kwargs):
            sent_messages.append(body)
            return True

        try:
            with patch("facilities.flows.voice_handler.send_text", side_effect=mock_send_text), \
                 patch("facilities.flows.voice_handler.send_interactive_buttons", side_effect=mock_send_ib), \
                 patch("whatsapp.ux.send_text", side_effect=mock_send_text), \
                 patch("whatsapp.ux.send_interactive_buttons", side_effect=mock_send_ib), \
                 patch("facilities.llm._call_llm", return_value='''{
                     "intents": [
                         {
                             "intent": "create_task",
                             "confidence": 0.95,
                             "entities": {
                                 "building": "Common",
                                 "type": "Other",
                                 "issue_action": "have the C check, D check, and B check data ready",
                                 "owner": "Facilities Director",
                                 "status": "Open"
                             }
                         }
                     ]
                 }'''):
                handled = route_incoming_message(sender, voice_transcript=transcript)

            assert handled is True
            # Verify that access was never denied
            for msg in sent_messages:
                assert "You are not registered as a Facilities team member" not in msg

            # Verify confirmation or processing prompt was sent
            assert any("Operation" in msg or "Create Task" in msg or "Processing voice note" in msg for msg in sent_messages)
        finally:
            clear_fac_session(sender)
            clear_all_team_contexts()

    def test_kanav_cannot_be_rejected_even_if_auth_returns_none(self):
        """Even if database lookup temporarily fails, Director fail-safe must grant access."""
        sender = "+919811867829"
        sent_messages = []

        def mock_send_text(to, body, *args, **kwargs):
            sent_messages.append(body)
            return True

        with patch("facilities.flows.router.resolve_facilities_user", return_value=None), \
             patch("whatsapp.ux.send_text", side_effect=mock_send_text), \
             patch("facilities.flows.home.show_home") as mock_show_home:
            route_facilities_message(sender, text="menu")

        assert mock_show_home.called
        for msg in sent_messages:
            assert "You are not registered as a Facilities team member" not in msg

