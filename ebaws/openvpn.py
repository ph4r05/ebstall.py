#!/usr/bin/env python
# -*- coding: utf-8 -*-

from __future__ import print_function
import os
import logging
import collections
import re
import util
import subprocess
import types
import osutil


__author__ = 'dusanklinec'
logger = logging.getLogger(__name__)


CONFIG_LINE_BLANK = 0
CONFIG_LINE_COMMENT = 1
CONFIG_LINE_CMD_COMMENT = 2
CONFIG_LINE_CMD = 3


class ConfigLine(object):
    """
    # One open vpn config line
    """
    def __init__(self, idx=None, raw=None, ltype=None, cmd=None, params=None, comment=None, *args, **kwargs):
        self.idx = idx
        self._raw = raw
        self.ltype = ltype
        self.cmd = cmd
        self.params = params if params is not None else ''
        self.comment = comment if comment is not None else ''

    @property
    def raw(self):
        """
        Builds raw config line
        :return:
        """
        if self.ltype in [CONFIG_LINE_COMMENT, CONFIG_LINE_BLANK]:
            return self._raw

        res = '' if self.ltype == CONFIG_LINE_CMD else ';'
        res += '%s %s %s' % (self.cmd, self.params, self.comment)
        return res

    @raw.setter
    def raw(self, val):
        self._raw = val

    @classmethod
    def build(cls, line, idx=0):
        line = line.strip()
        cl = cls(idx=idx, raw=line)

        if line is None or len(line.strip()) == 0:
            cl.ltype = CONFIG_LINE_BLANK
            return cl

        cmt_match = re.match(r'^\s*#.*', line)
        if cmt_match is not None:
            cl.ltype = CONFIG_LINE_COMMENT
            return cl

        cmd_cmt_match = re.match(r'^\s*;.*', line)
        cmd_match = re.match(r'^\s*(;)?\s*([a-zA-Z0-9\-_]+)\s+(.+?)(\s*(#|;).+)?$', line)

        if cmd_match is None and cmd_cmt_match is not None:
            cl.ltype = CONFIG_LINE_COMMENT
            return cl

        cl.ltype = CONFIG_LINE_CMD if cmd_match.group(1) is None else CONFIG_LINE_CMD_COMMENT
        cl.cmd = cmd_match.group(2).strip()
        cl.params = cmd_match.group(3).strip()
        cl.comment = cmd_match.group(4)
        return cl


