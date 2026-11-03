"""Durable SMS lifecycle coordinator; write approval is explicit and bounded."""
from dataclasses import asdict, replace
from datetime import datetime, timedelta, timezone
import hashlib
import json

from close_status_policy import OWNER, timestamp
from sms_rest_policy import (Calendar, Plan, admit, transition, return_to_rep, protection, owners,
                             TERMINAL_SEQUENCE)
from workflow_holds import BATCH, OWNER_SINCE, custom


def digest(value):
    return hashlib.sha256(json.dumps(value,sort_keys=True,default=str,separators=(',',':')).encode()).hexdigest()


def fence(snapshot):
    return digest({'lead':snapshot.lead, 'evidence':asdict(snapshot.evidence),
                   'task':snapshot.task,'signal_at':snapshot.signal_at,'signal_id':snapshot.signal_id,
                   'holds':snapshot.holds,'sales':snapshot.sales_subscriptions,'unknown':snapshot.unknown_subscriptions})


class Workflow:
    def __init__(self, reader, writer, store, roster, *, calendar=Calendar(), clock=None, launch_at=None):
        self.reader,self.writer,self.store,self.roster = reader,writer,store,roster
        self.calendar = calendar
        self.clock = clock or (lambda: datetime.now(timezone.utc))
        self.launch_at = launch_at

    def propose(self, lead_id, *, task_id=None, target_owner=None, eligible_reps=()):
        now = self.clock()
        cycle = self.store.cycle(lead_id)
        snapshot = self.reader.read(lead_id,now,cycle=cycle,task_id=task_id)
        if cycle is None:
            plan = admit(snapshot,self.roster,now,self.calendar,launch_at=self.launch_at)
        elif cycle['phase'] == 'return_requested':
            plan = (return_to_rep(cycle,snapshot,self.roster,now,target_owner,eligible_reps)
                    if target_owner else Plan('keep','lane2_capacity_or_routing_receipt_pending',cycle))
        elif cycle['phase'] == 'recovery':
            plan = Plan('excluded','uncertain_operation_requires_reconciliation',cycle)
        else:
            plan = transition(cycle,snapshot,self.roster,now)
        return {'lead_id':lead_id,'proposed_at':now.isoformat(),'task_id':task_id,
                'target_owner':target_owner,'eligible_reps':list(eligible_reps),
                'snapshot_fence':fence(snapshot),'plan':asdict(plan)}

    def apply(self, proposal, *, authorization):
        self._authorize(proposal,authorization)
        lead_id, now = proposal['lead_id'], self.clock()
        with self.store.serialized():
            if self.store.uncertain(lead_id): raise RuntimeError('Uncertain writes require read-only reconciliation')
            cycle = self.store.cycle(lead_id)
            snapshot = self.reader.read(lead_id,now,cycle=cycle,task_id=proposal.get('task_id'))
            if fence(snapshot) != proposal['snapshot_fence']:
                return {'state':'held','reason':'fresh_eligibility_changed'}
            plan = Plan(**proposal['plan'])
            if plan.action in ('excluded','keep'): return {'state':plan.action,'reason':plan.reason}
            if plan.cycle is None: raise ValueError('Cycle state required')
            planned = dict(plan.cycle)
            baseline = {'suppressions':list(snapshot.evidence.suppressions)}
            planned['evidence_baseline'] = baseline
            planned['evidence_through'] = now.isoformat()
            # State-only transitions are committed without any Close mutation.
            if not plan.lead_patch and not plan.task_patch and not plan.pause_subscriptions:
                self.store.save(planned)
                return {'state':'verified','action':plan.action,'close_puts':0}
            if plan.action == 'rest':
                # Rest starts at the actual operation, never retroactively at task creation.
                planned['rest_started_at'] = now.isoformat()
                planned['rest_expires_at'] = (now+timedelta(days=90)).isoformat()
                planned['due_at'] = planned['rest_expires_at']
            recovery = dict(planned,phase='recovery',due_at=None,
                            intended_phase=planned['phase'],pending_plan=asdict(plan))
            # Persist both the operation and exclusions before the first network write.
            self.store.save(recovery)
            if plan.action == 'rest':
                self.store.acquire(lead_id,'sms_rest',planned['token'],now.isoformat(),
                                   planned['rest_expires_at'],{'original_batch':planned['original_batch']})
            expected_lead = dict(snapshot.lead)
            baseline_cycle = dict(cycle or planned,evidence_baseline=baseline,evidence_through=now.isoformat())
            operations = [(f'/sequence_subscription/{sid}/',{'status':'paused'}) for sid in plan.pause_subscriptions]
            if plan.task_patch: operations.append((f"/task/{planned['task_id']}/",plan.task_patch))
            if plan.lead_patch: operations.append((f'/lead/{lead_id}/',plan.lead_patch))
            for path,patch in operations:
                fresh = self.reader.read(lead_id,self.clock(),cycle=baseline_cycle)
                reason = protection(fresh,self.roster,self.clock(),planned)
                if reason: raise RuntimeError('Protected before write: '+reason)
                if (owners(fresh.lead) != owners(expected_lead)
                        or custom(fresh.lead,OWNER_SINCE) != custom(expected_lead,OWNER_SINCE)
                        or fresh.lead['status_id'] != expected_lead['status_id']
                        or custom(fresh.lead,BATCH) != custom(expected_lead,BATCH)
                        or fresh.signal_at != snapshot.signal_at):
                    raise RuntimeError('Concurrent lead change; operation retained for reconciliation')
                if plan.action == 'rest' and path.startswith('/lead/'):
                    if fresh.sales_subscriptions or fresh.unknown_subscriptions:
                        raise RuntimeError('Sales workflow still active or unknown; rest commit blocked')
                self._put_once(lead_id,planned['token'],path,patch)
                if path.startswith('/lead/'):
                    expected_lead.update(patch)
            final = self.reader.client.request('GET',f'/lead/{lead_id}/')
            if plan.lead_patch and any(self._value(final,key) != value for key,value in plan.lead_patch.items()):
                raise RuntimeError('Final status/owner/batch readback differs')
            # Own hold is removed only after the rep assignment and projection verify.
            if plan.release_hold: self.store.release(lead_id,'sms_rest',planned['token'])
            planned['last_verified_owner_since'] = custom(final,OWNER_SINCE)
            self.store.save(planned)
            return {'state':'verified','action':plan.action,'close_puts':len(operations)}

    def _authorize(self, proposal, authorization):
        if not self.writer.enabled: raise PermissionError('SMS production writes disabled')
        now = self.clock()
        if (not authorization or authorization.get('proposal_sha256') != digest(proposal)
                or proposal['lead_id'] not in authorization.get('allowed_lead_ids',[])
                or timestamp(authorization['expires_at']) <= now
                or now-timestamp(proposal['proposed_at']) > timedelta(minutes=10)):
            raise PermissionError('Fresh exact pilot approval required')
        required = ('shared_store_and_all_writers','all_dial_queues','sales_dispatch_and_enrollment',
                    'business_calendar','lane2_routing','schema_and_tenant')
        if not all(authorization.get('verified_integrations',{}).get(key) for key in required):
            raise PermissionError('Deployment integration receipts incomplete')
        from hold_rollout import validate_manifest
        validate_manifest(authorization.get('hold_deployment_manifest'),now)
        if self.launch_at is None: raise PermissionError('Explicit launch cutoff required')

    def _value(self, obj, key):
        value = custom(obj,key[7:]) if key.startswith('custom.') else obj.get(key)
        return value

    def _put_once(self, lead_id, token, path, patch):
        write_id = self.store.intent(lead_id,token,{'path':path,'patch':patch,'at':self.clock().isoformat()})
        self.writer.put(path,patch)
        current = self.reader.client.request('GET',path)
        if any(self._value(current,key) != value for key,value in patch.items()):
            # Close may normalize task date precision; compare timestamps explicitly.
            if not (set(patch)=={'assigned_to','date'} and current.get('assigned_to')==patch['assigned_to']
                    and timestamp(current.get('due_date') or current['date'])==timestamp(patch['date'])):
                raise RuntimeError('Unverified write; reconcile before retry')
        self.store.receipt(write_id)

    def reconcile(self, lead_id):
        """Read-only provider reconciliation. Never retries or compensates a write."""
        results=[]
        with self.store.serialized():
            for record in self.store.uncertain(lead_id):
                intent=record['payload']; current=self.reader.client.request('GET',intent['path'])
                matches=all(self._value(current,key)==value for key,value in intent['patch'].items())
                results.append({'write_id':record['id'],'target_matches':matches,'intent':intent})
                if matches: self.store.receipt(record['id'])
        return results

    def observe(self, events, through):
        """Commit native event revisions before checkpoint. No Close writes here."""
        from sms_rest_close import opted_out, signal_from_event
        with self.store.serialized():
            for event in events:
                key=event['id']+':'+event['date_updated']+':'+digest(event.get('data'))
                if self.store.connection.execute('SELECT event_id FROM workflow_event WHERE event_id=?',(key,)).fetchone():
                    continue
                lead_id=event.get('lead_id'); cycle=self.store.cycle(lead_id) if lead_id else None
                data=event.get('data') or {}
                terminal=(event.get('object_type')=='task' and event.get('action')=='created'
                          and data.get('text','').strip()=='VP THE END' and data.get('sequence_id')==TERMINAL_SEQUENCE)
                if not cycle and not terminal: continue
                payload={'event':event,'terminal_pending':terminal and not cycle}
                if not cycle:
                    self.store.connection.execute('INSERT OR IGNORE INTO workflow_event VALUES(?,?,?)',
                        (key,through.isoformat(),json.dumps(payload)))
                    continue
                if opted_out(event):
                    cycle.setdefault('evidence_baseline',{}).setdefault('suppressions',[]).append('stop_dnc_event')
                if event.get('object_type','').startswith('activity.'):
                    lead=self.reader.client.request('GET',f'/lead/{lead_id}/')
                    if event.get('action') != 'deleted' and event.get('data') is None:
                        raise RuntimeError('Event payload missing; checkpoint held')
                    signal=signal_from_event(event,lead,self.reader.registration_types)
                    if signal and (not cycle.get('signal_at') or signal>timestamp(cycle['signal_at'])):
                        cycle.update(signal_at=signal.isoformat(),signal_id=event['id'])
                        if cycle['phase'] in ('rest','grace'): cycle['due_at']=through.isoformat()
                cycle['evidence_through']=through.isoformat()
                self.store.save(cycle)
                self.store.connection.execute('INSERT OR IGNORE INTO workflow_event VALUES(?,?,?)',
                    (key,through.isoformat(),json.dumps(payload)))
            self.store.connection.execute("INSERT INTO workflow_meta VALUES('event_through',?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                                           (through.isoformat(),))
