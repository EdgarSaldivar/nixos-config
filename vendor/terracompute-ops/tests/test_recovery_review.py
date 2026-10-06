from __future__ import annotations

import json
import sqlite3
import tempfile
import unittest
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

from terracompute_ops.current_state import CurrentState
from terracompute_ops.owned_work import OwnedWork
from terracompute_ops.recovery_review import RecoveryCoordinator
from terracompute_ops.spool_client import Answer, Progress
from terracompute_ops.state import StateStore
from terracompute_ops.supervisor import Supervisor
from terracompute_ops import display

BOOT_A = '11111111-2222-4333-8444-555555555555'
BOOT_B = '22222222-2222-4333-8444-555555555555'
EVENT = dict(fault_family='systemd',code='service_demo_not_active',component='demo')
REVIEW = dict(before='The demo service check failed.',after='Fresh complete samples passed for five minutes.',
              boot_relation='A different boot preceded recovery; causality is unknown.',cause='Unknown.',
              evidence=['recovery event and retained samples'],uncertainty=['No proof of permanent repair.'],
              preventive_followup='Review service logs before suggesting a preventive change.')


class Spool:
    def __init__(self):
        self.requests, self.results, self.cancellations = {}, {}, []
        self.active = False
        self.before_ack = None
    def ask(self,ticket,**kwargs):
        self.requests[ticket] = kwargs
        return True
    def peek(self,ticket,**kwargs):
        assert kwargs['owner'] == self.requests[ticket]['owner']
        return self.results.get(ticket)
    def acknowledge(self,ticket,**kwargs):
        if self.before_ack:
            self.before_ack()
        return self.results.pop(ticket,None) is not None
    def discard(self,ticket,**kwargs):
        self.results.pop(ticket,None)
    def request_cancellation(self,owner):
        self.cancellations.append(owner)
    def progress(self,owner):
        return Progress('dispatched' if self.active else 'queued',self.active,
                        (datetime.now(timezone.utc)+timedelta(minutes=10)).isoformat())
    def answer(self,text):
        ticket = list(self.requests)[-1]
        self.results[ticket] = Answer('completed',text,None)


class RecoveryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.start = self.now = datetime.now(timezone.utc).replace(microsecond=0)
        self.store = StateStore(Path(self.temp.name),clock=lambda:self.now)
        self.supervisor = Supervisor(self.store)
        self.db = sqlite3.connect(':memory:')
        self.spool = Spool()
        self.commands = []
        self.observer = SimpleNamespace(observe=self.read)
        self.review = RecoveryCoordinator(self.db,self.store.db,self.spool,clock=lambda:self.now,observer=self.observer)
    def tearDown(self):
        self.db.close()
        self.store.close()
        self.temp.cleanup()
    def read(self,command,subject):
        self.commands.append(command)
        return SimpleNamespace(text=lambda:'synthetic read output')
    def sample(self,seconds,*,boot=BOOT_A,events=(),result='pass'):
        self.now = self.start+timedelta(seconds=seconds)
        stamp = self.now.isoformat().replace('+00:00','Z')
        return self.supervisor.observe(dict(target='terracompute',machine_id='17049',source='ssh',
            boot_id=boot,boot_verified=True,observed_at=stamp,measured_at=stamp,
            healthy=not events,events=list(events),complete=True,
            snapshot={'services':{'demo':'active' if not events else 'failed'}},
            coverage=[dict(check='systemd:service_demo_not_active',resource='demo',result=result,
                           evidence_ref='/snapshot/services/demo')]))
    def recovered(self):
        self.sample(0,events=[EVENT],result='fail')
        self.sample(60,boot=BOOT_B)
        self.sample(360,boot=BOOT_B)
        self.assertEqual(len(self.store.recovery_events()),1)
    def job(self):
        return dict(self.review._rows('SELECT * FROM tc_action_recovery_jobs').fetchone())
    def test_actual_boot_recovery_is_reviewed_once_across_restart_and_replay(self):
        self.recovered()
        self.review.tick()
        self.assertEqual(self.review.projection()['phase'],'queued')
        self.spool.active = True
        self.assertEqual(self.review.projection()['phase'],'reviewing_recovery')
        request = next(iter(self.spool.requests.values()))
        self.assertEqual((request['kind'],request['requested']),('diagnose',False))
        self.assertTrue(request['investigation_id'].startswith('recovery:'))
        self.spool.answer(json.dumps(REVIEW))
        self.spool.before_ack = lambda:self.assertIsNotNone(self.job()['report_json'])
        self.review.tick()
        self.review = RecoveryCoordinator(self.db,self.store.db,self.spool,clock=lambda:self.now,observer=self.observer)
        self.db.execute('UPDATE tc_action_recovery_cursor SET event_id=0')
        self.db.commit()
        self.review.tick()
        self.assertEqual(self.db.execute('SELECT COUNT(*) FROM tc_action_recovery_jobs').fetchone()[0],1)
        self.assertEqual(len(self.spool.requests),1)
        self.assertEqual(len(self.review.undelivered()),1)
        self.review.mark_queued(self.job()['event_id'])
        self.assertEqual(self.review.undelivered(),[])
    def test_unknown_midway_requires_a_new_verification_window(self):
        self.sample(0,events=[EVENT],result='fail')
        self.sample(60,boot=BOOT_B)
        self.sample(120,boot=BOOT_B,result='unknown')
        self.sample(360,boot=BOOT_B)
        self.review.tick()
        self.assertFalse(self.spool.requests)
        self.sample(660,boot=BOOT_B)
        self.review.tick()
        self.assertEqual(len(self.spool.requests),1)
    def test_late_old_boot_report_is_cancelled_and_not_accepted(self):
        self.recovered();self.review.tick()
        self.spool.answer(json.dumps(dict(REVIEW,cause='untrusted old result')))
        self.sample(420,boot=BOOT_A)  # Retired boot cannot roll the epoch back.
        self.assertEqual(CurrentState(self.store.db,lambda:self.now).boot(),BOOT_B)
        self.sample(480,boot='33333333-2222-4333-8444-555555555555')
        self.review.tick()
        value = json.loads(self.job()['report_json'])
        self.assertNotIn('untrusted old result',json.dumps(value))
        self.assertTrue(self.spool.cancellations)
        self.assertIn('ownership changed',value['cause'])
    def test_an_action_field_is_rejected_by_the_report_only_contract(self):
        self.recovered();self.review.tick()
        self.spool.answer(json.dumps(dict(REVIEW,action={'command':'synthetic forbidden writer'})))
        self.review.tick()
        self.assertEqual(self.commands,[])
        self.assertIn('contract invalid',json.loads(self.job()['report_json'])['cause'])
    def test_two_read_rounds_share_budget_identity_and_third_is_refused(self):
        self.recovered();self.review.tick()
        for _ in range(2):
            self.spool.answer(json.dumps({'observe':['echo synthetic-read']}))
            self.review.tick();self.review.tick();self.review.tick()
        self.spool.answer(json.dumps({'observe':['echo too-many']}))
        self.review.tick()
        self.assertEqual(len(self.commands),2)
        self.assertEqual(len({r['investigation_id'] for r in self.spool.requests.values()}),1)
        self.assertIn('contract invalid',json.loads(self.job()['report_json'])['cause'])
    def test_restart_between_read_commit_and_ack_cleans_old_slot_before_next_round(self):
        self.recovered();self.review.tick()
        ticket = next(iter(self.spool.requests))
        self.spool.answer(json.dumps({'observe':['echo retained-read']}))
        class Crash(BaseException):
            pass
        def interrupted():
            raise Crash()
        self.spool.before_ack = interrupted
        with self.assertRaises(Crash):
            self.review.tick()
        self.assertEqual(self.job()['state'],'reading')
        self.assertIn(ticket,self.spool.results)
        self.spool.before_ack = None
        self.review = RecoveryCoordinator(self.db,self.store.db,self.spool,clock=lambda:self.now,observer=self.observer)
        self.review.tick();self.review.tick()
        self.assertNotIn(ticket,self.spool.results)
        self.assertEqual(self.commands,['echo retained-read'])
        self.assertEqual(len(self.spool.requests),2)

    def test_timeout_and_interrupted_read_are_explicit_unknown_without_replay(self):
        self.recovered();self.review.tick()
        self.spool.answer(json.dumps({'observe':['echo synthetic-read']}))
        self.review.tick()
        self.db.execute("UPDATE tc_action_recovery_reads SET state='running'")
        self.db.commit()
        self.review = RecoveryCoordinator(self.db,self.store.db,self.spool,clock=lambda:self.now,observer=self.observer)
        self.assertIn('unknown',self.db.execute('SELECT output FROM tc_action_recovery_reads').fetchone()[0])
        self.now += timedelta(minutes=11)
        self.review.tick()
        self.assertEqual(self.commands,[])
        self.assertIn('deadline',json.loads(self.job()['report_json'])['cause'])
    def test_current_head_and_verified_boots_ignore_archive_arrival(self):
        self.recovered()
        self.sample(420,boot=BOOT_A,events=[EVENT],result='fail')
        current = CurrentState(self.store.db,lambda:self.now)
        self.assertEqual(current.document('ssh')[0]['boot_id'],BOOT_B)
        self.assertEqual([b for b,_ in display.read_boots(Path(self.temp.name))],[BOOT_A,BOOT_B])


class WorkTests(unittest.TestCase):
    def test_result_is_saved_before_ack_and_obsolete_cache_never_replays(self):
        db = sqlite3.connect(':memory:')
        self.addCleanup(db.close)
        now = datetime.now(timezone.utc)
        valid = [True]
        work = OwnedWork(db,lambda:now,lambda context:valid[0])
        work.context = dict(loop_id=str(uuid.uuid4()),boot_id=BOOT_A,subject='synthetic',episode=1)
        spool = Spool();client = work.wrap(spool)
        self.assertIsNone(client.collect('ticket'))
        client.ask('ticket',incident_id='synthetic',evidence_hash='a'*64,prompt='synthetic',severity='info')
        spool.answer('synthetic answer')
        spool.before_ack = lambda:self.assertIsNotNone(work.row('ticket')['answer_json'])
        self.assertEqual(client.collect('ticket').text,'synthetic answer')
        self.assertEqual(client.collect('ticket').text,'synthetic answer')
        valid[0] = False
        work.reconcile()
        self.assertEqual(client.collect('ticket').text,'')
        self.assertTrue(spool.cancellations)
        self.assertEqual(len(spool.requests),1)

    def test_bounded_sweep_rotates_past_retained_uncertain_work(self):
        db = sqlite3.connect(':memory:')
        self.addCleanup(db.close)
        now = datetime.now(timezone.utc)
        checked = set()
        work = OwnedWork(db,lambda:now,lambda context:checked.add(context['index']) or True)
        for index in range(300):
            db.execute("INSERT INTO tc_action_work VALUES(?,?,?,?,'pending',NULL,0,?,?)",
                (f't{index:04}', '{}', json.dumps({'index':index}),
                 (now+timedelta(minutes=10)).isoformat(),now.isoformat(),now.isoformat()))
        db.commit()
        work.reconcile();work.reconcile()
        self.assertEqual(len(checked),300)
