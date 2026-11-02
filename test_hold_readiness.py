import copy
import os
import unittest
from unittest.mock import Mock, patch

from read_hold_readiness import has_hold_exclusion, collect
from workflow_holds import no_sales_hold_query
from close_status_policy import ORG


class QueueInventoryTests(unittest.TestCase):
    def test_only_an_unnegated_conjunction_proves_exclusion(self):
        guard = no_sales_hold_query()
        self.assertTrue(has_hold_exclusion(guard))
        self.assertTrue(has_hold_exclusion({'type': 'and', 'queries': [guard]}))
        self.assertFalse(has_hold_exclusion({'type': 'or', 'queries': [guard, {'type': 'object_type'}]}))
        self.assertFalse(has_hold_exclusion({'type': 'and', 'negate': True, 'queries': [guard]}))
        self.assertFalse(has_hold_exclusion({'type': 'and', 'queries': [{**guard, 'negate': False}]}))

    def test_only_our_views_get_candidate_patches_and_all_settings_are_preserved(self):
        client = Mock(); client.request.return_value = {'id': ORG}
        original = {'query': {'type': 'object_type', 'object_type': 'lead'},
                    'sort': [{'direction': 'asc'}], 'results_limit': 123}
        client.list.side_effect = [[{'id': 'save_ours', 'name': 'L2 · Example', 's_query': original},
            {'id': 'save_other', 'name': 'Someone else', 's_query': original}], []]
        before = copy.deepcopy(original)
        queues, changes, _ = collect(client)
        self.assertEqual(len(queues), 2); self.assertEqual(len(changes), 1)
        self.assertEqual(original, before)
        self.assertEqual(changes[0]['after']['s_query']['sort'], before['sort'])
        self.assertEqual(changes[0]['after']['s_query']['results_limit'], 123)
        self.assertEqual(changes[0]['after']['s_query']['query']['queries'][0], before['query'])
        self.assertFalse(changes[0]['production_write_authorized'])

    def test_all_locally_generated_views_exclude_hold_markers(self):
        with patch.dict(os.environ, {'CLOSE_API_KEY': 'fake'}):
            import create_lane2_views
        for view in create_lane2_views.VIEWS:
            with self.subTest(name=view[0]): self.assertEqual(has_hold_exclusion(view[2]),'Ops' not in view[0])


if __name__ == '__main__': unittest.main()
