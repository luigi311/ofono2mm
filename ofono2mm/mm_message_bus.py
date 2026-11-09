"""ObjectManager behavior expected by libmm-glib's modem discovery."""

import asyncio
import logging

from dbus_fast import Message
from dbus_fast.aio import MessageBus
from dbus_fast.service import ServiceInterface


MANAGER_PATH = '/org/freedesktop/ModemManager1'
MODEM_INTERFACE = 'org.freedesktop.ModemManager1.Modem'
OBJECT_MANAGER = 'org.freedesktop.DBus.ObjectManager'


class MMMessageBus(MessageBus):
    """Manage modem objects, while keeping SIMs and bearers directly accessible.

    dbus-fast's default ObjectManager lists every exported child and emits
    discovery signals at the child's path. MMManager expects only modems and
    listens for those signals at MANAGER_PATH. Override the library's built-in
    ObjectManager hooks; ordinary property access and signals are unaffected.
    """

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._mm_announcements = {}
        self._mm_replies = set()

    @staticmethod
    async def _properties(interface):
        future = asyncio.get_running_loop().create_future()

        def complete(_interface, values, *callback_args):
            # dbus-fast 4 adds user_data before the error argument.
            error = callback_args[-1]
            if future.done():
                return
            if error is not None:
                future.set_exception(error)
            else:
                future.set_result(values)

        ServiceInterface._get_all_property_values(interface, complete)
        return await future

    def _interfaces_at(self, path):
        interfaces = self._path_exports.get(path, {})
        if isinstance(interfaces, dict):
            return interfaces
        # Older dbus-fast releases store a list instead of a name-to-interface map.
        return {interface.name: interface for interface in interfaces}

    def _default_get_managed_objects_handler(self, msg, send_reply):
        if msg.path != MANAGER_PATH:
            return super()._default_get_managed_objects_handler(msg, send_reply)

        # Take a snapshot before awaiting any asynchronous property getters.
        objects = {path: dict(self._interfaces_at(path))
                   for path in self._path_exports
                   if path.startswith(MANAGER_PATH + '/') and MODEM_INTERFACE in self._interfaces_at(path)}

        async def reply():
            try:
                result = {}
                for path, interfaces in objects.items():
                    values = await asyncio.gather(*(self._properties(interface)
                                                    for interface in interfaces.values()))
                    result[path] = dict(zip(interfaces, values))
                # A modem can disappear while an async getter is pending.
                # Do not resurrect it in discovery after its removal signal.
                result = {
                    path: {name: value for name, value in interfaces.items()
                           if self._interfaces_at(path).get(name) is objects[path][name]}
                    for path, interfaces in result.items()
                    if self._interfaces_at(path).get(MODEM_INTERFACE) is objects[path][MODEM_INTERFACE]
                }
                if not self._disconnected:
                    send_reply(Message.new_method_return(msg, 'a{oa{sa{sv}}}', [result]))
            except Exception as error:
                send_reply.send_error(error)

        task = asyncio.create_task(reply())
        self._mm_replies.add(task)
        task.add_done_callback(self._mm_replies.discard)

    def _emit_interface_added(self, path, interface):
        if self._disconnected:
            return
        if path != MANAGER_PATH and not path.startswith(MANAGER_PATH + '/'):
            return super()._emit_interface_added(path, interface)
        if MODEM_INTERFACE not in self._interfaces_at(path):
            return

        previous = self._mm_announcements.get(path)

        async def announce():
            if previous is not None:
                await previous
            if MODEM_INTERFACE not in self._interfaces_at(path) or self._interfaces_at(path).get(interface.name) is not interface:
                return
            try:
                values = await self._properties(interface)
            except Exception:
                logging.exception('Failed to read properties for %s', interface.name)
                values = {}
            if MODEM_INTERFACE not in self._interfaces_at(path) or self._interfaces_at(path).get(interface.name) is not interface:
                return
            self.send(Message.new_signal(
                path=MANAGER_PATH, interface=OBJECT_MANAGER, member='InterfacesAdded',
                signature='oa{sa{sv}}', body=[path, {interface.name: values}]))

        task = asyncio.create_task(announce())
        self._mm_announcements[path] = task

        def finished(done):
            if self._mm_announcements.get(path) is done:
                self._mm_announcements.pop(path)

        task.add_done_callback(finished)

    def _emit_interface_removed(self, path, removed_interfaces):
        if self._disconnected:
            return
        if path != MANAGER_PATH and not path.startswith(MANAGER_PATH + '/'):
            return super()._emit_interface_removed(path, removed_interfaces)
        if MODEM_INTERFACE not in removed_interfaces and MODEM_INTERFACE not in self._interfaces_at(path):
            return
        self.send(Message.new_signal(
            path=MANAGER_PATH, interface=OBJECT_MANAGER, member='InterfacesRemoved',
            signature='oas', body=[path, removed_interfaces]))
