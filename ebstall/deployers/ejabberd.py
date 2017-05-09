#!/usr/bin/env python
# -*- coding: utf-8 -*-

from __future__ import print_function
import os
import logging
import ebstall.errors as errors
import collections
import re
import requests
import ebstall.util as util
import types
import ebstall.osutil as osutil
import shutil
import time
import pkg_resources

from ebstall.consts import PROVISIONING_SERVERS
from ebstall.deployers import letsencrypt

__author__ = 'dusanklinec'
logger = logging.getLogger(__name__)


class Ejabberd(object):
    """
    Nextcloud module
    """

    def __init__(self, sysconfig=None, audit=None, write_dots=False, config=None,  *args, **kwargs):
        self.sysconfig = sysconfig
        self.write_dots = write_dots
        self.audit = audit
        self.config = config
        self.hostname = None

        self.file_rpm = 'ejabberd-17.04-0.x86_64.rpm'
        self.file_deb = 'ejabberd_17.04-0_amd64.deb'

    #
    # Configuration
    #

    def _get_tls_paths(self):
        """
        Returns chain & key path for TLS or None, None
        :return: keychain path, privkey path
        """
        cert_dir = os.path.join(letsencrypt.LE_CERT_PATH, self.hostname)
        cert_path = os.path.join(cert_dir, letsencrypt.LE_CA)
        key_path = os.path.join(cert_dir, letsencrypt.LE_PRIVATE_KEY)
        return cert_path, key_path

    def _find_root(self):
        """
        Finds the ejabberd root dir
        :return: 
        """

    def configure(self):
        """
        Configures ejabberd server
        :return: 
        """

        pass

    #
    # Installation
    #

    def _download_file(self, url, filename, attempts=1):
        """
        Downloads binary file, saves to the file
        :param url:
        :param filename:
        :return:
        """
        return util.download_file(url, filename, attempts)

    def _deploy_downloaded(self, archive_path, basedir):
        """
        Analyzes downloaded file, deploys to the webroot
        :param archive_path:
        :param basedir:
        :return:
        """
        cmd_exec = None
        pkg = self.sysconfig.get_packager()
        if pkg == osutil.PKG_YUM:
            cmd_exec = 'sudo yum localinstall -y %s' % util.escape_shell(archive_path)
        elif pkg == osutil.PKG_APT:
            cmd_exec = 'sudo dpkg -i %s' % util.escape_shell(archive_path)

        ret = self.sysconfig.exec_shell(cmd_exec, write_dots=self.write_dots)
        if ret != 0:
            raise errors.SetupError('Could not install ejabberd server')

    def _install(self, attempts=3):
        """
        Downloads ejabberd install package from the server, installs it.
        :return:
        """
        pkg = self.sysconfig.get_packager()
        if pkg == osutil.PKG_YUM:
            base_file = self.file_rpm
        elif pkg == osutil.PKG_APT:
            base_file = self.file_deb
        else:
            raise errors.EnvError('Unsupported package manager for ejabberd server')

        try:
            logger.debug('Going to download nextcloud from the provisioning servers')
            for provserver in PROVISIONING_SERVERS:
                url = 'https://%s/ejabberd/%s' % (provserver, base_file)
                tmpdir = util.safe_new_dir('/tmp/ejabberd-install')

                try:
                    self.audit.audit_evt('prov-ejabberd', url=url)

                    # Download archive.
                    archive_path = os.path.join(tmpdir, base_file)
                    self._download_file(url, archive_path, attempts=attempts)

                    # Update
                    self._deploy_downloaded(archive_path, tmpdir)
                    return 0

                except errors.SetupError as e:
                    logger.debug('SetupException in fetching Ejabberd from the provisioning server: %s' % e)
                    self.audit.audit_exception(e, process='prov-ejabberd')

                except Exception as e:
                    logger.debug('Exception in fetching Ejabberd from the provisioning server: %s' % e)
                    self.audit.audit_exception(e, process='prov-ejabberd')

                finally:
                    if os.path.exists(tmpdir):
                        shutil.rmtree(tmpdir)

                return 0

        except Exception as e:
            logger.debug('Exception when fetching Ejabberd')
            self.audit.audit_exception(e)
            raise errors.SetupError('Could not install Ejabberd', cause=e)

    def install(self):
        """
        Installs itself
        :return: installer return code
        """
        ret = self._install(attempts=3)
        if ret != 0:
            raise errors.SetupError('Could not install Ejabberd')
        return 0





