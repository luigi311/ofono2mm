#!/usr/bin/env python3
"""Manually reproduce and observe a stalled mobile-data session on a FuriPhone.

Run from the phone terminal or over Wi-Fi/USB SSH. The modem test uses the
FuriLabs AT interface to drop the active internet PDP context, after matching
its APN to an active modem CID. It can leave cellular data unusable until
restoration. It does not install or modify ofono2mm.

Examples:
  python3 scripts/test-mobile-data-recovery.py
      Read-only preflight: verify the session and cellular IPv6 HTTPS.
  python3 scripts/test-mobile-data-recovery.py --trigger --hold 60
      Drop the modem session, observe for 60 seconds, then restore data.
  python3 scripts/test-mobile-data-recovery.py --trigger --mode ofono
      Control test: disconnect through oFono and observe automatic recovery.
  python3 scripts/test-mobile-data-recovery.py --trigger --leave-disconnected
      Leave the session for manual inspection; toggle data in Phosh afterward.
  python3 scripts/test-mobile-data-recovery.py --restore-from /path/to/results
      Restore the session saved by an earlier run.

Ctrl+C, SIGTERM and SIGHUP attempt restoration after an injection. SIGKILL and
power loss cannot be handled. Results include private telephony debug logs;
review them before sharing. MODEM_DEBUG=true in the running bridge improves
the journal capture. The script does not enable logging or restart services.

Requires python3-dbus, python3-gi, curl and systemd's journalctl.
The normal phone user used for Phosh can usually access oFono. Restoration
tries oFono first, then NetworkManager; authorization failures are reported.
"""

import argparse
import csv
from datetime import datetime
import json
import os
from pathlib import Path
import re
import shlex
import signal
import subprocess
import sys
import tempfile
import time


MM = 'org.freedesktop.ModemManager1'
NM = 'org.freedesktop.NetworkManager'
PROPERTIES = 'org.freedesktop.DBus.Properties'
CONTEXT = 'org.ofono.ConnectionContext'
CONNECTION_MANAGER = 'org.ofono.ConnectionManager'
AT = 'org.ofono.FuriLabs.AT'


def at_lines(response):
    lines = [line.strip() for line in str(response).splitlines() if line.strip()]
    if not lines or lines[-1] != 'OK':
        raise RuntimeError('Modem command failed: ' + str(response).strip())
    return lines[:-1]


def internet_cid(activation, definitions, apn):
    """Select exactly one active modem context with the internet context's APN."""
    active = set()
    for line in at_lines(activation):
        match = re.fullmatch(r'\+CGACT:\s*(\d+)\s*,\s*([01])', line)
        if match and match[2] == '1':
            active.add(int(match[1]))
    matches = []
    for line in at_lines(definitions):
        if not line.startswith('+CGDCONT:'):
            continue
        fields = next(csv.reader([line.split(':', 1)[1]], skipinitialspace=True))
        if len(fields) < 3 or not fields[0].isdigit():
            raise RuntimeError('Malformed modem context definition')
        cid = int(fields[0])
        if apn and fields[2].casefold() == apn.casefold() and cid in active:
            matches.append(cid)
    if len(matches) != 1:
        raise RuntimeError('Cannot uniquely match the internet APN to an active modem CID')
    return matches[0]


def context_interface(properties):
    return str(properties.get('Settings', {}).get('Interface')
               or properties.get('IPv6.Settings', {}).get('Interface') or '')


def link_state(interface):
    if not interface or '/' in interface or interface in ('.', '..'):
        return {'up': False}
    path = Path('/sys/class/net') / interface
    try:
        return {'up': bool(int((path / 'flags').read_text().strip(), 16) & 1),
                'operstate': (path / 'operstate').read_text().strip()}
    except OSError as error:
        return {'up': False, 'error': str(error)}


def connected(state):
    internet = [context for context in state.get('contexts', [])
                if context['type'] == 'internet' and context['active']
                and context['interface'] and context['link']['up']]
    return (len(internet) == 1 and state.get('nm_state') == 100
            and any(bearer['connected'] and bearer['interface'] == internet[0]['interface']
                    for bearer in state.get('bearers', [])))


def classify(state, https_ok):
    if connected(state) and https_ok:
        return 'CONNECTED'
    internet = [context for context in state.get('contexts', []) if context['type'] == 'internet']
    if (not https_ok and state.get('nm_state') == 100 and len(internet) == 1
            and internet[0]['active'] and not internet[0]['link']['up']
            and any(bearer['connected'] and bearer['interface'] == internet[0]['interface']
                    for bearer in state.get('bearers', []))):
        return 'STALE_CONNECTED'
    return 'NOT_RECOVERED'


