"""Close-only follow-up policy. No I/O or implicit write authorization."""
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import hashlib
import json
from pathlib import Path

ORG = 'orga_Qwbx9tyUrl7Jdjurg9xJ8N6w1MN4PD2NQOgIhd8r7rC'
COMPANY = '88a28173-2a61-4cbb-92ea-f6b8be4592f3'
OWNER = 'cf_gOfS9pFwext58oberEegLyix8hZzeHrxhCZOVh3P3rd'
HOT = 'stat_kY1aMGKOui3jjlgniY2LQWMadXN78cr7vTHVMPDCliy'
GENERAL = 'stat_mRxbAkfEqzEcmEF2Z5CkucMQocXAbwzs0hSlD0SzHEh'
NONRESPONSIVE = 'stat_l8ATqabgtlrL7EKL5o0EL8ufBg8UMPxMJJP1AXI9V9i'
LEGACY = 'stat_Mf5jDseVsiPfAZw1VkjOAQFT9skT4xFucnHKwWDZevV'
DNC = 'stat_U9MI7pqsvIjceTv3pCU7b1EghO8Q83h1HUcL6fGVyi6'
OPP_HOT = 'stat_mApYfeCdMszhTCCp6TJMWBRCRtmMilVyvaMpVlzbZZR'
OPP_GENERAL = 'stat_oyR6irMMbv9KIigS8VEmX44DmXMhUut3EL57pFsExPF'
OPP_NONRESPONSIVE = 'stat_bhmaw9aSACxeEIW9wjdE9dnSBMyQEWpZBSZZRdg0fcK'
SALES_PIPELINE = 'pipe_78hyBUVS7IKikGEmstObu1'
OPP_TARGETS = {HOT: OPP_HOT, GENERAL: OPP_GENERAL, NONRESPONSIVE: OPP_NONRESPONSIVE}
FOLLOW_UP = frozenset((HOT, GENERAL, NONRESPONSIVE, LEGACY))
HELD_HASH = '2eccf7a2ad015da2d0672ea6e670111e21d29c7b51584e45a7a146f2d48ac6ba'
WINDOW = timedelta(days=7)


def timestamp(value):
    parsed = datetime.fromisoformat(value.replace('Z', '+00:00'))
    if parsed.tzinfo is None:
        raise ValueError('Timezone required')
    return parsed.astimezone(timezone.utc)


def held_hash(lead_id):
    return hashlib.sha256(lead_id.encode()).hexdigest()


def load_held_leads(path=None):
    path = path or Path(__file__).with_name('close_status_held_leads.json')
    manifest = json.loads(Path(path).read_text())
    hashes = manifest['lead_id_sha256']
    digest = hashlib.sha256('\n'.join(sorted(hashes)).encode()).hexdigest()
    if manifest['count'] != 75 or len(set(hashes)) != 75 or digest != HELD_HASH:
        raise ValueError('Held-lead manifest missing, incomplete or changed')
    return frozenset(hashes)


def load_exclusions():
    """Keep the 75 explicit holds and 82 changed records blocked pending review."""
    original = load_held_leads()
    raw = json.loads(Path(__file__).with_name('close_status_deferred_leads.json').read_text())
    hashes = raw['lead_id_sha256']
    digest = hashlib.sha256('\n'.join(sorted(hashes)).encode()).hexdigest()
    if raw['count'] != 82 or len(set(hashes)) != 82 or digest != 'a0c225a5f0a87e14174a22a3e6b7452970316cff0d6183a4177dd03b0a0f472b':
        raise ValueError('Changed-record exclusion manifest missing or changed')
    if original & set(hashes):
        raise ValueError('Exclusion manifests overlap unexpectedly')
    return original | frozenset(hashes)


@dataclass(frozen=True)
class Roster:
    closer_ids: frozenset
    captured_at: datetime
    source_receipt: dict | None = None

    def valid(self, now):
        return bool(self.closer_ids) and timedelta(0) <= now - self.captured_at < timedelta(hours=24)


@dataclass(frozen=True)
class Opportunity:
    id: str
    status: str
    updated_at: datetime
    status_type: str
    label: str
    owner: str | None
    pipeline: str


