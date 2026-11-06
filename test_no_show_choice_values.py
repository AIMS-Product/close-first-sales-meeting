import unittest

from no_show_recovery import NO_SHOW_RECOVERY_FIELD, custom_value, plan_writes


class NoShowChoiceTests(unittest.TestCase):
    def lead(self, value):
        return {'id': 'lead_fixture', 'status_id': 'stat_normal', 'num_upcoming_meetings': 0,
                'custom.' + NO_SHOW_RECOVERY_FIELD: value}

    def test_existing_list_choice_does_not_repeat_activation(self):
        lead = self.lead(['Yes'])
        eligible, writes, _ = plan_writes({lead['id']}, {lead['id']: lead}, [lead], NO_SHOW_RECOVERY_FIELD)
        self.assertEqual(eligible, {lead['id']})
        self.assertEqual(writes, [])

    def test_expired_list_choice_still_clears(self):
        lead = self.lead(['Yes'])
        _, writes, _ = plan_writes(set(), {}, [lead], NO_SHOW_RECOVERY_FIELD)
        self.assertEqual(writes, [(lead, {'custom.' + NO_SHOW_RECOVERY_FIELD: 'No'}, 'clear')])

    def test_scalar_and_nested_choice_inputs_remain_supported(self):
        self.assertEqual(custom_value(self.lead('Yes'), NO_SHOW_RECOVERY_FIELD), 'Yes')
        self.assertEqual(custom_value({'custom': {NO_SHOW_RECOVERY_FIELD: ['No']}}, NO_SHOW_RECOVERY_FIELD), 'No')


if __name__ == '__main__':
    unittest.main()
