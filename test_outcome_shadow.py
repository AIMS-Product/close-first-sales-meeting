import unittest
from datetime import datetime, timezone

import outcome_shadow


ORG_EMAILS = {"eric@modern-amenities.com"}


class FakeZoom:
    def __init__(self, participants):
        self.participants = participants

    def participants_for(self, meeting_id, starts_at):
        self.meeting_id = meeting_id
        self.starts_at = starts_at
        return self.participants


def meeting(attendee_name=None, attendee_email="debbie@example.com"):
    return {
        "id": "meeting_1",
        "lead_id": "lead_1",
        "title": "Vendingpreneurs Momentum - Next Steps",
        "starts_at": datetime(2026, 9, 21, 17, 0, tzinfo=timezone.utc).isoformat(),
        "outcome_id": None,
        "location": "https://us06web.zoom.us/j/81416527389",
        "note": "",
        "attendees": [
            {
                "email": attendee_email,
                "name": attendee_name,
                "contact_id": "contact_1",
                "is_organizer": False,
            },
            {
                "email": "eric@modern-amenities.com",
                "name": "Eric Piccione",
                "contact_id": None,
                "is_organizer": True,
            },
        ],
    }


def lead(contact_name="Debbie Barca"):
    return {
        "id": "lead_1",
        "display_name": "Debbie Barca",
        "contacts": [
            {
                "id": "contact_1",
                "name": contact_name,
                "emails": [{"email": "debbie@example.com"}],
            }
        ],
    }


class ShadowMatchingTests(unittest.TestCase):
    def test_contact_name_recovers_anonymous_zoom_guest(self):
        participants = [
            {"name": "Eric Piccione", "email": "eric@modern-amenities.com", "seconds": 3600},
            {"name": "Debbie Barca", "email": "", "seconds": 3500},
        ]

        result = outcome_shadow.replay_meeting(
            meeting(attendee_name=None), lead(), FakeZoom(participants), ORG_EMAILS
        )

        self.assertEqual([], result["current_names"])
        self.assertEqual(["Debbie Barca"], result["proposed_names"])
        self.assertEqual("no_show", result["current"]["outcome"])
        self.assertEqual("completed", result["proposed"]["outcome"])
        self.assertTrue(result["changed"])

    def test_host_only_meeting_remains_no_show(self):
        participants = [
            {"name": "Eric Piccione", "email": "eric@modern-amenities.com", "seconds": 3600},
        ]

        result = outcome_shadow.replay_meeting(
            meeting(attendee_name=None), lead(), FakeZoom(participants), ORG_EMAILS
        )

        self.assertEqual("no_show", result["current"]["outcome"])
        self.assertEqual("no_show", result["proposed"]["outcome"])
        self.assertFalse(result["changed"])

    def test_unrelated_anonymous_guest_does_not_match(self):
        participants = [
            {"name": "Eric Piccione", "email": "eric@modern-amenities.com", "seconds": 3600},
            {"name": "Someone Else", "email": "", "seconds": 3500},
        ]

        result = outcome_shadow.replay_meeting(
            meeting(attendee_name=None), lead(), FakeZoom(participants), ORG_EMAILS
        )

        self.assertEqual("no_show", result["proposed"]["outcome"])
        self.assertFalse(result["changed"])

    def test_different_contact_surname_does_not_match_lead(self):
        participants = [
            {"name": "Eric Piccione", "email": "eric@modern-amenities.com", "seconds": 1200},
            {"name": "Ashley Hoeger", "email": "", "seconds": 1100},
        ]
        ashley_lead = lead(contact_name="Ashley Hoeger")
        ashley_lead["display_name"] = "Ashley Trybus"

        result = outcome_shadow.replay_meeting(
            meeting(attendee_name=None), ashley_lead, FakeZoom(participants), ORG_EMAILS
        )

        self.assertEqual([], result["proposed_names"])
        self.assertEqual("no_show", result["proposed"]["outcome"])
        self.assertFalse(result["changed"])

    def test_fuzzy_match_with_different_zoom_surname_is_rejected(self):
        participants = [
            {"name": "Eric Piccione", "email": "eric@modern-amenities.com", "seconds": 1200},
            {"name": "Debbie Barcas", "email": "", "seconds": 1100},
        ]

        result = outcome_shadow.replay_meeting(
            meeting(attendee_name=None), lead(), FakeZoom(participants), ORG_EMAILS
        )

        self.assertEqual(["Debbie Barca"], result["proposed_names"])
        self.assertEqual([], result["strictly_matched_names"])
        self.assertEqual("no_show", result["proposed"]["outcome"])
        self.assertFalse(result["changed"])

    def test_existing_email_match_is_unchanged(self):
        participants = [
            {"name": "Eric Piccione", "email": "eric@modern-amenities.com", "seconds": 3600},
            {"name": "Guest", "email": "debbie@example.com", "seconds": 3500},
        ]

        result = outcome_shadow.replay_meeting(
            meeting(attendee_name=None), lead(), FakeZoom(participants), ORG_EMAILS
        )

        self.assertEqual("completed", result["current"]["outcome"])
        self.assertEqual("completed", result["proposed"]["outcome"])
        self.assertFalse(result["changed"])

    def test_only_matching_close_contact_is_used(self):
        other_lead = lead(contact_name="Different Barca")
        other_lead["contacts"].append({
            "id": "contact_2",
            "name": "Debbie Barca",
            "emails": [{"email": "other@example.com"}],
        })

        names = outcome_shadow.proposed_prospect_names(
            meeting(attendee_name=None, attendee_email="debbie@example.com"),
            other_lead,
            ORG_EMAILS,
        )

        self.assertEqual(["Different Barca"], names)


if __name__ == "__main__":
    unittest.main()
