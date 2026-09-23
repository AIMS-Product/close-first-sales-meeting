import unittest

import outcome_sync as production
from single_name_shadow import (
    candidate_diagnostics,
    proposed_prospect_names_for,
    proposed_zoom_signal,
    single_name_contact_candidates,
    unique_external_first_name_signal,
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

    def test_unique_external_full_name_recovers_single_name_contact(self):
        participants = [
            {"name": "Rep", "email": "rep@example.com", "seconds": 1800},
            {"name": "Terri Smith", "email": "", "seconds": 1200},
        ]
        result, reason = unique_external_first_name_signal(
            participants,
            meeting(),
            lead(contact_name="Terri"),
            ORG_EMAILS,
        )
        self.assertEqual((result, reason), (
            "completed", "unique_external_first_name_match"
        ))

    def test_unique_path_rejects_different_zoom_first_name(self):
        participants = [
            {"name": "Rep", "email": "rep@example.com", "seconds": 1800},
            {"name": "Susan Smith", "email": "", "seconds": 1200},
        ]
        result, reason = unique_external_first_name_signal(
            participants,
            meeting(),
            lead(contact_name="Terri"),
            ORG_EMAILS,
        )
        self.assertIsNone(result)
        self.assertEqual(reason, "zoom_participant_first_mismatch")

    def test_unique_path_rejects_one_word_zoom_name(self):
        participants = [
            {"name": "Rep", "email": "rep@example.com", "seconds": 1800},
            {"name": "Terri", "email": "", "seconds": 1200},
        ]
        result, reason = unique_external_first_name_signal(
            participants,
            meeting(),
            lead(contact_name="Terri"),
            ORG_EMAILS,
        )
        self.assertIsNone(result)
        self.assertEqual(reason, "zoom_participant_missing_surname")

    def test_unique_path_rejects_second_qualifying_external_participant(self):
        participants = [
            {"name": "Rep", "email": "rep@example.com", "seconds": 1800},
            {"name": "Terri Smith", "email": "", "seconds": 1200},
            {"name": "Guest Person", "email": "", "seconds": 600},
        ]
        result, reason = unique_external_first_name_signal(
            participants,
            meeting(),
            lead(contact_name="Terri"),
            ORG_EMAILS,
        )
        self.assertIsNone(result)
        self.assertEqual(reason, "qualifying_zoom_participant_not_unique")

    def test_unique_path_ignores_short_external_join(self):
        participants = [
            {"name": "Rep", "email": "rep@example.com", "seconds": 1800},
            {"name": "Terri Smith", "email": "", "seconds": 1200},
            {"name": "Guest Person", "email": "", "seconds": 60},
        ]
        result, reason = unique_external_first_name_signal(
            participants,
            meeting(),
            lead(contact_name="Terri"),
            ORG_EMAILS,
        )
        self.assertEqual((result, reason), (
            "completed", "unique_external_first_name_match"
        ))

    def test_unique_path_rejects_multiword_lead(self):
        participants = [
            {"name": "Rep", "email": "rep@example.com", "seconds": 1800},
            {"name": "Ashley Hoeger", "email": "", "seconds": 1200},
        ]
        result, reason = unique_external_first_name_signal(
            participants,
            meeting(),
            lead(display_name="Ashley Trybus", contact_name="Ashley"),
            ORG_EMAILS,
        )
        self.assertIsNone(result)
        self.assertEqual(reason, "lead_not_single_name")

    def test_proposed_signal_promotes_only_the_guarded_exception(self):
        participants = [
            {"name": "Rep", "email": "rep@example.com", "seconds": 1800},
            {"name": "Terri Smith", "email": "", "seconds": 1200},
        ]
        result, _detail, path = proposed_zoom_signal(
            participants,
            meeting(),
            lead(contact_name="Terri"),
            {"terri@example.com"},
            ORG_EMAILS,
        )
        self.assertEqual(result, "completed")
        self.assertEqual(path, "unique_external_first_name_match")


if __name__ == "__main__":
    unittest.main()