@dataclass(frozen=True)
class Evidence:
    lead_id: str
    status: str
    owners: tuple
    opportunities: tuple = ()
    latest_inbound: datetime | None = None
    suppressions: tuple = ()
    review_reasons: tuple = ()
    future_meeting: bool = False
    complete: bool = True


@dataclass(frozen=True)
class Decision:
    kind: str
    reason: str
    lead_status: str | None = None
    opportunity_id: str | None = None
    opportunity_status: str | None = None
    basis: str = ""
    owner_ids: tuple = ()

    @property
    def changes(self):
        return self.kind == 'change'


def decide(evidence, roster, held, now):
    """Order protections before all HOT, consolidation and repair decisions."""
    if held_hash(evidence.lead_id) in held:
        return Decision('hold', 'explicit_held_lead_exclusion')
    if not roster.valid(now):
        return Decision('hold', 'active_closer_roster_unavailable_or_stale')
    if set(evidence.owners) & roster.closer_ids:
        return Decision('hold', 'active_closer_owner')
    if len(evidence.owners) > 1:
        return Decision('hold', 'ambiguous_owner')
    if not evidence.complete:
        return Decision('hold', 'incomplete_close_evidence')
    if evidence.status not in FOLLOW_UP:
        return Decision('hold', 'protected_or_out_of_scope_status')
    if evidence.suppressions:
        return Decision('hold', 'suppression:' + '|'.join(sorted(set(evidence.suppressions))))
    if evidence.review_reasons:
        return Decision('hold', 'review:' + '|'.join(sorted(set(evidence.review_reasons))))
    if evidence.future_meeting:
        return Decision('hold', 'upcoming_meeting')
    for opp in evidence.opportunities:
        label = opp.label.lower()
        if opp.status_type == 'won' or 'won' in label or 'do not contact' in label or 'dnc' == label.strip():
            return Decision('hold', 'won_or_do_not_contact_opportunity')
        if opp.status_type == 'active' and opp.owner in roster.closer_ids:
            return Decision('hold', 'active_closer_opportunity_owner')
    winner = max(evidence.opportunities, key=lambda opp: opp.updated_at, default=None)
    if winner:
        if sum(opp.updated_at == winner.updated_at for opp in evidence.opportunities) != 1:
            return Decision('hold', 'controlling_opportunity_tie')
        if winner.pipeline != SALES_PIPELINE or winner.status not in OPP_TARGETS.values():
            return Decision('hold', 'unrelated_or_conflicting_controlling_opportunity')
    if evidence.latest_inbound and evidence.latest_inbound > now:
        return Decision('hold', 'future_inbound_timestamp')
    fresh = evidence.latest_inbound is not None and evidence.latest_inbound > now - WINDOW
    if evidence.status == LEGACY:
        if winner and winner.status != OPP_NONRESPONSIVE:
            return Decision('hold', 'legacy_status_conflicts_with_opportunity')
        target, reason = NONRESPONSIVE, 'consolidate_legacy_status'
    elif fresh:
        target, reason = HOT, 'recent_inbound'
    elif evidence.status == HOT:
        if winner and winner.status not in (OPP_HOT, OPP_GENERAL):
            return Decision('hold', 'hot_expiry_conflicts_with_opportunity')
        target, reason = GENERAL, 'seven_day_inbound_expiry'
    else:
        # Existing sync owns other mismatches; only latent HOT is ours to repair.
        if winner and winner.status not in (OPP_HOT, OPP_TARGETS[evidence.status]):
            return Decision('hold', 'non_hot_mismatch_for_existing_sync')
        target, reason = evidence.status, 'prevent_stale_hot_sync_reversion'
    opp_target = OPP_TARGETS[target]
    change_opp = winner is not None and winner.status != opp_target
    change_lead = evidence.status != target
    if not change_opp and not change_lead:
        return Decision('keep', 'recent_inbound' if fresh else 'already_consistent')
    return Decision('change', reason, target if change_lead else None,
                    winner.id if change_opp else None, opp_target if change_opp else None,
                    hashlib.sha256(repr(evidence).encode()).hexdigest(), evidence.owners)
