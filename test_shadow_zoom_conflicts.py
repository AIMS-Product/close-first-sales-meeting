import unittest

import shadow_zoom_conflicts as shadow


ORG_EMAILS = {"rep@modern-amenities.com"}


class EmailAliasTests(unittest.TestCase):
    def test_numeric_email_alias_matches_isaacd(self):
        self.assertEqual("isaacd", shadow.email_alias("99isaacd@gmail.com"))
        seconds = shadow.alias_attendance_seconds(
            [{"name": "Isaacd", "email": "", "seconds": 6708}],
            {"isaacd"},
            ORG_EMAILS,
        )
        self.assertEqual(6708, seconds)

    def test_short_exact_alias_matches_tso(self):
        self.assertEqual("tso", shadow.email_alias("tso0410@gmail.com"))
        seconds = shadow.alias_attendance_seconds(
            [{"name": "Tso", "email": "", "seconds": 2480}],
            {"tso"},
            ORG_EMAILS,
        )
        self.assertEqual(2480, seconds)

    def test_alias_can_recover_participant_with_different_external_email(self):
        outcome, detail, source = shadow.shadow_zoom_signal(
            [
                {
                    "name": "Isaacd",
                    "email": "different@example.net",
                    "seconds": 6708,
                },
                {
                    "name": "Robin Perkins",
                    "email": "rep@modern-amenities.com",
                    "seconds": 6856,
                },
            ],
            {"99isaacd@gmail.com"},
            ORG_EMAILS,
            ["Isaac Dodds"],
            {"isaacd"},
        )
        self.assertEqual("completed", outcome)
        self.assertEqual("zoom-verified-identity", source)
        self.assertIn("6708s", detail)

    def test_one_word_first_name_requires_supporting_email_alias(self):
        seconds, reasons = shadow.verified_identity_attendance(
            [{"name": "Isaac", "email": "", "seconds": 6884}],
            {"isaacd"},
            ORG_EMAILS,
            ["Isaac Dodds"],
        )
        self.assertEqual(6884, seconds)
        self.assertEqual({"first-name-plus-email-alias"}, reasons)

    def test_same_surname_counts_a_verified_household_attendee(self):
        seconds, reasons = shadow.verified_identity_attendance(
            [{"name": "Johannes Specks", "email": "", "seconds": 2207}],
            set(),
            ORG_EMAILS,
            ["Markus Specks"],
        )
        self.assertEqual(2207, seconds)
        self.assertEqual({"same-surname"}, reasons)

    def test_different_surname_remains_rejected(self):
        seconds, reasons = shadow.verified_identity_attendance(
            [{"name": "Ashley Hoeger", "email": "", "seconds": 2400}],
            set(),
            ORG_EMAILS,
            ["Ashley Trybus"],
        )
        self.assertEqual(0, seconds)
        self.assertEqual(set(), reasons)

    def test_role_and_too_short_aliases_are_ignored(self):
        self.assertIsNone(shadow.email_alias("info@example.com"))
        self.assertIsNone(shadow.email_alias("ms@example.com"))

    def test_alias_mismatch_does_not_count(self):
        seconds = shadow.alias_attendance_seconds(
            [{"name": "Ashley Hoeger", "email": "", "seconds": 2400}],
            {"ashleytrybus"},
            ORG_EMAILS,
        )
        self.assertEqual(0, seconds)


class ConflictResolutionTests(unittest.TestCase):
    def resolve(self, **overrides):
        values = {
            "zoom_outcome": "no_show",
            "zoom_detail": "host present, prospect absent",
            "zoom_source": "zoom",
        }
        values.update(overrides)
        return shadow.resolve_shadow_conflict(**values)

    def test_positive_zoom_stays_first(self):
        outcome, source, _ = self.resolve(
            zoom_outcome="completed",
            zoom_detail="prospect on for 900s",
            attention_outcome="completed",
        )
        self.assertEqual(("completed", "zoom"), (outcome, source))

    def test_attention_analysis_vetoes_host_only_no_show(self):
        outcome, source, _ = self.resolve(
            attention_outcome="completed",
            attention_detail="matching meeting analysis",
        )
        self.assertEqual(("completed", "attention-over-zoom"), (outcome, source))

    def test_answered_phone_vetoes_host_only_no_show(self):
        outcome, source, _ = self.resolve(
            phone_outcome="completed",
            phone_detail="answered call 600s",
        )
        self.assertEqual(("completed", "phone-over-zoom"), (outcome, source))

    def test_disposition_only_conflict_routes_to_review(self):
        outcome, source, _ = self.resolve(
            disposition_outcome="completed",
            disposition_detail="New Call Show",
        )
        self.assertEqual(("review", "zoom-disposition-conflict"), (outcome, source))

    def test_uncontradicted_host_only_stays_no_show(self):
        outcome, source, _ = self.resolve()
        self.assertEqual(("no_show", "zoom"), (outcome, source))

    def test_reviewed_override_only_changes_allowlisted_target(self):
        base = {
            "shadow_outcome": "review",
            "shadow_source": "zoom-disposition-conflict",
            "shadow_detail": "conflict",
        }
        markus = shadow.reviewed_outcome(
            "acti_uIMvr0JeN1pm9fMEJbVyn47L4g4VLu3dhY90wo6gCuo", base
        )
        unrelated = shadow.reviewed_outcome("acti_other", base)
        self.assertEqual("completed", markus["shadow_outcome"])
        self.assertEqual("reviewed-override", markus["shadow_source"])
        self.assertEqual("review", unrelated["shadow_outcome"])


if __name__ == "__main__":
    unittest.main()
