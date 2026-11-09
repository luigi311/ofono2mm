#!/usr/bin/env python3
"""Data collection and classification for test-modem-startup.sh.

Phosh bf5f21f exposes LogDomains on mobi.phosh.Shell.DebugControl, and its
phosh-wwan-mm / phosh-wwan-info logs report the state used by its indicator.
They do not expose Phosh's cached signal percentage or rendered bar count.
"""

import argparse
import asyncio
import csv
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
import subprocess
import sys


DEBUG_NAME = 'mobi.phosh.Shell.DebugControl'
DEBUG_PATH = '/mobi/phosh/Shell/DebugControl'
DOMAINS = ['phosh-wwan-mm', 'phosh-wwan-info']
DBUS = 'org.freedesktop.DBus'
PROPERTIES = DBUS + '.Properties'
MM = 'org.freedesktop.ModemManager1'
OUTCOMES = ('MATCH', 'PHOSH_DESYNC', 'BRIDGE_DESYNC', 'NOT_READY', 'UNKNOWN', 'ERROR')
CSV_FIELDS = ('cycle', 'timestamp', 'outcome', 'ofono_type', 'ofono_signal', 'mm_state',
              'mm_type', 'mm_signal', 'phosh_present', 'phosh_type', 'phosh_tracked', 'reason')


def write_json(path, value):
    path = Path(path)
    temporary = path.with_suffix(path.suffix + '.tmp')
    temporary.write_text(json.dumps(value, indent=2) + '\n')
    temporary.replace(path)


def journal(*args):
    result = subprocess.run(
        ['journalctl', '--boot', '--no-pager', '--output=json', *args],
        capture_output=True, text=True, timeout=10)
    if result.returncode:
        raise RuntimeError('Cannot read journal: ' + result.stderr.strip())
    return [json.loads(line) for line in result.stdout.splitlines() if line.startswith('{')]


def session_addresses():
    addresses = [os.environ.get('DBUS_SESSION_BUS_ADDRESS'),
                 f"unix:path={os.environ.get('XDG_RUNTIME_DIR', f'/run/user/{os.getuid()}')}/bus"]
    # An SSH session may not inherit the graphical session's bus address.
    for process in Path('/proc').glob('[0-9]*'):
        try:
            if process.stat().st_uid != os.getuid() or (process / 'comm').read_text().strip() != 'phosh':
                continue
            for entry in (process / 'environ').read_bytes().split(b'\0'):
                if entry.startswith(b'DBUS_SESSION_BUS_ADDRESS='):
                    addresses.append(entry.split(b'=', 1)[1].decode())
        except (OSError, UnicodeError):
            continue
    return list(dict.fromkeys(address for address in addresses if address))


async def call(bus, destination, path, interface, member, signature='', body=None):
    from dbus_fast import Message, MessageFlag, MessageType
    reply = await asyncio.wait_for(bus.call(Message(
        destination=destination, path=path, interface=interface, member=member,
        signature=signature, body=body or [], flags=MessageFlag.NO_AUTOSTART)), 5)
    if reply.message_type == MessageType.ERROR:
        raise RuntimeError(f'{reply.error_name}: ' + str(reply.body[0] if reply.body else member))
    return reply.body


async def owner(bus):
    return (await call(bus, DBUS, '/org/freedesktop/DBus', DBUS,
                       'GetNameOwner', 's', [DEBUG_NAME]))[0]


async def domains(bus):
    return (await call(bus, DEBUG_NAME, DEBUG_PATH, PROPERTIES,
                       'Get', 'ss', [DEBUG_NAME, 'LogDomains']))[0].value


async def set_domains(bus, value):
    from dbus_fast import Variant
    await call(bus, DEBUG_NAME, DEBUG_PATH, PROPERTIES,
               'Set', 'ssv', [DEBUG_NAME, 'LogDomains', Variant('as', value)])


async def disconnect(bus):
    if bus is not None:
        bus.disconnect()
        await bus.wait_for_disconnect()


