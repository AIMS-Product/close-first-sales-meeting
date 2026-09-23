import unittest

import outcome_sync
import repair_zoom_false_noshows


ORG_EMAILS = {"rep@vendingpreneurs.com"}


def meeting(contact_id="contact_1", attendee_name=None):
    return {
        "attendees": [{
            "email": "prospect@example.com",
            "name": attendee_name,
            "contact_id": contact_id,
        }]
    }


def lead(display_name, contact_name):
    return {
        "display_name": display_name,
        "contacts": [{
            "id": "contact_1",
            "name": contact_name,
            "emails": [{"email": "prospect@example.com"}],
        }],
    }


class ProductionNameMatchingTests(unittest.TestCase):
    def test_contact_name_fills_missing_attendee_name(self):
        names = outcome_sync.prospect_names_for(
            meeting(), lead("Debbie Barca", "Debbie Barca"), ORG_EMAILS
        )

        self.assertEqual(["Debbie Barca"], names)

    def test_different_contact_surname_is_rejected(self):
        names = outcome_sync.prospect_names_for(
            meeting(), lead("Ashley Trybus", "Ashley Hoeger"), ORG_EMAILS
        )

        self.assertEqual([], names)

    def test_different_zoom_surname_is_rejected(self):
        self.assertFalse(outcome_sync._name_match("Lowell Gilliland", "Lowell Gill"))

    def test_zoom_age_metadata_is_ignored(self):
        self.assertTrue(
            outcome_sync._name_match("Chelsea Streeter 37 YO", "Chelsea Streeter")
        )

    def test_ambiguous_one_word_name_is_rejected(self):
        self.assertFalse(outcome_sync._name_match("Mark D", "Mark"))


class RepairAllowlistTests(unittest.TestCase):
    def test_known_exceptions_are_not_allowlisted(self):
        targets = repair_zoom_false_noshows.REPAIR_TARGETS

        self.assertNotIn("acti_wWzkSGghaFoOxblzin8uqLJiJHQRIS2RxE44EzprIwY", targets)
        self.assertNotIn("acti_X6rjlXSdCSM9IXO17h22ctwzVErQhVhP5ZtP3J3FRuf", targets)

    def test_only_reviewed_nine_are_allowlisted(self):
        self.assertEqual(9, len(repair_zoom_false_noshows.REPAIR_TARGETS))


if __name__ == "__main__":
    unittest.main()