class OpenVpn(object):
    """
    OpenVPN server configuration & management
    """

    SETTINGS_DIR = '/etc/openvpn'
    SETTINGS_FILE = 'server.conf'

    def __init__(self, sysconfig=None, *args, **kwargs):
        self.sysconfig = sysconfig

        # Result of load_config_file_lines
        self.server_config_data = None
        self.server_config_modified = False

    #
    # server.conf reading & modification
    #

    def get_config_file_path(self):
        """
        Returns config file path
        :return: server config file path
        """
        return os.path.join(self.SETTINGS_DIR, self.SETTINGS_FILE)

    def load_config_file_lines(self):
        """
        Loads config file to a string
        :return: array of ConfigLine or None if file does not exist
        """
        cpath = self.get_config_file_path()
        if not os.path.exists(cpath):
            return []

        lines = []
        with open(cpath, 'r') as fh:
            for idx, line in enumerate(fh):
                ln = ConfigLine.build(line=line, idx=idx)
                lines.append(ln)
        return lines

    def set_config_value(self, cmd, values, remove=False):
        """
        Sets command to the specified value in the configuration file.
        Loads file from the disk if server_config_data is None (file was not yet loaded).

        Supports also multicommands - one command with more values.

        Modifies self.server_config_data, self.server_config_modified
        :param cmd:
        :param values: single value or array of values for multi-commands (e.g., push).
                       None & remove -> remove all commands. Otherwise just commands with the given values are removed.
        :param remove: if True, configuration command is removed
        :return: True if file was modified
        """
        # If file is not loaded - load
        if self.server_config_data is None:
            self.server_config_data = self.load_config_file_lines()

        last_cmd_idx = 0
        file_changed = False
        if not isinstance(values, types.ListType):
            if values is None:
                values = []
            else:
                values = [values]

        values_set = [False] * len(values)
        for idx, cfg in enumerate(self.server_config_data):
            if cfg.ltype not in [CONFIG_LINE_CMD, CONFIG_LINE_CMD_COMMENT]:
                continue
            if cfg.cmd != cmd:
                continue

            # Only commands of interest here
            last_cmd_idx = idx
            is_desired_value = cfg.params in values
            is_desired_value |= remove and len(values) == 0
            value_idx = values.index(cfg.params) if not remove and cfg.params in values else None

            if is_desired_value:
                if cfg.ltype == CONFIG_LINE_CMD and not remove:
                    # Command is already set to the same value. File not modified.
                    # Cannot quit yet, has to comment out other values
                    if value_idx is not None:
                        values_set[value_idx] = True
                    pass

                elif cfg.ltype == CONFIG_LINE_CMD:
                    # Remove command - comment out
                    cfg.ltype = CONFIG_LINE_CMD_COMMENT
                    file_changed = True

                elif cfg.ltype == CONFIG_LINE_CMD_COMMENT and remove:
                    # Remove && comment - leave as it is
                    # Cannot quit yet, has to comment out other values
                    pass

                else:
                    # CONFIG_LINE_CMD_COMMENT and not remove.
                    # Just change the type to active value - switch from comment to command
                    # Cannot quit yet, has to comment out other values
                    cfg.ltype = CONFIG_LINE_CMD
                    file_changed = True
                    if value_idx is not None:
                        values_set[value_idx] = True

            elif cfg.ltype == CONFIG_LINE_CMD and not remove:
                # Same command, but different value - comment this out
                # If remove is True, only desired values were removed.
                cfg.ltype = CONFIG_LINE_CMD_COMMENT

        if remove:
            self.server_config_modified = file_changed
            return file_changed

        # Add those commands not set in the cycle above
        ctr = 0
        for idx, cval in enumerate(values):
            if values_set[idx]:
                continue

            cl = ConfigLine(idx=None, raw=None, ltype=CONFIG_LINE_CMD, cmd=cmd, params=value)
            self.server_config_data.insert(last_cmd_idx+1+ctr, cl)

            ctr += 1
            file_changed = True

        self.server_config_modified = file_changed
        return file_changed

    def update_config_file(self, force=False):
        """
        Updates server configuration file.
        Resets server_config_modified after the file update was flushed to the disk

        :return: True if file was modified
        """
        if not force and not self.server_config_modified:
            return False

        cpath = self.get_config_file_path()
        fh, backup = util.safe_create_with_backup(cpath, 'w', 0o644)
        with fh:
            for cl in self.server_config_data:
                fh.write(cl.raw + '\n')

        self.server_config_modified = False  # reset after flush
        return True

    #
    # Configuration
    #
    def generate_dh_group(self):
        """
        Generates a new Diffie-Hellman group for the server.
        openssl dhparam -out dh2048.pem 2048
        :return:
        """
        size = 2048  # constant for now
        dh_file = os.path.join(self.SETTINGS_DIR, 'dh%d.pem' % size)
        cmd = 'sudo openssl dhparam -out \'%s\' %d' % (dh_file, size)
        p = subprocess.Popen(cmd, shell=True)
        return p.wait()

    def configure_crl(self, crl_path):
        """
        Configures server with the given CRL file
        :param crl_path:
        :return: True if file was changed
        """
        self.set_config_value('crl-verify', crl_path, remove=crl_path is None)
        return self.update_config_file()

    def configure_server(self):
        """
        Perform base server configuration.
        :return: True if file was changed
        """
        self.set_config_value('port', '1194')
        self.set_config_value('proto', 'udp')
        self.set_config_value('cipher', 'AES-256-CBC')
        self.set_config_value('dh', 'dh2048.pem')
        self.set_config_value('ca', 'ca.crt')
        self.set_config_value('cert', 'server.crt')
        self.set_config_value('key', 'server.key')
        return self.update_config_file()

    #
    # Installation
    #
    def install(self):
        """
        Installs itself
        :return: installer return code
        """
        cmd_exec = 'sudo yum install -y openvpn'
        if self.sysconfig.get_packager() == osutil.PKG_APT:
            cmd_exec = 'sudo apt-get install -y openvpn'

        p = subprocess.Popen(cmd_exec, shell=True)
        p.communicate()
        return p.returncode

    def get_svc_map(self):
        """
        Returns service naming for different start systems
        :return:
        """
        return {
            osutil.START_SYSTEMD: 'openvpn.service',
            osutil.START_INITD: 'openvpn'
        }

    def enable(self):
        """
        Enables service after OS start
        :return:
        """
        return self.sysconfig.enable_svc(self.get_svc_map())

    def switch(self, start=None, stop=None, restart=None):
        """
        Starts/stops/restarts the service
        :param state:
        :param start:
        :param stop:
        :param restart:
        :return:
        """
        return self.sysconfig.switch_svc(self.get_svc_map(), start=start, stop=stop, restart=restart)

