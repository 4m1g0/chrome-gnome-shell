#!/usr/bin/env python

'''
    GNOME Shell integration for Chrome
    Copyright (C) 2016  Yuri Konotopov <ykonotopov@gmail.com>

    This program is free software: you can redistribute it and/or modify
    it under the terms of the GNU General Public License as published by
    the Free Software Foundation, either version 3 of the License, or
    (at your option) any later version.
'''

from __future__ import unicode_literals
from __future__ import print_function
from gi.repository import GLib, Gio
import json
import os
import re
import socket
import stat
import struct
import sys
import time
import traceback
from select import select
from tempfile import gettempdir
from threading import Thread, Lock

CONNECTOR_VERSION	= 7
DEBUG_ENABLED		= False

SHELL_SCHEMA = "org.gnome.shell"
ENABLED_EXTENSIONS_KEY = "enabled-extensions"
EXTENSION_DISABLE_VERSION_CHECK_KEY = "disable-extension-version-validation"

BUFFER_SUPPORTED = hasattr(sys.stdin, 'buffer')
mainLoop = GLib.MainLoop()
mutex = Lock()
watcherConnected = False
mainLoopInterrupted = False

proxy = Gio.DBusProxy.new_for_bus_sync(Gio.BusType.SESSION,
                                       Gio.DBusProxyFlags.NONE,
                                       None,
                                       'org.gnome.Shell',
                                       '/org/gnome/Shell',
                                       'org.gnome.Shell.Extensions',
                                       None)

# https://wiki.gnome.org/Projects/GnomeShell/Extensions/UUIDGuidelines
def isUUID(uuid):
    return uuid is not None and re.match('[-a-zA-Z0-9@._]+$', uuid) is not None


# Helper function that sends a message to the webapp.
def send_message(response):
    message = json.dumps(response)
    message_length = len(message.encode('utf-8'))

    if message_length > 1024*1024:
        logError('Too long message (%d): "%s"' % (message_length, message))
        return

    try:
        # Write message size.
        if BUFFER_SUPPORTED:
            sys.stdout.buffer.write(struct.pack(b'I', message_length))
        else:
            sys.stdout.write(struct.pack(b'I', message_length))

        # Write the message itself.
        sys.stdout.write(message)
        sys.stdout.flush()
    except IOError as e:
        logError('IOError occured: %s' % e.strerror)
        sys.exit(1)


def send_error(message):
    send_message({'success': False, 'message': message})

def debug(message):
    if DEBUG_ENABLED:
        logError(message)


def logError(message):
    print(message, file=sys.stderr)


def dbus_call_response(method, parameters, resultProperty):
    try:
        result = proxy.call_sync(method,
                                 parameters,
                                 Gio.DBusCallFlags.NONE,
                                 -1,
                                 None)

        send_message({'success': True, resultProperty: result.unpack()[0]})
    except GLib.GError as e:
        send_error(e.message)

# Thread that reads messages from the webapp.
def read_thread_func():
    while not mainLoop.is_running() and not mainLoopInterrupted:
        time.sleep(0.2)

    while mainLoop.is_running():
        rlist, _, _ = select([sys.stdin], [], [], 1)
        if rlist:
            # Read the message length (first 4 bytes).
            if BUFFER_SUPPORTED:
                text_length_bytes = sys.stdin.buffer.read(4)
            else:
                text_length_bytes = sys.stdin.read(4)
        else:
            continue

        if len(text_length_bytes) == 0:
            mainLoop.quit()
            break

        # Unpack message length as 4 byte integer.
        text_length = struct.unpack(b'i', text_length_bytes)[0]

        # Read the text (JSON object) of the message.
        if BUFFER_SUPPORTED:
            text = sys.stdin.buffer.read(text_length).decode('utf-8')
        else:
            text = sys.stdin.read(text_length).decode('utf-8')

        request = json.loads(text)

        if 'execute' in request:
            if 'uuid' in request and not isUUID(request['uuid']):
                continue

            mutex.acquire()
            debug('[%d] Execute: to %s' % (os.getpid(), request['execute']))

            if request['execute'] == 'initialize':
                settings = Gio.Settings.new(SHELL_SCHEMA)
                shellVersion = proxy.get_cached_property("ShellVersion")
                if EXTENSION_DISABLE_VERSION_CHECK_KEY in settings.keys():
                    disableVersionCheck = settings.get_boolean(EXTENSION_DISABLE_VERSION_CHECK_KEY)
                else:
                    disableVersionCheck = False

                send_message(
                    {
                        'success': True,
                        'properties': {
                            'connectorVersion': CONNECTOR_VERSION,
                            'shellVersion': shellVersion.unpack(),
                            'versionValidationEnabled': not disableVersionCheck
                        }
                    }
                )

            elif request['execute'] == 'installExtension':
                dbus_call_response("InstallRemoteExtension",
                                   GLib.Variant.new_tuple(GLib.Variant.new_string(request['uuid'])),
                                   "status")

            elif request['execute'] == 'listExtensions':
                dbus_call_response("ListExtensions", None, "extensions")

            elif request['execute'] == 'enableExtension':
                settings = Gio.Settings.new(SHELL_SCHEMA)
                uuids = settings.get_strv(ENABLED_EXTENSIONS_KEY)

                extensions = []
                if 'extensions' in request:
                    extensions = request['extensions']
                else:
                    extensions.append({'uuid': request['uuid'], 'enable': request['enable'] })

                for extension in extensions:
                    if not isUUID(extension['uuid']):
                        continue

                    if extension['enable']:
                        uuids.append(extension['uuid'])
                    elif extension['uuid'] in uuids:
                        uuids.remove(extension['uuid'])

                settings.set_strv(ENABLED_EXTENSIONS_KEY, uuids)

                send_message({'success': True})

            elif request['execute'] == 'launchExtensionPrefs':
                proxy.call("LaunchExtensionPrefs",
                           GLib.Variant.new_tuple(GLib.Variant.new_string(request['uuid'])),
                           Gio.DBusCallFlags.NONE,
                           -1,
                           None,
                           None,
                           None)

            elif request['execute'] == 'getExtensionErrors':
                dbus_call_response("GetExtensionErrors",
                                   GLib.Variant.new_tuple(GLib.Variant.new_string(request['uuid'])),
                                   "extensionErrors")

            elif request['execute'] == 'getExtensionInfo':
                dbus_call_response("GetExtensionInfo",
                                   GLib.Variant.new_tuple(GLib.Variant.new_string(request['uuid'])),
                                   "extensionInfo")

            elif request['execute'] == 'uninstallExtension':
                dbus_call_response("UninstallExtension",
                                   GLib.Variant.new_tuple(GLib.Variant.new_string(request['uuid'])),
                                   "status")

            debug('[%d] Execute: from %s' % (os.getpid(), request['execute']))
            mutex.release()


