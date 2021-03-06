#!/usr/bin/env python
# -*- coding: utf-8 -*-

from __future__ import print_function
import os
import logging
import ebstall.errors as errors
import collections
import re
import ebstall.util as util
import subprocess
import types
import ebstall.osutil as osutil
import time
import sys
import shutil
import pkg_resources

from ebstall.consts import PROVISIONING_SERVERS

__author__ = 'dusanklinec'
logger = logging.getLogger(__name__)


class Jboss(object):
    """
    Jboss managing class
    """
    JBOSS_OPT = '/opt'
    JBOSS_HOME = '/opt/jboss-eap-6.4.0'
    JBOSS_USER = 'jboss'

    # JBoss paths
    JBOSS_CLI = 'bin/jboss-cli.sh'
    JBOSS_KEYSTORE = 'standalone/configuration/keystore/keystore.jks'
    JBOSS_CONFIG = 'standalone/configuration/standalone.xml'
    JBOSS_DEPLOY = 'standalone/deployments'

    JBOSS_ARCHIVE = 'jboss-eap-6.4.0.tgz'

    def __init__(self, sysconfig=None, audit=None, write_dots=False, eb_config=None, config=None, *args, **kwargs):
        self.sysconfig = sysconfig
        self.write_dots = write_dots
        self.audit = audit
        self.eb_config = eb_config
        self.config = config

    #
    # Configuration
    #

    def get_user(self):
        """
        Returns JBoss user
        :return:
        """
        return self.JBOSS_USER

    def get_jboss_home(self):
        """
        Returns JBoss home directory, first try to look at env var, then return default val
        :return:
        """
        if 'JBOSS_HOME' in os.environ and len(os.environ['JBOSS_HOME']) > 0:
            return os.path.abspath(os.environ['JBOSS_HOME'])

        if self.eb_config is not None:
            config_home = self.eb_config.jboss_home
            if config_home is not None:
                return config_home

            return os.path.abspath(self.JBOSS_HOME)

    def get_jboss_config(self):
        """
        Returns JBoss configuration file - for redirects
        :return:
        """
        return os.path.join(self.get_jboss_home(), self.JBOSS_CONFIG)

    def get_keystore_path(self):
        """
        Returns path to the jboss keystore - https
        :return:
        """
        return os.path.abspath(os.path.join(self.get_jboss_home(), self.JBOSS_KEYSTORE))

    def get_deploy_path(self):
        """
        Returns path to the jboss keystore - https
        :return:
        """
        return os.path.abspath(os.path.join(self.get_jboss_home(), self.JBOSS_DEPLOY))

    def configure_server(self):
        """
        Perform base server configuration.
        :return: True if file was changed
        """
        return False

    def test_environment(self):
        """
        Returns true if env is ok
        :return:
        """
        return os.path.exists(self.get_jboss_home())

    #
    # Installation
    #

    def _download_file(self, url, filename, attempts=3):
        """
        Downloads binary file, saves to the file
        :param url:
        :param filename:
        :return:
        """
        return util.download_file(url, filename, attempts)

    def _deploy_downloaded(self, archive_path, basedir):
        """
        Analyzes downloaded file, deploys to the install dir
        :param archive_path:
        :param basedir:
        :return:
        """
        cmd = 'sudo tar -xzvf %s' % util.escape_shell(archive_path)
        ret, out, err = self.sysconfig.cli_cmd_sync(cmd, write_dots=True, cwd=basedir)
        if ret != 0:
            raise errors.SetupError('Could not extract update archive')

        folders = [f for f in os.listdir(basedir) if not os.path.isfile(os.path.join(basedir, f))
                   and f != '.' and f != '..']

        if len(folders) != 1:
            raise errors.SetupError('Invalid folder structure after update extraction')

        archive_dir = os.path.join(basedir, folders[0])
        if not os.path.exists(archive_dir):
            raise errors.SetupError('Directory with jboss not found in the install archive: %s' % archive_dir)

        archive_slash = util.add_ending_slash(archive_dir)
        dest_slash = util.add_ending_slash(self.get_jboss_home())

        # reinstall - preserve user data
        excludes = ''
        cmd = 'sudo rsync -av --delete %s %s %s' \
              % (excludes, util.escape_shell(archive_slash), util.escape_shell(dest_slash))
        ret, out, err = self.sysconfig.cli_cmd_sync(cmd, write_dots=True, cwd=basedir)
        if ret != 0:
            raise errors.SetupError('jboss sync failed')

        self.fix_privileges()

    def _install(self, attempts=3):
        """
        Downloads JBoss installation file from provisioning server.
        :return:
        """
        base_file = self.JBOSS_ARCHIVE
        try:
            logger.debug('Going to download JBoss from the provisioning servers')
            for provserver in PROVISIONING_SERVERS:
                url = 'https://%s/jboss/%s' % (provserver, base_file)
                tmpdir = util.safe_new_dir('/tmp/jboss-install')

                try:
                    self.audit.audit_evt('prov-jboss', url=url)

                    # Download archive.
                    archive_path = os.path.join(tmpdir, base_file)
                    self._download_file(url, archive_path, attempts=attempts)

                    # Install
                    self._deploy_downloaded(archive_path, tmpdir)
                    return 0

                except errors.SetupError as e:
                    logger.debug('SetupException in fetching JBoss from the provisioning server: %s' % e)
                    self.audit.audit_exception(e, process='prov-jboss')

                except Exception as e:
                    logger.debug('Exception in fetching JBoss from the provisioning server: %s' % e)
                    self.audit.audit_exception(e, process='prov-jboss')

                finally:
                    if os.path.exists(tmpdir):
                        shutil.rmtree(tmpdir)

                return 0

        except Exception as e:
            logger.debug('Exception when fetching jBoss')
            self.audit.audit_exception(e)
            raise errors.SetupError('Could not install jBoss', cause=e)

    def install(self):
        """
        Installs itself
        Jboss installer is not supported yet, has to be already present on the system.
        :return: installer return code
        """

        # TODO: full install on clean image:
        # TODO:  - create jboss user
        # TODO:  - _install to download jboss image, symlink /opt/jboss -> /opt/jboss-eap.6.4.0
        # TODO:  - start script for jboss

        home = self.get_jboss_home()
        if not os.path.exists(home):
            raise errors.SetupError('JBoss not found in %s' % home)
        return 0

    def get_svc_map(self):
        """
        Returns service naming for different start systems
        :return:
        """
        return {
            osutil.START_SYSTEMD: 'jboss-eap-6.4.0.service',
            osutil.START_INITD: 'jboss-eap-6.4.0'
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
        :param start:
        :param stop:
        :param restart:
        :return:
        """
        return self.sysconfig.switch_svc(self.get_svc_map(), start=start, stop=stop, restart=restart)

    def setup_os(self):
        """
        Configures OS
        :return:
        """
        self.install_cron()
        return 0

    def jboss_restart(self):
        """
        Restarts JBoss daemon
        Here is important to start it with setsid so daemon is started in a new shell session.
        Otherwise Jboss would have been killed in case python terminates.
        :return:
        """
        os.spawnlp(os.P_NOWAIT, "sudo", "bash", "bash", "-c",
                   "setsid /etc/init.d/jboss-eap-6.4.0 restart 2>/dev/null >/dev/null </dev/null &")

        self.audit.audit_exec('sudo bash -c "setsid /etc/init.d/jboss-eap-6.4.0 restart '
                              '2>/dev/null >/dev/null </dev/null &"')

        time.sleep(10)
        return self.wait_after_start()

    def get_cron_file(self):
        """
        Returns contents of the cron.d file for cleaning log records
        :return: crl cron file string
        """
        cron = '#!/bin/bash\n'
        cron += "1 1 * * * root find %s/standalone/log/ -name 'server.log.*' -mtime +15 -exec /bin/rm {} \;\n" \
                % self.get_jboss_home()
        return cron

    def install_cron(self):
        """
        Installs all cron.d files required by the JBoss
        :return: 0 on success, can throw exception
        """
        crl_cron = self.get_cron_file()
        if self.sysconfig is None:
            raise ValueError('Sysconfig is None, required for cron installation')

        return self.sysconfig.install_crond_file(file_name='jboss-log-clean', file_contents=crl_cron)

    #
    # CLI
    #

    def cli_cmd(self, cmd):
        """
        Executes a JBoss CLI command
        :param cmd:
        :return:
        """
        cli = os.path.abspath(os.path.join(self.get_jboss_home(), self.JBOSS_CLI))
        if not os.path.exists(cli):
            logger.debug('CLI does not exist')
            return 1

        cli_cmd = 'sudo -E -H -u %s %s -c \'%s\'' % (self.JBOSS_USER, cli, cmd)

        with open('/tmp/jboss-cli.log', 'a+') as logger_obj:
            ret, out, err = self.sysconfig.cli_cmd_sync(cli_cmd, log_obj=logger_obj, write_dots=self.write_dots,
                                                        cwd=self.get_jboss_home())
            return ret, out, err

    def reload(self):
        """
        Reloads JBoss server by issuing :reload command on the JBoss CLI
        :return:
        """
        ret = self.cli_cmd(':reload')
        time.sleep(3)
        self.wait_after_start()
        return ret

    def wait_after_start(self):
        """
        Waits until JBoss responds with success after start
        :return:
        """
        jboss_works = False
        max_attempts = 30

        for i in range(0, max_attempts):
            if i > 0:
                if self.write_dots:
                    sys.stderr.write('.')
                time.sleep(3)

            try:
                ret, out, err = self.cli_cmd(':read-attribute(name=server-state)')
                if out is None or len(out) == 0:
                    continue

                out_total = '\n'.join(out)

                if re.search(r'["\']?outcome["\']?\s*=>\s*["\']?success["\']?', out_total) and \
                        re.search(r'["\']?result["\']?\s*=>\s*["\']?running["\']?', out_total):
                    jboss_works = True
                    break

            except Exception as ex:
                continue

        return jboss_works

    def fix_privileges(self):
        """
        Fixes JBoss privileges in the Jboss home dir
        :return:
        """
        self.sysconfig.chown_recursive(self.get_jboss_home(), self.JBOSS_USER, self.JBOSS_USER)

    #
    # CLI config
    #

    def add_mysql_jdbc(self):
        """
        Adds MySQL JDBC to the JBoss.
        Performed only once after JBoss installation.
        :return:
        """
        return self.cli_cmd('/subsystem=datasources/jdbc-driver=com.mysql.jdbc.Driver:add'
                                    '(driver-name=com.mysql.jdbc.Driver,driver-class-name=com.mysql.jdbc.Driver,'
                                    'driver-module-name=com.mysql,driver-xa-datasource-class-name='
                                    'com.mysql.jdbc.jdbc2.optional.MysqlXADataSource)')

    def get_rewrite_rules_list(self):
        """
        Returns list of rewrite rules defined for default virtual serer.
        :return:
        """
        cmd = '/subsystem=web/virtual-server=default-host:read-children-names(child-type=rewrite)'
        ret, out, err = self.cli_cmd(cmd)
        if ret != 0:
            raise errors.SetupError('Cannot get JBoss rewrite rules')

        out_json = util.jboss_to_json(out)
        if out_json is None or 'result' not in out_json:
            raise errors.SetupError('Invalid JBoss response on rewrite rules get')
        return out_json['result']

    def get_rewrite_rules(self):
        """
        Returns rewrite rules for the default virtual host with their definitions
        :return:
        """
        cmd = '/subsystem=web/virtual-server=default-host:read-children-resources(child-type=rewrite)'
        ret, out, err = self.cli_cmd(cmd)
        if ret != 0:
            raise errors.SetupError('Cannot get JBoss rewrite rules')

        out_json = util.jboss_to_json(out)
        if out_json is None or 'result' not in out_json:
            raise errors.SetupError('Invalid JBoss response on rewrite rules get')
        return out_json['result']

    def remove_rewrite_rule(self, rule):
        """
        Removes rewrite rule from the default virtual host
        :param rule:
        :return:
        """
        cmd = '/subsystem=web/virtual-server=default-host/rewrite=%s:remove' % rule
        ret, out, err = self.cli_cmd(cmd)
        if ret != 0:
            raise errors.SetupError('Cannot get JBoss rewrite rules')
        return ret

    def add_rewrite_rule(self, rule_id, pattern, subs, flags='L,QSA,R'):
        """
        Adds a new rewrite rule to the jboss
        :param rule_id:
        :param pattern:
        :param subs:
        :param flags:
        :return:
        """
        pattern = pattern.replace('"', '\\"')
        subs = subs.replace('"', '\\"')
        flags = flags.replace('"', '\\"')
        cmd = '/subsystem=web/virtual-server=default-host/rewrite=%s:add(pattern="%s", substitution="%s", flags="%s")' \
              % (rule_id, pattern, subs, flags)
        ret, out, err = self.cli_cmd(cmd)
        if ret != 0:
            raise errors.SetupError('Cannot set JBoss rewrite rule %s' % rule_id)
        return ret

    def enable_default_root(self):
        """
        Enables default root for JBoss - required for rewrites
        /subsystem=web/virtual-server=default-host:write-attribute(name="enable-welcome-root",value=true)
        :return:
        """
        cmd = '/subsystem=web/virtual-server=default-host:write-attribute(name="enable-welcome-root",value=true)'
        ret, out, err = self.cli_cmd(cmd)
        if ret != 0:
            raise errors.SetupError('Cannot set JBoss default host')
        return ret

    def remove_all_rewrite_rules(self):
        """
        Removes all rewrite rules defined for the defualt virtual host.
        Needs jboss reload
        :return:
        """
        rules_list = self.get_rewrite_rules_list()
        for rule_id in rules_list:
            self.remove_rewrite_rule(rule_id)

    def undeploy_fs(self, name, wait_indicator=True, wait_timeout=10.0):
        """
        Undeploys by removing from the deployment dir
        :param name: 
        :param wait_indicator: 
        :param wait_timeout: 
        :return: 
        """
        deployed_path = os.path.join(self.get_deploy_path(), name)
        undeployed_path = os.path.join(self.get_deploy_path(), '%s.undeployed' % name)

        if not os.path.exists(deployed_path):
            logger.debug('Deployed path does not exist, skipping: %s' % deployed_path)
            return 1

        os.remove(deployed_path)

        # wait for undeployed file indicator
        if not wait_indicator:
            logger.debug('Waiting for undeploy finish skipped')
            return 0

        undeployed_detected = False
        check_start = time.time()
        while time.time() - check_start <= wait_timeout:
            time.sleep(0.25)
            if os.path.exists(undeployed_path):
                logger.debug('Detected undeployed indicator')
                return 0

        logger.debug('Undeploy indicator presence check timeout.')
        return 0

