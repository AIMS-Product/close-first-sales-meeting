import unittest

import outcome_sync as production
from single_name_shadow import (
    candidate_diagnostics,
    proposed_prospect_names_for,
    single_name_contact_candidates,
)


ORG_EMAILS = {"rep@example.com"}


def lead(display_name="Terri", contact_name="Terri Smith"):
    return {
        "display_name": display_name,
        "contacts": [{
            "id": "cont_1",
            "name": contact_name,
            "emails": [{"email": "terri@example.com"}],
        }],
    }


def meeting(contact_id="cont_1", email="terri@example.com"):
    return {
        "attendees": [
            {"name": "Rep", "email": "rep@example.com"},
            {"name": "Terri", "email": email, "contact_id": contact_id},
        ]
    }


class SingleNameShadowTests(unittest.TestCase):
    def test_exact_contact_id_supplies_full_name(self):
        self.assertEqual(
            single_name_contact_candidates(meeting(), lead(), ORG_EMAILS),
            ["Terri Smith"],
        )

    def test_exact_attendee_email_fallback_supplies_full_name(self):
        self.assertEqual(
            single_name_contact_candidates(
                meeting(contact_id=None), lead(), ORG_EMAILS
            ),
            ["Terri Smith"],
        )

    def test_different_contact_first_name_is_rejected(self):
        self.assertEqual(
            single_name_contact_candidates(
                meeting(), lead(contact_name="Susan Smith"), ORG_EMAILS
            ),
            [],
        )

    def test_contact_without_surname_is_rejected(self):
        self.assertEqual(
            single_name_contact_candidates(
                meeting(), lead(contact_name="Terri"), ORG_EMAILS
            ),
            [],
        )

    def test_diagnostics_explain_contact_without_surname(self):
        diagnostics = candidate_diagnostics(
            meeting(), lead(contact_name="Terri"), ORG_EMAILS
        )
        self.assertEqual(diagnostics["linked_contacts"], 1)
        self.assertEqual(diagnostics["contact_missing_surname"], 1)
        self.assertEqual(diagnostics["contact_first_matches"], 1)

    def test_multiword_lead_keeps_existing_surname_guard(self):
        ashley_lead = lead(
            display_name="Ashley Trybus", contact_name="Ashley Hoeger"
        )
        self.assertEqual(
            proposed_prospect_names_for(meeting(), ashley_lead, ORG_EMAILS),
            [],
        )

    def test_zoom_different_surname_does_not_match(self):
        names = proposed_prospect_names_for(meeting(), lead(), ORG_EMAILS)
        participants = [
            {"name": "Rep", "email": "rep@example.com", "seconds": 1800},
            {"name": "Terri Jones", "email": "", "seconds": 1200},
        ]
        result, _detail = production.zoom_signal(
            participants, {"terri@example.com"}, ORG_EMAILS, names
        )
        self.assertEqual(result, "no_show")

    def test_zoom_exact_full_name_matches(self):
        names = proposed_prospect_names_for(meeting(), lead(), ORG_EMAILS)
        participants = [
            {"name": "Rep", "email": "rep@example.com", "seconds": 1800},
            {"name": "Terri Smith", "email": "", "seconds": 1200},
        ]
        result, _detail = production.zoom_signal(
            participants, {"terri@example.com"}, ORG_EMAILS, names
        )
        self.assertEqual(result, "completed")


if __name__ == "__main__":
    unittest.main()