async def prepare(args):
    from dbus_fast.aio import MessageBus
    failures = []
    for address in session_addresses():
        bus = None
        try:
            bus = await asyncio.wait_for(MessageBus(bus_address=address).connect(), 5)
            name_owner = await owner(bus)
            pid = (await call(bus, DBUS, '/org/freedesktop/DBus', DBUS,
                              'GetConnectionUnixProcessID', 's', [name_owner]))[0]
            if Path(f'/proc/{pid}').stat().st_uid != os.getuid():
                raise RuntimeError('Phosh belongs to a different user')
            if Path(f'/proc/{pid}/comm').read_text().strip() != 'phosh':
                raise RuntimeError('The debug interface owner is not the Phosh process')
            original = await domains(bus)
            entries = journal('-n', '1')
            if not entries or not entries[-1].get('__CURSOR'):
                raise RuntimeError('No readable journal cursor; cannot observe Phosh reliably')
            state = {'session_address': address, 'owner': name_owner,
                     'pid': pid, 'original_domains': original}
            # Save before changing settings so the Bash EXIT trap can restore them.
            write_json(args.state, state)
            await set_domains(bus, list(dict.fromkeys(original + DOMAINS)))
            print(f'Observing Phosh PID {pid} through its runtime WWAN logs.')
            return
        except Exception as error:
            failures.append(str(error))
            if Path(args.state).exists():
                raise
        finally:
            await disconnect(bus)
    raise RuntimeError('Cannot access Phosh runtime debug control: ' + '; '.join(failures))


async def restore(args):
    from dbus_fast.aio import MessageBus
    state = json.loads(Path(args.state).read_text())
    bus = await asyncio.wait_for(MessageBus(bus_address=state['session_address']).connect(), 5)
    try:
        if await owner(bus) == state['owner']:
            await set_domains(bus, state['original_domains'])
            print('Restored Phosh logging settings.', file=sys.stderr)
        else:
            print('Phosh restarted; leaving the new process logging settings alone.', file=sys.stderr)
    finally:
        await disconnect(bus)


def parse_phosh(entries):
    state = dict.fromkeys(('present', 'ui_present', 'enabled', 'sim', 'unlocked', 'technology', 'tracked_path'))
    state.update(events=0, technology_seen=False)
    for entry in entries:
        domain = entry.get('GLIB_DOMAIN')
        if domain and domain not in DOMAINS:
            continue
        message = entry.get('MESSAGE', '')
        if not isinstance(message, str):
            continue
        tracking = re.search(r'Tracking modem at: (\S+)', message)
        if tracking:
            state['tracked_path'] = tracking[1]
            state['events'] += 1
        if 'Dropping modem at:' in message or 'No modem found' in message:
            state.update(present=False, ui_present=False, enabled=False, sim=False,
                         unlocked=False, technology=None, technology_seen=True, tracked_path=None)
            state['events'] += 1
        ui_present = re.search(r'Updating wwan present: ([01])', message)
        if ui_present:
            state['ui_present'] = ui_present[1] == '1'
            state['events'] += 1
        for field, pattern in (
                ('present', r'Modem is (not )?present'),
                ('enabled', r'Modem is (not )?enabled, state:'),
                ('sim', r'SIM is (not )?present')):
            match = re.search(pattern, message)
            if match:
                state[field] = not bool(match[1])
                state['events'] += 1
        unlocked = re.search(r'SIM is (un)?locked:', message)
        if unlocked:
            state['unlocked'] = bool(unlocked[1])
            state['events'] += 1
        technology = re.search(r'Access tec is (\S+)', message)
        if technology:
            state['technology'] = None if technology[1] in ('(null)', 'NULL', '(nil)') else technology[1]
            state['technology_seen'] = True
            state['events'] += 1
    return state


def ofono_label(technology):
    return {'nr': '5G', 'lte': '4G', 'hspap': '3.75G', 'hspa': '3.5G',
            'hsupa': '3G', 'hsdpa': '3G', 'umts': '3G',
            'edge': '2.75G', 'gprs': '2.5G', 'gsm': '2G'}.get(technology)


def mm_label(technology):
    for bits, label in ((1 << 15, '5G'), (1 << 14, '4G'), (1 << 9, '3.75G'),
                        (1 << 8, '3.5G'), ((1 << 5) | (1 << 6) | (1 << 7), '3G'),
                        (1 << 4, '2.75G'), (1 << 3, '2.5G'), ((1 << 1) | (1 << 2), '2G')):
        if technology & bits:
            return label
    return None