def on_shell_signal(d_bus_proxy, sender_name, signal_name, parameters):
    if signal_name == 'ExtensionStatusChanged':
        mutex.acquire()
        debug('[%d] Signal: to %s' % (os.getpid(), signal_name))
        send_message({'signal': signal_name, 'parameters': parameters.unpack()})
        debug('[%d] Signal: from %s' % (os.getpid(), signal_name))
        mutex.release()


def on_shell_appeared(connection, name, name_owner):
    global watcherConnected

    # Things get broken if we send 1st signal
    if not watcherConnected:
        watcherConnected = True
        return

    mutex.acquire()
    debug('[%d] Signal: to %s' % (os.getpid(), name))
    send_message({'signal': name})
    debug('[%d] Signal: from %s' % (os.getpid(), name))
    mutex.release()


def default_exception_hook(type, value, tb):
    logError("Uncaught exception of type %s occured" % type)
    traceback.print_tb(tb)
    logError("Exception: %s" % value)

    mainLoop.quit()


def setup_thread_excepthook():
    """
    Workaround for `sys.excepthook` thread bug from:
    http://bugs.python.org/issue1230540

    Call once from the main thread before creating any threads.
    """

    init_original = Thread.__init__

    def init(self, *args, **kwargs):

        init_original(self, *args, **kwargs)
        run_original = self.run

        def run_with_except_hook(*args2, **kwargs2):
            try:
                run_original(*args2, **kwargs2)
            except Exception:
                sys.excepthook(*sys.exc_info())

        self.run = run_with_except_hook

    Thread.__init__ = init


def main():
    debug('[%d] Startup' % (os.getpid()))

    # Set custom exception hook
    setup_thread_excepthook()
    sys.excepthook = default_exception_hook

    """
    We should listen GNOME Shell events only in one instance.
    Use local socket to determine if another instance already running.
    """
    lock_socket = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
    lock_socket_address = '\0chrome-gnome-shell-%d' % os.getppid()

    try:
        """
        Abstract local sockets only supported in Linux.
        Fallback to filesystem local socket in *BSD.
        """
        if not 'linux' in sys.platform.lower():
            lock_socket_address = '%s/chrome-gnome-shell-%d' % (gettempdir(), os.getppid())

            # Try to cleanup from unexpected shutdown
            if os.path.lexists(lock_socket_address):
                if os.path.isfile(lock_socket_address) or os.path.islink(lock_socket_address):
                    debug('[%d] File %s exists. Unlinking.' % (os.getpid(), lock_socket_address))
                    os.unlink(lock_socket_address)
                elif stat.S_ISSOCK(os.stat(lock_socket_address).st_mode):
                    debug('[%d] local socket %s is exists.' % (os.getpid(), lock_socket_address))
                    try:
                        lock_socket.connect(lock_socket_address)
                        lock_socket.close()
                    except socket.error as e:
                        debug('[%d] Local socked is abandoned. Unlinking.' % os.getpid())
                        os.unlink(lock_socket_address)

        lock_socket.bind(lock_socket_address)
        debug('[%d] Local socket %s obtained' % (os.getpid(), lock_socket_address.replace('\0', '[NUL]')))

        shellAppearedId = Gio.bus_watch_name(Gio.BusType.SESSION,
                                             'org.gnome.Shell',
                                             Gio.BusNameWatcherFlags.NONE,
                                             on_shell_appeared,
                                             None)
        shellSignalId = proxy.connect('g-signal', on_shell_signal)
    except socket.error:
        debug('[%d] Local socket already bound' % (os.getpid()))
        lock_socket = False

    appLoop = Thread(target=read_thread_func)
    appLoop.start()

    try:
        mainLoop.run()
    except KeyboardInterrupt:
        mainLoop.quit()

    mainLoopInterrupted = True

    if lock_socket:
        proxy.disconnect(shellSignalId)
        Gio.bus_unwatch_name(shellAppearedId)

        lock_socket.close()

        # Cleanup filesystem local socket
        if lock_socket_address[0] != '\0':
            debug('[%d] Unlinking local socket' % (os.getpid()))
            os.unlink(lock_socket_address)

    appLoop.join()
    debug('[%d] Quit' % (os.getpid()))
    sys.exit(0)


if __name__ == '__main__':
    main()
