from datetime import datetime, timedelta, timezone
import unittest

from hold_rollout import SURFACES, pending_manifest, validate_manifest


def fake_ready_manifest(now):
    """Offline witness fixture; never saved as production readiness evidence."""
    manifest = pending_manifest(now)
    for row in manifest['surfaces'].values():
        row.update(state='verified', observedAt=now.isoformat(),
                   contractSha256=manifest['contractSha256'], evidenceSha256='a' * 64)
    return manifest


class ManifestTests(unittest.TestCase):
    def setUp(self): self.now = datetime.now(timezone.utc)

    def test_complete_current_witness_passes(self):
        validate_manifest(fake_ready_manifest(self.now), self.now)

    def test_pending_capture_is_not_activation_permission(self):
        with self.assertRaises(PermissionError): validate_manifest(pending_manifest(self.now), self.now)

    def test_every_required_surface_is_mandatory(self):
        for surface in SURFACES:
            manifest = fake_ready_manifest(self.now); del manifest['surfaces'][surface]
            with self.subTest(surface=surface), self.assertRaises(PermissionError): validate_manifest(manifest, self.now)

    def test_wrong_tenant_or_old_contract_cannot_pass(self):
        for key in ('companyId', 'organizationId', 'contractSha256'):
            manifest = fake_ready_manifest(self.now); manifest[key] = 'wrong'
            with self.subTest(key=key), self.assertRaises(PermissionError): validate_manifest(manifest, self.now)

    def test_stale_and_future_observations_block(self):
        for offset in (timedelta(days=-2), timedelta(minutes=1)):
            manifest = fake_ready_manifest(self.now)
            manifest['surfaces']['dial_queues']['observedAt'] = (self.now + offset).isoformat()
            with self.subTest(offset=offset), self.assertRaises(PermissionError): validate_manifest(manifest, self.now)

    def test_evidence_hash_cannot_be_missing(self):
        manifest = fake_ready_manifest(self.now); manifest['surfaces']['sales_dispatch']['evidenceSha256'] = ''
        with self.assertRaises(PermissionError): validate_manifest(manifest, self.now)


if __name__ == '__main__': unittest.main()
