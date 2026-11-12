"""Offline checks for the manual fault injector's selection and restoration rules."""
import importlib.util
from pathlib import Path
import signal
import subprocess
from types import SimpleNamespace
import unittest
from unittest.mock import patch


SCRIPT = Path(__file__).resolve().parents[1] / 'scripts/test-mobile-data-recovery.py'
spec = importlib.util.spec_from_file_location('mobile_data_recovery', SCRIPT)
probe = importlib.util.module_from_spec(spec)
spec.loader.exec_module(probe)


class ContextSelectionTests(unittest.TestCase):
    activation = '\r\n+CGACT: 1,1\r\n+CGACT: 2,1\r\n+CGACT: 3,0\r\nOK\r\n'
    definitions = ('+CGDCONT: 1,"IPV4V6","ims","",0,0\r\n'
                   '+CGDCONT: 2,"IPV4V6","fast.t-mobile.com","",0,0\r\n'
                   '+CGDCONT: 3,"IPV4V6","fast.t-mobile.com","",0,0\r\nOK\r\n')

    def test_matches_active_internet_apn_without_selecting_ims_or_inactive_context(self):
        self.assertEqual(probe.internet_cid(self.activation, self.definitions, 'FAST.T-MOBILE.COM'), 2)

    def test_refuses_duplicate_active_apn(self):
        activation = self.activation.replace('+CGACT: 3,0', '+CGACT: 3,1')
        with self.assertRaisesRegex(RuntimeError, 'uniquely'):
            probe.internet_cid(activation, self.definitions, 'fast.t-mobile.com')

    def test_refuses_missing_or_inactive_internet_apn(self):
        for apn in ('', 'other-carrier'):
            with self.subTest(apn=apn), self.assertRaises(RuntimeError):
                probe.internet_cid(self.activation, self.definitions, apn)
        with self.assertRaises(RuntimeError):
            probe.internet_cid(self.activation.replace('+CGACT: 2,1', '+CGACT: 2,0'),
                               self.definitions, 'fast.t-mobile.com')

    def test_requires_successful_complete_at_responses(self):
        for response in ('', 'ERROR', 'OK\r\n+CME ERROR: 3', '+CGACT: 2,1'):
            with self.subTest(response=response), self.assertRaises(RuntimeError):
                probe.internet_cid(response, self.definitions, 'fast.t-mobile.com')

    def test_handles_echoes_and_spacing(self):
        self.assertEqual(probe.internet_cid(
            'AT+CGACT?\r\n+CGACT: 7, 1\r\nOK',
            'AT+CGDCONT?\r\n+CGDCONT: 7, "IPV4V6", "internet.example", ""\r\nOK',
            'internet.example'), 7)


def state(up=True, active=True, nm_state=100, bearer_interface='ccmni1'):
    return {'contexts': [{'type': 'internet', 'active': active,
                          'interface': 'ccmni1', 'link': {'up': up}}],
            'bearers': [{'connected': True, 'interface': bearer_interface}],
            'nm_state': nm_state}


class ClassificationTests(unittest.TestCase):
    def test_reproduced_state_requires_down_link_and_stale_connected_reports(self):
        self.assertEqual(probe.classify(state(up=False), False), 'STALE_CONNECTED')
        self.assertEqual(probe.classify(state(up=False, active=False), False), 'NOT_RECOVERED')
        self.assertEqual(probe.classify(state(up=False, nm_state=30), False), 'NOT_RECOVERED')
        self.assertEqual(probe.classify(state(up=False, bearer_interface='ccmni2'), False), 'NOT_RECOVERED')

    def test_backend_state_alone_does_not_count_as_working_traffic(self):
        self.assertEqual(probe.classify(state(), False), 'NOT_RECOVERED')
        self.assertEqual(probe.classify(state(), True), 'CONNECTED')
        self.assertFalse(probe.connected(state(up=False)))


class FakeRecorder:
    directory = Path('/tmp/example-results')

    def __init__(self):
        self.messages = []

    def log(self, *args, **kwargs):
        pass

    def say(self, message):
        self.messages.append(message)


class FakeProbe:
    def __init__(self, error=None, healthy=False, restores=True, restore_error=False,
                 interrupt_signal=signal.SIGINT):
        self.recorder = FakeRecorder()
        self.injected = False
        self.calls = []
        self.error = error
        self.healthy = healthy
        self.restores = restores
        self.restore_error = restore_error
        self.interrupt_signal = interrupt_signal

    def prepare(self):
        self.calls.append('prepare')

    def preflight(self):
        self.calls.append('preflight')
        if self.error == 'preflight':
            raise RuntimeError('Preflight failed')

    def inject(self):
        self.calls.append('inject')
        self.injected = True
        if self.error == 'inject-timeout':
            raise TimeoutError('Delivery uncertain')

    def observe(self, *args):
        self.calls.append('observe')
        if self.error == 'interrupt':
            raise probe.Interrupted(self.interrupt_signal)

    def snapshot(self, *args):
        return state(up=self.healthy)

    def https(self, *args):
        return self.healthy

    def restore(self):
        self.calls.append('restore')
        if self.restore_error:
            raise RuntimeError('Restoration error')
        return self.restores

    def collect(self):
        self.calls.append('collect')