def classify(actual, phosh, mm_path):
    ofono, modem = actual['ofono'], actual['mm']
    if (ofono.get('error') or not ofono.get('powered') or not ofono.get('online')
            or ofono.get('status') not in ('registered', 'roaming')):
        return 'NOT_READY', 'oFono is not currently online and registered'
    expected = ofono_label(ofono.get('technology'))
    if expected is None:
        return 'UNKNOWN', 'oFono technology is unsupported by this checker'
    if (modem.get('error') or modem.get('state', 0) < 8
            or mm_label(modem.get('access', 0)) != expected or modem.get('sim', '/') == '/'
            or modem.get('unlock', 0) > 1):
        return 'BRIDGE_DESYNC', 'ModemManager registration, SIM, or technology disagrees with oFono'
    if not phosh['events']:
        return 'UNKNOWN', 'No fresh Phosh WWAN log messages in this cycle'
    tracked = phosh.get('tracked_path')
    if tracked and tracked != mm_path:
        if tracked.startswith('/org/freedesktop/ModemManager1/') and '/Modem/' not in tracked:
            return 'PHOSH_DESYNC', 'Phosh selected a non-modem object'
        return 'UNKNOWN', 'Phosh is tracking another modem; check the selected paths'
    if phosh['present'] is False or phosh['ui_present'] is False:
        return 'PHOSH_DESYNC', 'Phosh reports no modem while oFono and ModemManager are registered'
    if any(phosh[field] is False for field in ('enabled', 'sim', 'unlocked')):
        return 'PHOSH_DESYNC', 'Phosh reports the modem disabled, SIM missing, or locked'
    if phosh['technology_seen'] and phosh['technology'] != expected:
        return 'PHOSH_DESYNC', 'Phosh network type disagrees with oFono and ModemManager'
    if (phosh['present'] is not True or any(phosh[field] is None for field in ('enabled', 'sim', 'unlocked'))
            or not phosh['technology_seen']):
        return 'UNKNOWN', 'Phosh WWAN telemetry is incomplete'
    return 'MATCH', 'Phosh modem tracking and network type match'


async def actual_state(args):
    from dbus_fast import BusType
    from dbus_fast.aio import MessageBus
    bus = await asyncio.wait_for(MessageBus(bus_type=BusType.SYSTEM).connect(), 5)
    actual = {'ofono': {}, 'mm': {}}
    try:
        try:
            modem, network = await asyncio.gather(
                call(bus, 'org.ofono', args.ofono_path, 'org.ofono.Modem', 'GetProperties'),
                call(bus, 'org.ofono', args.ofono_path, 'org.ofono.NetworkRegistration', 'GetProperties'))
            actual['ofono'] = {
                'powered': modem[0].get('Powered').value if 'Powered' in modem[0] else None,
                'online': modem[0].get('Online').value if 'Online' in modem[0] else None,
                **{field: network[0][name].value if name in network[0] else None
                   for field, name in (('status', 'Status'), ('technology', 'Technology'), ('strength', 'Strength'))}}
        except Exception as error:
            actual['ofono']['error'] = str(error)
        # Read only the required properties. Modem.GetAll also invokes AT-backed band getters.
        try:
            names = ('State', 'AccessTechnologies', 'SignalQuality', 'Sim', 'UnlockRequired')
            replies = await asyncio.gather(*(call(bus, MM, args.mm_path, PROPERTIES, 'Get', 'ss',
                                                 [MM + '.Modem', name]) for name in names))
            values = dict(zip(names, (reply[0].value for reply in replies)))
            actual['mm'] = {'state': values['State'], 'access': values['AccessTechnologies'],
                            'strength': values['SignalQuality'][0], 'recent': values['SignalQuality'][1],
                            'sim': values['Sim'], 'unlock': values['UnlockRequired']}
        except Exception as error:
            actual['mm']['error'] = str(error)
    finally:
        await disconnect(bus)
    return actual


async def sample(args):
    from dbus_fast.aio import MessageBus
    state = json.loads(Path(args.state).read_text())
    bus = await asyncio.wait_for(MessageBus(bus_address=state['session_address']).connect(), 5)
    try:
        if await owner(bus) != state['owner']:
            raise RuntimeError('Phosh restarted during the test; start a new run')
        active = await domains(bus)
        if 'all' not in active and not set(DOMAINS).issubset(active):
            raise RuntimeError('Phosh WWAN logging was disabled during the test')
    finally:
        await disconnect(bus)
    actual = await actual_state(args)
    cursor = Path(args.cursor).read_text().strip()
    entries = journal('--after-cursor', cursor, f"_PID={state['pid']}")
    Path(args.output).with_suffix('.journal.jsonl').write_text(
        ''.join(json.dumps(entry) + '\n' for entry in entries))
    phosh = parse_phosh(entries)
    outcome, reason = classify(actual, phosh, args.mm_path)
    write_json(args.output, {'timestamp': datetime.now(timezone.utc).isoformat(),
                            'actual': actual, 'phosh': phosh, 'outcome': outcome, 'reason': reason})


def append_csv(path, row):
    exists = Path(path).exists()
    with open(path, 'a', newline='') as output:
        writer = csv.DictWriter(output, fieldnames=CSV_FIELDS)
        if not exists:
            writer.writeheader()
        writer.writerow(row)


