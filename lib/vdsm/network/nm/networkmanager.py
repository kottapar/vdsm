# Copyright 2016 Red Hat, Inc.
#
# This program is free software; you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation; either version 2 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program; if not, write to the Free Software
# Foundation, Inc., 51 Franklin Street, Fifth Floor, Boston, MA 02110-1301 USA
#
# Refer to the README and COPYING files for full details of the license
#
from __future__ import absolute_import

from vdsm.commands import execCmd
from vdsm.utils import CommandPath, memoized

from .nmdbus import NMDbus
from .nmdbus.active import NMDbusActiveConnections
from .nmdbus.device import NMDbusDevice
from .nmdbus.settings import NMDbusSettings

SYSTEMCTL = CommandPath('systemctl', '/bin/systemctl', '/usr/bin/systemctl')
NM_SERVICE = 'NetworkManager'


@memoized
def is_running():
    rc, out, err = execCmd([SYSTEMCTL.cmd, 'status', NM_SERVICE])
    return rc == 0


def init():
    NMDbus.init()


class Device(object):

    def __init__(self, device_name):
        self._nm_settings = NMDbusSettings()
        self._nm_act_connections = NMDbusActiveConnections()

        nm_device = NMDbusDevice()
        self._device = nm_device.device(device_name)

    def connections(self):
        for connection_path in self._device.connections_path:
            yield self._nm_settings.connection(connection_path)

    @property
    def active_connection(self):
        nm_act_cons = self._nm_act_connections
        ac_path = self._device.active_connection_path
        return nm_act_cons.connection(ac_path) if ac_path != '/' else None

    def cleanup_inactive_connections(self):
        """
        Remove all non active connection that are associated with the device,
        leaving only the active connection.
        """
        for connection in self._non_active_connections():
            connection.delete()

    def _non_active_connections(self):
        active_connection = self.active_connection
        for connection_path in self._device.connections_path:
            connection = self._nm_settings.connection(connection_path)
            if (not active_connection or
                    connection.connection.uuid != active_connection.uuid):
                yield self._nm_settings.connection(connection_path)