def plain(value):
    if isinstance(value, dict):
        return {str(key): plain(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [plain(item) for item in value]
    if type(value).__name__ == 'Boolean':
        return bool(value)
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    return str(value)


class Recorder:
    def __init__(self, directory):
        self.directory = directory
        self.started = time.monotonic()
        self.events = (directory / 'events.jsonl').open('w', buffering=1)

    def log(self, kind, **fields):
        record = {'time': datetime.now().astimezone().isoformat(),
                  'elapsed': round(time.monotonic() - self.started, 3),
                  'kind': kind, **plain(fields)}
        self.events.write(json.dumps(record) + '\n')

    def say(self, message):
        self.log('message', text=message)
        try:
            print(message, flush=True)
        except (BrokenPipeError, OSError):
            pass  # Continue restoring data even if the SSH terminal disappeared.


class Probe:
    def __init__(self, args, recorder):
        # Keep --help and parser/unit tests usable on a computer without D-Bus bindings.
        import dbus
        from dbus.mainloop.glib import DBusGMainLoop
        from gi.repository import GLib
        self.dbus = dbus
        DBusGMainLoop(set_as_default=True)
        self.bus = dbus.SystemBus()
        self.glib = GLib.MainContext.default()
        self.args, self.recorder = args, recorder
        self.saved = {}
        self.cursor = None
        self.injected = False

    def interface(self, name, path, interface):
        return self.dbus.Interface(self.bus.get_object(name, path, introspect=False), interface)

    def properties(self, name, path, interface):
        return self.interface(name, path, PROPERTIES).GetAll(interface, timeout=4)

    def contexts(self):
        return self.interface('org.ofono', self.saved['ofono_path'], CONNECTION_MANAGER).GetContexts(timeout=4)

    def no_calls(self):
        calls = self.interface('org.ofono', self.saved['ofono_path'],
                               'org.ofono.VoiceCallManager').GetCalls(timeout=4)
        if calls:
            raise RuntimeError('A phone call is active; no fault will be injected')

    def device(self):
        manager = self.interface(NM, '/org/freedesktop/NetworkManager', NM)
        devices = [str(path) for path in manager.GetDevices(timeout=4)
                   if str(self.properties(NM, path, NM + '.Device').get('Interface'))
                   == self.saved['ofono_path']]
        if len(devices) != 1:
            raise RuntimeError('Expected exactly one NetworkManager device for this modem')
        return devices[0]

    def at(self, command):
        response = str(self.interface('org.ofono', self.saved['ofono_path'], AT)
                       .SendCommand(command + '\r\n', timeout=12))
        self.recorder.log('at', command=command, response=response)
        at_lines(response)
        return response

    def prepare(self):
        if self.args.restore_from:
            self.saved = json.loads((self.args.restore_from / 'session.json').read_text())
            if self.saved.get('version') != 1:
                raise RuntimeError('Unsupported saved session format')
        else:
            self.saved = {'version': 1, 'ofono_path': self.args.ofono_path, 'family': self.args.family}
            contexts = [(str(path), properties) for path, properties in self.contexts()
                        if str(properties.get('Type')) == 'internet' and properties.get('Active')]
            if len(contexts) != 1:
                raise RuntimeError('Expected exactly one active internet context')
            path, properties = contexts[0]
            self.saved.update(context_path=path, interface=context_interface(properties),
                              apn=str(properties.get('AccessPointName', '')))
            device = self.properties(NM, self.device(), NM + '.Device')
            if int(device['State']) != 100:
                raise RuntimeError('NetworkManager cellular connection is not activated')
            active = str(device['ActiveConnection'])
            self.saved['connection'] = str(self.properties(NM, active, NM + '.Connection.Active')['Connection'])
            objects = self.interface(MM, '/org/freedesktop/ModemManager1',
                                     'org.freedesktop.DBus.ObjectManager').GetManagedObjects(timeout=4)
            modems = [str(path) for path, interfaces in objects.items()
                      if MM + '.Modem' in interfaces
                      and str(interfaces[MM + '.Modem'].get('Device')) == self.args.ofono_path]
            if len(modems) != 1:
                raise RuntimeError('Expected exactly one matching ModemManager modem')
            self.saved['mm_path'] = modems[0]
        self.no_calls()
        # A restore file must still refer to an internet context with the same APN.
        properties = self.interface('org.ofono', self.saved['context_path'], CONTEXT).GetProperties(timeout=4)
        if (str(properties.get('Type')) != 'internet'
                or str(properties.get('AccessPointName', '')) != self.saved['apn']):
            raise RuntimeError('Saved internet context no longer matches this phone')
        (self.recorder.directory / 'session.json').write_text(json.dumps(self.saved, indent=2) + '\n')
        cursor = subprocess.run(['journalctl', '--no-pager', '-n', '1', '-o', 'json'],
                                capture_output=True, text=True, timeout=10)
        try:
            self.cursor = json.loads(cursor.stdout)['__CURSOR']
        except (ValueError, KeyError):
            self.recorder.say('Journal cursor unavailable; D-Bus events will still be recorded.')
        self.subscribe()

    def subscribe(self):
        def ofono_change(name, value, path=None, interface=None):
            if ((str(path) == self.saved['context_path'] and str(name) in ('Active', 'Settings', 'IPv6.Settings'))
                    or (str(path) == self.saved['ofono_path']
                        and str(name) in ('Interfaces', 'Online', 'Powered', 'Attached', 'Suspended'))):
                self.recorder.log('signal', source=interface, path=path, property=name, value=value)

        def mm_change(interface, changed, invalidated, path=None):
            if str(interface) == MM + '.Bearer':
                kept = {str(key): value for key, value in changed.items()
                        if str(key) in ('Connected', 'Interface', 'Ip4Config', 'Ip6Config', 'Suspended')}
                self.recorder.log('signal', source=interface, path=path, changed=kept, invalidated=invalidated)

        self.bus.add_signal_receiver(ofono_change, signal_name='PropertyChanged', bus_name='org.ofono',
                                     path_keyword='path', interface_keyword='interface')
        self.bus.add_signal_receiver(mm_change, signal_name='PropertiesChanged', bus_name=MM,
                                     dbus_interface=PROPERTIES, path_keyword='path')
        self.bus.add_signal_receiver(lambda new, old, reason: self.recorder.log(
            'signal', source='NM.StateChanged', new=new, old=old, reason=reason),
            signal_name='StateChanged', bus_name=NM, dbus_interface=NM + '.Device', path=self.device())

    def snapshot(self, label):
        state = {'label': label}
        try:
            state['contexts'] = [{'path': str(path), 'type': str(p.get('Type', '')),
                                  'active': bool(p.get('Active')), 'interface': context_interface(p),
                                  'link': link_state(context_interface(p))}
                                 for path, p in self.contexts()]
        except Exception as error:
            state['ofono_error'] = str(error)
        try:
            modem = self.properties(MM, self.saved['mm_path'], MM + '.Modem')
            state['modem_state'] = int(modem['State'])
            state['bearers'] = []
            for path in modem['Bearers']:
                p = self.properties(MM, path, MM + '.Bearer')
                state['bearers'].append({'path': str(path), 'connected': bool(p['Connected']),
                                         'interface': str(p['Interface'])})
        except Exception as error:
            state['mm_error'] = str(error)
        try:
            state['nm_state'] = int(self.properties(NM, self.device(), NM + '.Device')['State'])
        except Exception as error:
            state['nm_error'] = str(error)
        self.recorder.log('snapshot', **state)
        return state

    def https(self, state, label):
        interfaces = [context['interface'] for context in state.get('contexts', [])
                      if context['type'] == 'internet' and context['active'] and context['interface']]
        interface = interfaces[0] if interfaces else self.saved['interface']
        for attempt in (1, 2):
            try:
                result = subprocess.run(['curl', '--noproxy', '*', '-' + self.saved['family'],
                                         '--interface', interface, '--connect-timeout', '4', '--max-time', '6',
                                         '--silent', '--show-error', '--output', '/dev/null', '--write-out',
                                         '%{http_code}', 'https://example.com/'],
                                        capture_output=True, text=True, timeout=8)
                self.recorder.log('cellular_https', label=label, interface=interface, attempt=attempt,
                                  exit=result.returncode, http=result.stdout, stderr=result.stderr)
                if result.returncode == 0 and result.stdout.strip() == '200':
                    return True
            except subprocess.TimeoutExpired:
                self.recorder.log('cellular_https', label=label, interface=interface,
                                  attempt=attempt, error='curl process timed out')
        return False

    def observe(self, seconds, label, stop_connected=False):
        deadline = time.monotonic() + seconds
        next_sample = 0
        while time.monotonic() < deadline:
            while self.glib.pending():
                self.glib.iteration(False)
            if time.monotonic() >= next_sample:
                state = self.snapshot(label)
                if stop_connected and connected(state) and self.https(state, label):
                    return True
                next_sample = time.monotonic() + (1 if stop_connected else 5)
            time.sleep(0.05)
        return False

    def preflight(self):
        state = self.snapshot('before')
        if not connected(state) or not self.https(state, 'before'):
            raise RuntimeError('Cellular HTTPS preflight failed; no fault was injected')
        if self.args.mode == 'modem':
            self.saved['cid'] = internet_cid(self.at('AT+CGACT?'), self.at('AT+CGDCONT?'), self.saved['apn'])
            (self.recorder.directory / 'session.json').write_text(json.dumps(self.saved, indent=2) + '\n')
        self.recorder.say('Preflight passed: cellular HTTPS works; internet context is ' + self.saved['context_path'] + '.')

    def inject(self):
        self.no_calls()
        if self.args.mode == 'modem':
            # The modem may have reassigned its context IDs during preflight.
            current = self.interface('org.ofono', self.saved['context_path'], CONTEXT).GetProperties(timeout=4)
            if not current.get('Active') or str(current.get('AccessPointName', '')) != self.saved['apn']:
                raise RuntimeError('Internet context changed during preflight; no fault was injected')
            self.saved['cid'] = internet_cid(self.at('AT+CGACT?'), self.at('AT+CGDCONT?'), self.saved['apn'])
        self.recorder.say('Dropping the internet session through ' + self.args.mode + '.')
        self.injected = True  # A timed-out method may already have changed the modem.
        if self.args.mode == 'modem':
            self.at('AT+CGACT=0,' + str(self.saved['cid']))
        else:
            self.interface('org.ofono', self.saved['context_path'], CONTEXT).SetProperty(
                'Active', self.dbus.Boolean(False, variant_level=1), timeout=12)

    def restore(self):
        self.recorder.say('Restoring mobile data through oFono...')
        context = self.interface('org.ofono', self.saved['context_path'], CONTEXT)
        for active in (False, True):
            try:
                context.SetProperty('Active', self.dbus.Boolean(active, variant_level=1), timeout=35)
                if self.observe(12, 'restore-ofono', stop_connected=True):
                    self.recorder.say('Restored: cellular HTTPS succeeded.')
                    return True
            except Exception as error:
                self.recorder.log('restore_error', method='ofono', error=str(error))
        try:
            self.recorder.say('Trying the original NetworkManager profile...')
            manager = self.interface(NM, '/org/freedesktop/NetworkManager', NM)
            device = self.device()
            active = self.properties(NM, device, NM + '.Device')['ActiveConnection']
            if str(active) != '/':
                manager.DeactivateConnection(active, timeout=12)
            manager.ActivateConnection(self.dbus.ObjectPath(self.saved['connection']),
                                       self.dbus.ObjectPath(device), self.dbus.ObjectPath('/'), timeout=12)
            if self.observe(30, 'restore-nm', stop_connected=True):
                self.recorder.say('Restored: cellular HTTPS succeeded.')
                return True
        except Exception as error:
            self.recorder.log('restore_error', method='networkmanager', error=str(error))
        self.recorder.say('Restoration could not be verified. Toggle mobile data off/on in Phosh.')
        return False

    def collect(self):
        self.snapshot('final')
        if self.cursor:
            try:
                with (self.recorder.directory / 'journal.log').open('w') as output:
                    result = subprocess.run(['journalctl', '--no-pager', '--after-cursor', self.cursor,
                                             '-o', 'short-iso-precise'], stdout=output,
                                            stderr=subprocess.PIPE, text=True, timeout=20)
                (self.recorder.directory / 'journal-errors.txt').write_text(result.stderr)
            except Exception as error:
                self.recorder.log('journal_error', error=str(error))


class Interrupted(Exception):
    def __init__(self, signum):
        self.signum = signum


def interrupted(signum, _frame):
    raise Interrupted(signum)


def run_probe(probe, args):
    """Always restore an attempted fault unless an intentional leave completed."""
    result = {'mode': args.mode, 'outcome': 'ERROR', 'restored': None}
    leave = False
    restore_requested = False
    status = 0
    try:
        probe.prepare()
        if args.restore_from:
            # Run restoration below with terminal signals masked throughout.
            restore_requested = True
            result['outcome'] = 'RESTORE_REQUESTED'
        else:
            probe.preflight()
            result['outcome'] = 'PREFLIGHT_ONLY'
            if args.trigger:
                probe.inject()
                probe.recorder.say(f'Observing for {args.hold}s; inspect Phosh now. Ctrl+C restores data.')
                probe.observe(args.hold, 'observe')
                state = probe.snapshot('after-observation')
                ok = probe.https(state, 'after-observation')
                result['outcome'] = classify(state, ok)
                if result['outcome'] == 'CONNECTED':
                    result['restored'] = True
                probe.recorder.say('Test result: ' + result['outcome'])
                leave = args.leave_disconnected
    except Interrupted as error:
        status = 128 + error.signum
        result['outcome'] = 'INTERRUPTED'
        probe.recorder.say('Interrupted; restoring data before exit.')
    except Exception as error:
        status = 2
        result['outcome'] = 'ERROR'
        result['error'] = str(error)
        probe.recorder.log('error', error=str(error))
        probe.recorder.say('Error: ' + str(error))
    finally:
        # Another terminal signal must not interrupt the restoration sequence.
        for signum in (signal.SIGINT, signal.SIGTERM, signal.SIGHUP):
            signal.signal(signum, signal.SIG_IGN)
        if (probe.injected or restore_requested) and not leave and result['restored'] is not True:
            try:
                result['restored'] = probe.restore()
            except Exception as error:
                result['restored'] = False
                result['restoration_error'] = str(error)
                probe.recorder.say('Restoration failed: ' + str(error) + '. Toggle data off/on in Phosh.')
            if not result['restored']:
                status = 2
            if result['outcome'] == 'RESTORE_REQUESTED':
                result['outcome'] = 'RESTORED' if result['restored'] else 'RESTORE_FAILED'
        elif probe.injected and leave:
            probe.recorder.say('Leaving the session for manual inspection. Toggle data off/on in Phosh, or run:\n'
                               + shlex.join(['python3', sys.argv[0], '--restore-from', str(probe.recorder.directory)]))
        try:
            probe.collect()
        except Exception as error:
            probe.recorder.log('collection_error', error=str(error))
    return status, result


def arguments(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    action = parser.add_mutually_exclusive_group()
    action.add_argument('--trigger', action='store_true', help='Actually disconnect the internet session.')
    action.add_argument('--restore-from', type=Path, metavar='RESULTS', help='Restore an earlier saved session.')
    parser.add_argument('--mode', choices=('modem', 'ofono'), default='modem', help='Injection layer (default: modem).')
    parser.add_argument('--hold', type=int, default=45, metavar='SECONDS', help='Observation window, 10–300 seconds (default: 45).')
    parser.add_argument('--leave-disconnected', action='store_true', help='Leave the fault active after observation for manual inspection.')
    parser.add_argument('--ofono-path', default='/ril_0', help='Selected oFono modem (default: /ril_0).')
    parser.add_argument('--family', choices=('4', '6'), default='6', help='Native cellular HTTPS address family (default: 6).')
    parser.add_argument('--output', type=Path, help='New results directory (default: a private directory in /tmp).')
    args = parser.parse_args(argv)
    if not 10 <= args.hold <= 300:
        parser.error('--hold must be between 10 and 300 seconds')
    if args.leave_disconnected and not args.trigger:
        parser.error('--leave-disconnected requires --trigger')
    return args


def main(argv=None):
    args = arguments(argv)
    os.umask(0o077)
    if args.output:
        args.output.mkdir(mode=0o700)
        directory = args.output.resolve()
    else:
        directory = Path(tempfile.mkdtemp(prefix='ofono-data-recovery-'))
    recorder = Recorder(directory)
    recorder.say('Results: ' + str(directory))
    try:
        probe = Probe(args, recorder)
        for signum in (signal.SIGINT, signal.SIGTERM, signal.SIGHUP):
            signal.signal(signum, interrupted)
        status, result = run_probe(probe, args)
        (directory / 'result.json').write_text(json.dumps(result, indent=2) + '\n')
        return status
    except Exception as error:
        recorder.say('Cannot run test: ' + str(error))
        return 2
    finally:
        recorder.events.close()


if __name__ == '__main__':
    sys.exit(main())
