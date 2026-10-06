"""Coordinator regressions using the actual migrated collector and service stores."""
from __future__ import annotations

import json
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from terracompute_ops.diagnosing import SpoolDiagnoser, SpoolConversation, SpoolReviewer
from terracompute_ops.diagnosis import ProposedAction
from terracompute_ops.monitor_restart import ActorError
from terracompute_ops.state import StateStore
from terracompute_ops.supervisor import Supervisor
from terracompute_ops.spool_client import Answer

import test_action_service as service_fixture
from test_recovery_review import BOOT_A, BOOT_B, EVENT, REVIEW, Spool


class ReconciliationTests(unittest.TestCase):
    def setUp(self):
        self.fixture = f = service_fixture.ActionServiceTests('runTest')
        f.setUp()
        f.state_db.close()
        f.clock.value = datetime.now(timezone.utc).replace(microsecond=0)
        self.start = f.clock()
        self.store = StateStore(Path(f.temp.name)/'collector',clock=f.clock)
        f.state_db = self.store.db
        f.state_path = self.store.db_path
        f.actor.status_changes['boot_id'] = BOOT_A
        f.service = f.build_service()
        self.service = f.service
        self.supervisor = Supervisor(self.store)
        self.spool = Spool()
        self.service.diagnoser = SpoolDiagnoser(self.spool)
        self.sample(0,events=[EVENT],result='fail')
        self.key = f.state_db.execute('SELECT dedup_key FROM incidents').fetchone()[0]
    def tearDown(self):
        self.fixture.tearDown()
    def sample(self,seconds,*,boot=BOOT_A,events=(),result='pass'):
        f = self.fixture
        f.clock.value = self.start+timedelta(seconds=seconds)
        f.actor.status_changes['boot_id'] = boot
        stamp = f.clock().isoformat().replace('+00:00','Z')
        self.supervisor.observe(dict(target='terracompute',machine_id='17049',source='ssh',
            boot_id=boot,boot_verified=True,observed_at=stamp,measured_at=stamp,
            healthy=not events,events=list(events),complete=True,
            snapshot={'services':{'demo':'active' if not events else 'failed'}},
            coverage=[dict(check='systemd:service_demo_not_active',resource='demo',result=result,
                           evidence_ref='/snapshot/services/demo')]))
    def diagnose(self):
        return self.service._diagnose('',self.key,1,self.service.adapter.status(),self.fixture.clock())
    def test_first_tick_retires_old_boot_work_even_when_paused_and_preserves_transcript(self):
        self.assertTrue(self.diagnose().pending)
        loop = self.service.observations.open(self.key,1)
        facts = json.loads(loop['facts_json'])
        self.assertEqual(loop['code'],EVENT['code'])
        self.assertEqual(facts['condition']['resource'],'demo')
        self.assertTrue(facts['timeline'])
        self.service.observations.ask(loop['loop_id'],1,('echo retained-read',),'synthetic',self.fixture.clock())
        self.service.schedule.set('diagnosis:orphan',self.fixture.clock()+timedelta(hours=1))
        self.service.controls.set('paused','yes',4242,self.fixture.clock())
        self.sample(60,boot=BOOT_B)
        self.service.tick()
        db = self.fixture.actions_db
        self.assertEqual(db.execute('SELECT state FROM tc_action_observe_loops').fetchone()[0],'obsolete')
        read = db.execute('SELECT command,output FROM tc_action_observe_reads').fetchone()
        self.assertEqual(read[0],'echo retained-read')
        self.assertIsNotNone(read[1])
        self.assertIsNone(self.service.schedule.get('diagnosis:orphan')[0])
        self.assertTrue(self.spool.cancellations)
        projection = json.loads(db.execute('SELECT document_json FROM tc_action_agent_projection').fetchone()[0])
        self.assertEqual(projection['phase'],'verifying_recovery')
        self.assertEqual(self.service.observations.started_since(self.start-timedelta(seconds=1)),1)
        self.assertEqual([op for op,_ in self.fixture.actor.calls].count('restart'),0)
        self.assertEqual(self.service.phase_failures,0,self.fixture.reports)
    def test_recovery_review_starts_on_recover_without_active_incidents_and_cannot_act(self):
        self.sample(60,boot=BOOT_B);self.sample(360,boot=BOOT_B)
        self.service.controls.set('paused','yes',4242,self.fixture.clock())
        self.service.recover()
        self.assertEqual(len(self.spool.requests),1)
        request = next(iter(self.spool.requests.values()))
        self.assertFalse(request['requested'])
        self.assertEqual(request['kind'],'diagnose')
        self.spool.answer(json.dumps(REVIEW))
        self.service.tick()
        self.service.tick()  # Existing outbox delivers the durable report.
        self.assertTrue(any('Recovery review' in message for _,message,_ in self.fixture.telegram.sent))
        self.assertEqual([op for op,_ in self.fixture.actor.calls].count('restart'),0)
        self.assertEqual(self.fixture.actions_db.execute('SELECT COUNT(*) FROM tc_action_cycles').fetchone()[0],0)
        self.assertEqual(self.service.phase_failures,0,self.fixture.reports)
    def test_late_owned_answer_is_withheld_after_boot_change(self):
        self.diagnose()
        ticket = next(iter(self.spool.requests))
        self.spool.results[ticket] = Answer('completed','old answer with old authority',None)
        self.sample(60,boot=BOOT_B,events=[EVENT],result='fail')
        self.service._reconcile_work()
        self.assertEqual(self.service.diagnoser.spool.collect(ticket).text,'')
        self.assertFalse(self.service._seen_current(self.key,1))
        self.assertTrue(self.diagnose().pending)
        self.assertEqual(len(self.spool.requests),2)
        self.assertEqual(self.service.observations.started_since(self.start-timedelta(seconds=1)),2)
        # Volatile time/rental details do not fork this generation or its budget.
        self.sample(120,boot=BOOT_B,events=[EVENT],result='fail')
        self.diagnose()
        self.assertEqual(len(self.spool.requests),2)
        identities = {r['investigation_id'] for r in self.spool.requests.values()}
        self.assertEqual(len(identities),1)
    def test_unknown_coverage_waits_instead_of_diagnosing_a_hardware_fault(self):
        self.sample(60,result='unknown')
        self.service.tick()
        projection = json.loads(self.fixture.actions_db.execute('SELECT document_json FROM tc_action_agent_projection').fetchone()[0])
        self.assertEqual(projection['phase'],'waiting_for_evidence')
        self.assertFalse(self.spool.requests)
        for _ in range(4):
            self.fixture.clock.value += timedelta(seconds=15)
            self.service.tick()
        self.assertEqual(self.service.observations.started_since(self.start),0)
        self.assertEqual(self.service.phase_failures,0,self.fixture.reports)

    def test_operator_rounds_share_generation_and_end_with_the_conversation(self):
        self.service.conversation = SpoolConversation(self.spool)
        self.service.conversations.start('root',incident_key=self.key,episode=1,bdf='',
            subject_hash='a'*64,investigation_id='synthetic-chat',sender_id=4242,
            question='Review the evidence.',now=self.fixture.clock())
        for message in ('first round','follow-up'):
            ticket = self.service._ask_conversation('root',incident_key=self.key,episode=1,bdf='',
                message=message,sender_id=4242,subject_hash='a'*64,investigation_id='synthetic-chat')
            self.service.conversations.advance('root',ticket,self.fixture.clock())
            self.spool.results[ticket] = Answer('completed','synthetic accepted prose',None)
            self.service.conversation.collect(ticket)
        owners = [r['owner'] for r in self.spool.requests.values()]
        self.assertEqual(len({o.loop_id for o in owners}),1)
        self.service.conversations.end('root')
        self.service._reconcile_work()
        self.assertEqual(self.service.conversation.spool.collect(ticket).text,'')

    def test_old_boot_reviewer_approval_is_withheld_without_a_cycle(self):
        self.service.reviewer = SpoolReviewer(self.spool)
        action = ProposedAction('docker restart node-exporter','restart monitoring','true',
                                ('true',),'Restart monitoring.','Brief monitoring interruption.')
        old_binding = self.service._action_binding(action,'',self.key,1)
        self.service._queue_review(action,headline='synthetic finding',question='',bdf='',
            incident_key=self.key,episode=1,now=self.fixture.clock())
        owner = next(iter(self.spool.requests.values()))['owner']
        self.assertEqual((owner.incident_id,owner.episode_id),(self.key,1))
        self.spool.answer('VERDICT: approve\nSynthetic review.')
        self.sample(60,boot=BOOT_B,events=[EVENT],result='fail')
        self.service._reconcile_work()
        self.service._collect_reviews()
        self.assertNotEqual(self.service._action_binding(action,'',self.key,1),old_binding)
        self.assertEqual(self.fixture.actions_db.execute('SELECT COUNT(*) FROM tc_action_cycles').fetchone()[0],0)
        self.assertTrue(self.spool.cancellations)

    def test_fresh_target_boot_must_match_the_review_even_before_collector_catches_up(self):
        self.fixture.actor.status_changes['boot_id'] = BOOT_B
        with self.assertRaises(ActorError):
            self.service._execution_boot(BOOT_A)
        self.assertEqual([op for op,_ in self.fixture.actor.calls].count('restart'),0)

    def test_full_status_marks_expired_accepted_input_stale(self):
        self.fixture.clock.value += timedelta(minutes=13)
        self.assertIn('stale',self.service._latest_full_status_text(self.fixture.clock()).lower())
