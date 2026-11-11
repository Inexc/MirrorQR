"""Linux ScreenCast portal session. Requires a user desktop DBus session."""
import asyncio
import os
import secrets


class PortalSession:
    def __init__(self):
        self.bus = None
        self.session_path = None
        self.fd = None

    async def open(self):
        from dbus_next import Message, MessageType, Variant
        from dbus_next.aio import MessageBus

        self.bus = await MessageBus(negotiate_unix_fd=True).connect()
        intro = await self.bus.introspect('org.freedesktop.portal.Desktop', '/org/freedesktop/portal/desktop')
        obj = self.bus.get_proxy_object('org.freedesktop.portal.Desktop', '/org/freedesktop/portal/desktop', intro)
        interface = obj.get_interface('org.freedesktop.portal.ScreenCast')
        token = 'qr_' + secrets.token_hex(12)
        result = await self._request(interface.call_create_session, {
            'session_handle_token': Variant('s', token),
        })
        self.session_path = result['session_handle'].value
        await self._request(interface.call_select_sources, self.session_path, {
            'types': Variant('u', 3),  # MONITOR | WINDOW; picker chooses exactly one.
            'multiple': Variant('b', False),
            'cursor_mode': Variant('u', 1),  # Hidden is mandatory in portal ScreenCast.
        })
        result = await self._request(interface.call_start, self.session_path, '', {})
        streams = result.get('streams')
        if streams is None or len(streams.value) != 1:
            raise RuntimeError('Portal не вернул ровно один выбранный источник.')
        node_id = int(streams.value[0][0])
        reply = await self.bus.call(Message(
            destination='org.freedesktop.portal.Desktop',
            path='/org/freedesktop/portal/desktop',
            interface='org.freedesktop.portal.ScreenCast', member='OpenPipeWireRemote',
            signature='oa{sv}', body=[self.session_path, {}],
        ))
        if reply.message_type == MessageType.ERROR:
            raise RuntimeError('Portal отказал в доступе к PipeWire remote.')
        index = reply.body[0]
        self.fd = reply.unix_fds[index]
        return self.fd, node_id

    async def _request(self, method, *args):
        from dbus_next import MessageType, Variant

        token = 'qr_' + secrets.token_hex(12)
        options = dict(args[-1])
        options['handle_token'] = Variant('s', token)
        sender = self.bus.unique_name[1:].replace('.', '_')
        path = '/org/freedesktop/portal/desktop/request/' + sender + '/' + token
        future = asyncio.get_running_loop().create_future()

        def handler(message):
            if (message.message_type == MessageType.SIGNAL and message.path == path
                    and message.interface == 'org.freedesktop.portal.Request'
                    and message.member == 'Response' and not future.done()):
                future.set_result(message.body)

        # The DBus match rule is required; a local handler alone does not subscribe.
        from dbus_next import Message
        rule = "type='signal',interface='org.freedesktop.portal.Request',path='" + path + "'"
        match = await self.bus.call(Message(
            destination='org.freedesktop.DBus', path='/org/freedesktop/DBus',
            interface='org.freedesktop.DBus', member='AddMatch', signature='s', body=[rule]))
        if match.message_type == MessageType.ERROR:
            raise RuntimeError('Не удалось подписаться на ответ portal.')
        self.bus.add_message_handler(handler)
        try:
            returned_path = await method(*args[:-1], options)
            if returned_path != path:
                raise RuntimeError('Portal вернул неожиданный путь запроса.')
            response, result = await asyncio.wait_for(future, timeout=120)
            if response != 0:
                raise RuntimeError('Выбор экрана отменён или разрешение захвата отклонено.')
            return result
        finally:
            self.bus.remove_message_handler(handler)
            try:
                await self.bus.call(Message(
                    destination='org.freedesktop.DBus', path='/org/freedesktop/DBus',
                    interface='org.freedesktop.DBus', member='RemoveMatch', signature='s', body=[rule]))
            except Exception:
                pass

    async def close(self):
        from dbus_next import Message
        if self.bus is not None:
            try:
                if self.session_path:
                    await self.bus.call(Message(
                        destination='org.freedesktop.portal.Desktop', path=self.session_path,
                        interface='org.freedesktop.portal.Session', member='Close'))
            finally:
                self.bus.disconnect()
                self.bus = None
        if self.fd is not None:
            os.close(self.fd)
            self.fd = None