class RestorationTests(unittest.TestCase):
    def execute(self, fake, **options):
        args = SimpleNamespace(mode='modem', trigger=True, restore_from=None,
                               hold=10, leave_disconnected=False)
        for name, value in options.items():
            setattr(args, name, value)
        with patch.object(probe.signal, 'signal'):
            return probe.run_probe(fake, args)

    def test_default_preflight_does_not_disconnect_or_restore(self):
        fake = FakeProbe()
        status, result = self.execute(fake, trigger=False)
        self.assertEqual((status, result['outcome']), (0, 'PREFLIGHT_ONLY'))
        self.assertEqual(fake.calls, ['prepare', 'preflight', 'collect'])

    def test_failed_preflight_never_mutates_connection(self):
        fake = FakeProbe(error='preflight')
        status, result = self.execute(fake)
        self.assertEqual((status, result['outcome']), (2, 'ERROR'))
        self.assertEqual(fake.calls, ['prepare', 'preflight', 'collect'])

    def test_timed_out_injection_still_restores_and_collects_evidence(self):
        fake = FakeProbe(error='inject-timeout')
        status, result = self.execute(fake)
        self.assertEqual(status, 2)
        self.assertTrue(result['restored'])
        self.assertEqual(fake.calls[-2:], ['restore', 'collect'])

    def test_reproduction_is_success_and_connection_is_restored(self):
        fake = FakeProbe()
        status, result = self.execute(fake)
        self.assertEqual((status, result['outcome'], result['restored']), (0, 'STALE_CONNECTED', True))
        self.assertEqual(fake.calls[-2:], ['restore', 'collect'])

    def test_successful_automatic_recovery_is_not_disconnected_again(self):
        fake = FakeProbe(healthy=True)
        status, result = self.execute(fake)
        self.assertEqual((status, result['outcome'], result['restored']), (0, 'CONNECTED', True))
        self.assertNotIn('restore', fake.calls)
        self.assertFalse(any('Leaving the session' in message for message in fake.recorder.messages))

    def test_leave_disconnected_is_only_honored_on_completed_observation(self):
        fake = FakeProbe()
        self.execute(fake, leave_disconnected=True)
        self.assertNotIn('restore', fake.calls)
        for signum in (signal.SIGINT, signal.SIGTERM, signal.SIGHUP):
            with self.subTest(signal=signum):
                fake = FakeProbe(error='interrupt', interrupt_signal=signum)
                status, result = self.execute(fake, leave_disconnected=True)
                self.assertEqual(status, 128 + signum)
                self.assertEqual(result['outcome'], 'INTERRUPTED')
                self.assertIn('restore', fake.calls)

    def test_separate_restore_does_not_require_a_working_baseline_or_inject_fault(self):
        fake = FakeProbe()
        status, result = self.execute(fake, trigger=False, restore_from=Path('/tmp/original-results'))
        self.assertEqual((status, result['outcome']), (0, 'RESTORED'))
        self.assertEqual(fake.calls, ['prepare', 'restore', 'collect'])

    def test_restoration_failure_is_nonzero_and_keeps_logs(self):
        for options in ({'restores': False}, {'restore_error': True}):
            with self.subTest(options=options):
                fake = FakeProbe(**options)
                status, result = self.execute(fake)
                self.assertEqual(status, 2)
                self.assertFalse(result['restored'])
                self.assertEqual(fake.calls[-1], 'collect')


class TrafficTests(unittest.TestCase):
    def instance(self):
        instance = probe.Probe.__new__(probe.Probe)
        instance.saved = {'interface': 'ccmni1', 'family': '6'}
        instance.recorder = FakeRecorder()
        return instance

    def test_checks_cannot_succeed_using_wifi_or_an_http_proxy(self):
        with patch.object(probe.subprocess, 'run', return_value=SimpleNamespace(
                returncode=0, stdout='200', stderr='')) as run:
            self.assertTrue(self.instance().https(state(), 'baseline'))
        command = run.call_args.args[0]
        self.assertEqual(command[command.index('--interface') + 1], 'ccmni1')
        self.assertEqual(command[command.index('--noproxy') + 1], '*')
        self.assertIn('-6', command)

    def test_transient_timeout_gets_one_bounded_retry(self):
        with patch.object(probe.subprocess, 'run', side_effect=[
                subprocess.TimeoutExpired('curl', 8),
                SimpleNamespace(returncode=0, stdout='200', stderr='')]) as run:
            self.assertTrue(self.instance().https(state(), 'baseline'))
            self.assertEqual(run.call_count, 2)

    def test_persistent_failure_never_counts_as_connected(self):
        with patch.object(probe.subprocess, 'run', return_value=SimpleNamespace(
                returncode=7, stdout='000', stderr='connection failed')) as run:
            self.assertFalse(self.instance().https(state(), 'baseline'))
            self.assertEqual(run.call_count, 2)


if __name__ == '__main__':
    unittest.main()