def record(args):
    samples = [json.loads(Path(path).read_text()) for path in args.samples]
    result = samples[-1]
    # A stable verdict must also refer to the same actual network type.
    signatures = {(item['outcome'], item['actual']['ofono'].get('technology')) for item in samples}
    if len(signatures) != 1:
        result = {**result, 'outcome': 'UNKNOWN', 'reason': 'State changed between confirmation samples'}
    row = {'cycle': args.cycle, 'timestamp': result['timestamp'], 'outcome': result['outcome'],
           'ofono_type': result['actual']['ofono'].get('technology'),
           'ofono_signal': result['actual']['ofono'].get('strength'),
           'mm_state': result['actual']['mm'].get('state'), 'mm_type': mm_label(result['actual']['mm'].get('access', 0)),
           'mm_signal': result['actual']['mm'].get('strength'), 'phosh_present': result['phosh']['present'],
           'phosh_type': result['phosh']['technology'], 'phosh_tracked': result['phosh']['tracked_path'],
           'reason': result['reason']}
    append_csv(args.csv, row)
    print(f"Cycle {args.cycle}: {row['outcome']} — {row['reason']} "
          f"[oFono={row['ofono_type']}/{row['ofono_signal']}%, "
          f"MM={row['mm_type']}/{row['mm_signal']}%, Phosh={row['phosh_type']}, present={row['phosh_present']}]")


def record_error(args):
    row = dict.fromkeys(CSV_FIELDS)
    row.update(cycle=args.cycle, timestamp=datetime.now(timezone.utc).isoformat(),
               outcome='ERROR', reason=args.reason)
    append_csv(args.csv, row)
    print(f'Cycle {args.cycle}: ERROR — {args.reason}')


def summary(args):
    counts = dict.fromkeys(OUTCOMES, 0)
    if Path(args.csv).exists():
        with open(args.csv, newline='') as source:
            for row in csv.DictReader(source):
                counts[row['outcome']] += 1
    comparable = counts['MATCH'] + counts['PHOSH_DESYNC']
    rate = 100 * counts['PHOSH_DESYNC'] / comparable if comparable else None
    result = {'attempted': args.attempts, 'completed': sum(counts.values()), 'counts': counts,
              'incomplete': max(0, args.attempts - sum(counts.values())),
              'phosh_comparable': comparable, 'phosh_desync_percent': rate,
              'scope': 'Phosh WWAN tracking/type from its own logs; exact Phosh bar count is not exposed'}
    write_json(Path(args.csv).with_name('summary.json'), result)
    rate_text = f'{rate:.1f}%' if rate is not None else 'n/a'
    print(f"Totals: attempted={args.attempts} completed={result['completed']} incomplete={result['incomplete']} "
          f"match={counts['MATCH']} "
          f"phosh_desync={counts['PHOSH_DESYNC']} bridge_desync={counts['BRIDGE_DESYNC']} "
          f"not_ready={counts['NOT_READY']} unknown={counts['UNKNOWN']} errors={counts['ERROR']} "
          f"| Phosh mismatch rate={rate_text} ({counts['PHOSH_DESYNC']}/{comparable} comparable)")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest='command', required=True)
    for name in ('prepare', 'restore', 'cursor', 'sample'):
        command = commands.add_parser(name)
        command.add_argument('--state', required=True)
        if name in ('cursor', 'sample'):
            command.add_argument('--output', required=True)
        if name == 'sample':
            command.add_argument('--cursor', required=True)
            command.add_argument('--ofono-path', required=True)
            command.add_argument('--mm-path', required=True)
    command = commands.add_parser('record')
    command.add_argument('--csv', required=True)
    command.add_argument('--cycle', required=True, type=int)
    command.add_argument('samples', nargs='+')
    command = commands.add_parser('summary')
    command.add_argument('--csv', required=True)
    command.add_argument('--attempts', type=int, default=0)
    command = commands.add_parser('error')
    command.add_argument('--csv', required=True)
    command.add_argument('--cycle', required=True, type=int)
    command.add_argument('--reason', required=True)
    args = parser.parse_args()
    try:
        if args.command == 'cursor':
            entries = journal('-n', '1')
            if not entries or not entries[-1].get('__CURSOR'):
                raise RuntimeError('No readable journal cursor')
            Path(args.output).write_text(entries[-1]['__CURSOR'])
        elif args.command == 'record':
            record(args)
        elif args.command == 'summary':
            summary(args)
        elif args.command == 'error':
            record_error(args)
        else:
            asyncio.run(globals()[args.command](args))
    except Exception as error:
        print(f'{args.command}: {error}', file=sys.stderr)
        return 1
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
