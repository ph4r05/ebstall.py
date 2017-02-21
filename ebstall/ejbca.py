#!/usr/bin/env python
# -*- coding: utf-8 -*-

import os
import util
from sarge import run, Capture, Feeder
from ebclient.eb_utils import EBUtils
from softhsm import SoftHsmV1Config
from datetime import datetime
import time
import sys
import types
import subprocess
import shutil
import osutil
import re
import letsencrypt
import logging
import errors
from audit import AuditManager
from consts import LE_VERIFY_DNS, LE_VERIFY_TLSSNI, LE_VERIFY_DEFAULT


__author__ = 'dusanklinec'
logger = logging.getLogger(__name__)


class Ejbca(object):
    """
    EJBCA configuration & builder
    https://www.ejbca.org/docs/installation.html#Install
    """

    PORT = 8443
    PORT_PUBLIC = 8442

    # Default home dirs
    EJBCA_HOME = '/opt/ejbca_ce_6_3_1_1'
    JBOSS_HOME = '/opt/jboss-eap-6.4.0'
    JBOSS_USER = 'jboss'
    USER_HOME = '/home/ec2-user'
    SSH_USER = 'ec2-user'

    # EJBCA paths
    INSTALL_PROPERTIES_FILE = 'conf/install.properties'
    WEB_PROPERTIES_FILE = 'conf/web.properties'
    DATABASE_PROPERTIES_FILE = 'conf/database.properties'
    EJBCA_PROPERTIES_FILE = 'conf/ejbca.properties'
    MAIL_PROPERTIES_FILE = 'conf/mail.properties'
    P12_FILE = 'p12/superadmin.p12'

    # Storage paths
    PASSWORDS_FILE = '/root/ejbca.passwords'
    PASSWORDS_BACKUP_DIR = '/root/ejbca.passwords.old'
    DB_BACKUPS = '/root/ejbcadb.old'

    # JBoss paths
    JBOSS_CLI = 'bin/jboss-cli.sh'
    JBOSS_KEYSTORE = 'standalone/configuration/keystore/keystore.jks'
    JBOSS_CONFIG = 'standalone/configuration/standalone.xml'

    # MySQL connection
    MYSQL_HOST = 'localhost'
    MYSQL_PORT = '3306'
    MYSQL_DB = 'ejbca'
    MYSQL_USER = 'ejbca'

    # Default installation settings
    INSTALL_PROPERTIES = {
        'ca.name': 'SystemCA',
        'ca.dn': 'CN=SystemCA,O=Enigma Bridge Ltd,C=GB',
        'ca.tokentype': 'soft',
        'ca.keytype': 'RSA',
        'ca.keyspec': '2048',
        'ca.signaturealgorithm': 'SHA256WithRSA',
        'ca.validity': '7650',
        'ca.policy': 'null'
    }

    # web.properties file - misc settings.
    WEB_PROPERTIES = {
        'cryptotoken.p11.lib.255.name': 'EnigmaBridge',
        'cryptotoken.p11.lib.255.file': SoftHsmV1Config.SOFTHSM_SO_PATH,

        'httpsserver.hostname': 'localhost',
        'httpsserver.dn': 'CN=localhost,O=Enigma Bridge Ltd,C=GB',

        'superadmin.cn': 'SuperAdmin',
        'superadmin.dn': 'CN=SuperAdmin',
        'superadmin.batch': 'true',

        'vpn.email.from': 'root@localhost'
    }

    # MySQL database properties
    DATABASE_PROPERTIES = {
        # 'database.name': 'mysql',
        # 'database.url': 'jdbc:mysql://localhost:3306/ejbca?characterEncoding=UTF-8',
        # 'database.driver': 'com.mysql.jdbc.Driver',
        'database.username': 'ejbca',
        'database.password': 'sa'
    }

    # mail.properties file
    MAIL_PROPERTIES = {
        'mail.from': 'ejbca@localhost'
    }

    def __init__(self, install_props=None, web_props=None, print_output=False, eb_config=None, jks_pass=None,
                 config=None, staging=False, do_vpn=False, db_pass=None, master_p12_pass=None,
                 sysconfig=None, audit=None,
                 *args, **kwargs):

        self.install_props = util.defval(install_props, {})
        self.web_props = util.defval(web_props, {})
        self.database_props = {}
        self.mail_props = {}

        self.http_pass = util.defval(jks_pass, util.random_password(16))
        self.java_pass = 'changeit'  # EJBCA & JBoss bug here
        self.superadmin_pass = util.random_password(16)

        # MySQL EJBCA user password.
        self.db_pass = util.defval(db_pass, util.random_password(16))

        # P12 encryption password for VPN user enc.
        self.master_p12_pass = util.defval(master_p12_pass, util.random_password(16))

        self.do_vpn = do_vpn
        self.print_output = print_output
        self.hostname = None
        self.domains = None

        self.staging = staging
        self.lets_encrypt = None
        self.lets_encrypt_jks = None

        self.eb_config = eb_config
        self.config = config
        self.reg_svc = None
        self.sysconfig = sysconfig
        self.audit = audit
        if self.audit is None:
            self.audit = AuditManager(disabled=True)

        # Remove secrets from audit logging
        self.audit.add_secrets([self.http_pass, self.superadmin_pass, self.db_pass, self.master_p12_pass])

        self.ejbca_install_result = 1

        # Initialize settings
        self._setup_database_properties()

    def get_db_type(self):
        """
        Returns DB type to use in the installation
        :return: None for default (H2) or database type string, e.g., mysql
        """
        return self.config.ejbca_db_type

    def get_database_root_password(self):
        """
        Returns database root password for database setup. Used for external DBs (e.g, mysql)
        :return:
        """
        return self.config.mysql_root_password

    def get_ejbca_home(self):
        """
        Returns EJBCA home, first try to look at env var, then return default val
        :return:
        """
        if 'EJBCA_HOME' in os.environ and len(os.environ['EJBCA_HOME']) > 0:
            return os.path.abspath(os.environ['EJBCA_HOME'])

        if self.eb_config is not None:
            config_home = self.eb_config.ejbca_home
            if config_home is not None:
                return config_home

        return os.path.abspath(self.EJBCA_HOME)

    def get_ejbca_sh(self):
        """
        Returns EJBCA sh script
        :return:
        """
        return os.path.join(self.get_ejbca_home(), 'bin', 'ejbca.sh')

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

    def get_install_prop_file(self):
        return os.path.abspath(os.path.join(self.get_ejbca_home(), self.INSTALL_PROPERTIES_FILE))

    def get_web_prop_file(self):
        return os.path.abspath(os.path.join(self.get_ejbca_home(), self.WEB_PROPERTIES_FILE))

    def get_database_prop_file(self):
        return os.path.abspath(os.path.join(self.get_ejbca_home(), self.DATABASE_PROPERTIES_FILE))

    def get_email_prop_file(self):
        return os.path.abspath(os.path.join(self.get_ejbca_home(), self.MAIL_PROPERTIES_FILE))

    def properties_to_string(self, prop):
        """
        Converts dict based properties to a string
        :return:
        """
        result = []
        for k in prop:
            result.append("%s=%s" % (k, prop[k]))
        result = sorted(result)
        return '\n'.join(result)

    def set_config(self, config):
        self.config = config

    def _setup_database_properties(self):
        """
        Setting up database properties from the internal state
        e.g., database password, DB type.
        :return:
        """
        self.database_props['database.password'] = self.db_pass

        db_type = self.get_db_type()
        if db_type == 'mysql':
            # 'database.name': 'mysql',
            # 'database.url': 'jdbc:mysql://localhost:3306/ejbca?characterEncoding=UTF-8',
            # 'database.driver': 'com.mysql.jdbc.Driver',

            self.database_props['database.name'] = 'mysql'
            self.database_props['database.driver'] = 'com.mysql.jdbc.Driver'
            self.database_props['database.url'] = 'jdbc:mysql://%s:%s/%s?characterEncoding=UTF-8' \
                                                  % (self.MYSQL_HOST, self.MYSQL_PORT, self.MYSQL_DB)

        else:
            # Fallback - default H2 database
            return

    def set_domains(self, domains, primary=None, set_hostname=True):
        """
        Sets the domains EJBCA is reachable on
        :param domains:
        :return:
        """
        domains_empty = False
        if domains is None or len(domains) == 0:
            domains = ['localhost']
            domains_empty = True

        if not isinstance(domains, types.ListType):
            domains = [domains]

        # sort by (length, lexicographic)
        domains.sort()
        domains.sort(key=len, reverse=True)

        # if primary domain was not set use the longest one (convention).
        if primary is not None:
            if primary not in domains:
                domains.insert(0, primary)
            elif primary != domains[0]:
                raise ValueError('Primary domain has to be listed first in the domain list')
        else:
            primary = domains[0]

        self.domains = domains
        if set_hostname:
            self.set_hostname(primary)

    def check_hostname_domains_consistency(self):
        return self.domains is not None \
                and isinstance(self.domains, types.ListType) \
                and self.hostname == self.domains[0]

    def set_hostname(self, hostname):
        """
        Set hostname EJBCA will use - updates properties files in memory
        Should not be called outside the module (by user), use set_domains instead.
        :return:
        """
        if hostname is None:
            hostname = 'localhost'

        self.hostname = hostname
        if not self.check_hostname_domains_consistency():
            raise ValueError('Hostname is not consistent with domains, please, rather use set_domains()')

        self.web_props['httpsserver.hostname'] = hostname
        self.web_props['httpsserver.dn'] = 'CN=%s,O=Enigma Bridge Ltd,C=GB' % hostname

        leftmost_domain = util.get_leftmost_domain(hostname)
        self.install_props['ca.name'] = 'SystemCA-%s' % leftmost_domain
        self.install_props['ca.dn'] = 'CN=SystemCA-%s,O=Enigma Bridge Ltd,C=GB' % leftmost_domain

        # Update another hostname related properties
        if self.do_vpn:
            self.web_props['vpn.email.from'] = 'private-space@%s' % hostname
            self.mail_props['mail.from'] = 'private-space@%s' % hostname

        return self.web_props

    def _update_property_file(self, file, properties):
        """
        Updates EJBCA property file with backup
        :param file:
        :param content:
        :return:
        """
        prop_hdr = '#\n'
        prop_hdr += '# Config file generated: %s\n' % (datetime.now().strftime("%Y-%m-%d %H:%M"))
        prop_hdr += '#\n'

        file_hnd = None
        try:
            file_hnd, file_backup = util.safe_create_with_backup(file, 'w', 0o644)
            file_hnd.write(prop_hdr + self.properties_to_string(properties) + "\n\n")
        finally:
            if file_hnd is not None:
                file_hnd.close()

    def update_properties(self):
        """
        Updates properties files of the ejbca
        :return:
        """
        self._setup_database_properties()
        self.web_props['vpn.ejbca.home'] = self.get_ejbca_home()

        file_web = self.get_web_prop_file()
        file_ins = self.get_install_prop_file()
        file_db = self.get_database_prop_file()

        prop_web = util.merge(self.WEB_PROPERTIES, self.web_props)
        prop_ins = util.merge(self.INSTALL_PROPERTIES, self.install_props)
        prop_db = util.merge(self.DATABASE_PROPERTIES, self.database_props)

        self._update_property_file(file_web, prop_web)
        self._update_property_file(file_ins, prop_ins)
        self._update_property_file(file_db, prop_db)

        if self.do_vpn:
            file_mail = self.get_email_prop_file()
            prop_mail = util.merge(self.MAIL_PROPERTIES, self.mail_props)
            self._update_property_file(file_mail, prop_mail)

    def cli_cmd(self, cmd, log_obj=None, write_dots=False, on_out=None, on_err=None, ant_answer=True, cwd=None):
        """
        Runs command line task
        Used for ant and jboss-cli.sh
        :return:
        """
        default_cwd = self.get_ejbca_home()
        if on_out is None and ant_answer is not None:
            on_out = self.ant_answer
        cwd = cwd if cwd is not None else default_cwd

        return self.sysconfig.cli_cmd_sync(cmd, log_obj=log_obj, write_dots=write_dots,
                                           on_out=on_out, on_err=on_err, cwd=cwd)

    #
    # ANT CLI, calls
    #

    def ant_cmd(self, cmd, log_obj=None, write_dots=False, on_out=None, on_err=None):
        ret, out, err = self.cli_cmd('sudo -E -H -u %s ant %s' % (self.JBOSS_USER, cmd),
                                     log_obj=log_obj, write_dots=write_dots,
                                     on_out=on_out, on_err=on_err, ant_answer=True)
        if ret != 0:
            sys.stderr.write('\nError, process returned with invalid result code: %s\n' % ret)
            if isinstance(log_obj, types.StringTypes):
                sys.stderr.write('For more details please refer to %s \n' % log_obj)
        if write_dots:
            sys.stderr.write('\n')
        return ret, out, err

    def ant_deploy(self):
        return self.ant_cmd('deploy', log_obj='/tmp/ant-deploy.log', write_dots=self.print_output)

    def ant_deployear(self):
        return self.ant_cmd('deployear', log_obj='/tmp/ant-deployear.log', write_dots=self.print_output)

    def ant_answer(self, out, feeder, p=None, *args, **kwargs):
        out = out.strip()
        if out.startswith('Please enter'):            # default - use default value, no starving
            feeder.feed('\n')
        elif out.startswith('[input] Please enter'):  # default - use default value, no starving
            feeder.feed('\n')

    def ant_install_answer(self, out, feeder, p=None, *args, **kwargs):
        out = out.strip()
        if 'truststore with the CA certificate for https' in out:
            feeder.feed(self.java_pass + '\n')
        elif 'keystore with the TLS key for https' in out:
            feeder.feed(self.http_pass + '\n')
        elif 'the superadmin password' in out:
            feeder.feed(self.superadmin_pass + '\n')
        elif 'password CA token password' in out:
            feeder.feed('\n')
        elif out.startswith('Please enter'):          # default - use default value, no starving
            feeder.feed('\n')
        elif out.startswith('[input] Please enter'):  # default - use default value, no starving
            feeder.feed('\n')

    def ant_install(self):
        """
        Installation
        :return:
        """
        return self.ant_cmd('install', log_obj='/tmp/ant-install.log', write_dots=self.print_output, on_out=self.ant_install_answer)

    def ant_client_tools(self):
        return self.ant_cmd('clientToolBox', log_obj='/tmp/ant-clientToolBox.log', write_dots=self.print_output)

    #
    # JBoss CLI
    #

    def jboss_cmd(self, cmd):
        cli = os.path.abspath(os.path.join(self.get_jboss_home(), self.JBOSS_CLI))
        cli_cmd = 'sudo -E -H -u %s %s -c \'%s\'' % (self.JBOSS_USER, cli, cmd)

        with open('/tmp/jboss-cli.log', 'a+') as logger:
            ret, out, err = self.cli_cmd(cli_cmd, log_obj=logger,
                                         write_dots=self.print_output, ant_answer=False,
                                         cwd=self.get_jboss_home())
            return ret, out, err

    def jboss_reload(self):
        ret = self.jboss_cmd(':reload')
        time.sleep(3)
        self.jboss_wait_after_start()
        return ret

    def jboss_undeploy(self):
        return self.jboss_cmd('undeploy ejbca.ear')

    def jboss_remove_datasource(self):
        return self.jboss_cmd('data-source remove --name=ejbcads')

    def jboss_add_mysql_jdbc(self):
        """
        Adds MySQL JDBC to the JBoss.
        Performed only once after JBoss installation.
        :return:
        """
        return self.jboss_cmd('/subsystem=datasources/jdbc-driver=com.mysql.jdbc.Driver:add(driver-name=com.mysql.jdbc.Driver,driver-class-name=com.mysql.jdbc.Driver,driver-module-name=com.mysql,driver-xa-datasource-class-name=com.mysql.jdbc.jdbc2.optional.MysqlXADataSource)')

    def jboss_rollback_ejbca(self):
        cmds = ['/core-service=management/security-realm=SSLRealm/authentication=truststore:remove',
                '/core-service=management/security-realm=SSLRealm/server-identity=ssl:remove',
                '/core-service=management/security-realm=SSLRealm:remove',

                '/socket-binding-group=standard-sockets/socket-binding=httpspub:remove',
                '/subsystem=undertow/server=default-server/https-listener=httpspub:remove',
                '/subsystem=web/connector=httpspub:remove',

                '/socket-binding-group=standard-sockets/socket-binding=httpspriv:remove',
                '/subsystem=undertow/server=default-server/https-listener=httpspriv:remove',
                '/subsystem=web/connector=httpspriv:remove',

                '/socket-binding-group=standard-sockets/socket-binding=http:remove',
                '/subsystem=undertow/server=default-server/http-listener=http:remove',
                '/subsystem=web/connector=http:remove',

                '/subsystem=undertow/server=default-server/http-listener=default:remove',

                '/system-property=org.apache.catalina.connector.URI_ENCODING:remove',
                '/system-property=org.apache.catalina.connector.USE_BODY_ENCODING_FOR_QUERY_STRING:remove',

                '/interface=http:remove',
                '/interface=httpspub:remove',
                '/interface=httpspriv:remove']
        for cmd in cmds:
            self.jboss_cmd(cmd)
        self.jboss_reload()

    def _jboss_get_rewrite_rules_list(self):
        """
        Returns list of rewrite rules defined for default virtual serer.
        :return:
        """
        cmd = '/subsystem=web/virtual-server=default-host:read-children-names(child-type=rewrite)'
        ret, out, err = self.jboss_cmd(cmd)
        if ret != 0:
            raise errors.SetupError('Cannot get jboss rewrite rules')

        out_json = util.jboss_to_json(out)
        if out_json is None or 'result' not in out_json:
            raise errors.SetupError('Invalid jboss response on rewrite rules get')
        return out_json['result']

    def _jboss_get_rewrite_rules(self):
        """
        Returns rewrite rules for the default virtual host with their definitions
        :return:
        """
        cmd = '/subsystem=web/virtual-server=default-host:read-children-resources(child-type=rewrite)'
        ret, out, err = self.jboss_cmd(cmd)
        if ret != 0:
            raise errors.SetupError('Cannot get jboss rewrite rules')

        out_json = util.jboss_to_json(out)
        if out_json is None or 'result' not in out_json:
            raise errors.SetupError('Invalid jboss response on rewrite rules get')
        return out_json['result']

    def _jboss_remove_rewrite_rule(self, rule):
        """
        Removes rewrite rule from the default virtual host
        :param rule:
        :return:
        """
        cmd = '/subsystem=web/virtual-server=default-host/rewrite=%s:remove' % rule
        ret, out, err = self.jboss_cmd(cmd)
        if ret != 0:
            raise errors.SetupError('Cannot get jboss rewrite rules')
        return ret

    def _jboss_add_rewrite_rule(self, rule_id, pattern, subs, flags='L,QSA,R'):
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
        ret, out, err = self.jboss_cmd(cmd)
        if ret != 0:
            raise errors.SetupError('Cannot set jboss rewrite rule %s' % rule_id)
        return ret

    def jboss_remove_all_rewrite_rules(self):
        """
        Removes all rewrite rules defined for the defualt virtual host.
        Needs jboss reload
        :return:
        """
        rules_list = self._jboss_get_rewrite_rules_list()
        for rule_id in rules_list:
            self._jboss_remove_rewrite_rule(rule_id)

    def jboss_add_rewrite_ejbca(self):
        """
        Adds EJBCA default rewrite rules
        :return:
        """
        self._jboss_add_rewrite_rule('rule01', '^/$', '/ejbca/adminweb', 'L,QSA,R')
        self._jboss_add_rewrite_rule('rule02', '^/pki/?$', '/ejbca/adminweb', 'L,QSA,R')

    def jboss_add_rewrite_vpn(self):
        """
        Adds default rewrites for VPN configuration
        :return:
        """
        self._jboss_add_rewrite_rule('rule01', '^/$', '/ejbca/adminweb/vpn/vpnusers.jsf', 'L,QSA,R')
        self._jboss_add_rewrite_rule('rule02', '^/pki/?$', '/ejbca/adminweb', 'L,QSA,R')

    def jboss_configure_rewrite_ejbca(self):
        """
        Configures EJBCA rewrite rules
        :return:
        """
        self.jboss_remove_all_rewrite_rules()
        self.jboss_add_rewrite_ejbca()

    def jboss_configure_rewrite_vpn(self):
        """
        Configures VPN rewrite rules
        :return:
        """
        self.jboss_remove_all_rewrite_rules()
        self.jboss_add_rewrite_vpn()

    #
    # Backup / env reset
    #

    def get_mysql_root_connstring(self):
        """
        Returns connection string to the MySQL database for root.
        :return:
        """
        con_string = 'mysql://%s:%s@%s%s/%s' % ('root', self.get_database_root_password(),
                                                self.MYSQL_HOST, ':%s' % self.MYSQL_PORT, self.MYSQL_DB)
        return con_string

    def backup_mysql_database(self):
        """
        Backups EJBCA database in the standard location.
        internally uses mysqldump command to create SQL dump
        :return:
        """
        util.make_or_verify_dir(self.DB_BACKUPS)

        db_fpath = os.path.abspath(os.path.join(self.DB_BACKUPS, 'dbdump.sql'))
        fh, backup_file = util.safe_create_with_backup(db_fpath, mode='w', chmod=0o600)
        fh.close()

        cmd = 'sudo mysqldump --database \'%s\' -u \'%s\' -p\'%s\' > \'%s\'' \
              % (self.MYSQL_DB, 'root', self.get_database_root_password(), db_fpath)

        return self.sysconfig.exec_shell(cmd)

    def db_install_python_mysql(self):
        """
        Checks if Mysql for python is installed.
        :return:
        """
        try:
            import MySQLdb
        except:
            logger.debug('MySQLdb not found')
            pkger = self.sysconfig.get_packager()
            ret = -1

            if pkger == osutil.PKG_YUM:
                ret = self.sysconfig.exec_shell('sudo yum install -y python python-devel mysql-devel '
                                                'redhat-rpm-config gcc')
            elif pkger == osutil.PKG_APT:
                ret = self.sysconfig.exec_shell('sudo apt-get install python-pip python-dev libmysqlclient-dev')

            else:
                raise EnvironmentError('MySQLdb module not installed, code: %s' % ret)

            if ret != 0:
                raise OSError('Could not install MySQLdb related packages')

            ret = self.sysconfig.exec_shell('sudo pip install MySQL-python')
            if ret != 0:
                raise OSError('Could not install MySQLdb, code: %s' % ret)

    def reset_mysql_database(self):
        """
        Performs backup of the original MySQL database - if any.
        Resets the database to the original state - drop database, drop users, create from scratch.
        :return:
        """
        self.backup_mysql_database()
        con_str = self.get_mysql_root_connstring()

        # Install required packages for mysql
        self.db_install_python_mysql()
        try:
            from sqlalchemy import create_engine
            from sqlalchemy.orm import sessionmaker, scoped_session
            engine = create_engine(con_str, pool_recycle=3600)
            engine.execute("DROP DATABASE `%s`" % self.MYSQL_DB)
            engine.execute("CREATE DATABASE `%s` CHARACTER SET utf8 COLLATE utf8_general_ci" % self.MYSQL_DB)
            engine.execute("GRANT ALL PRIVILEGES ON `%s`.* TO '%s'@'localhost' IDENTIFIED BY '%s'"
                           % (self.MYSQL_DB, self.MYSQL_USER, self.db_pass))
            engine.execute("FLUSH PRIVILEGES")

        except Exception as e:
            logger.info('Exception in database regeneration %s' % e)
            raise

    def jboss_backup_database(self):
        """
        Removes original database, moving it to a backup location.
        :return:
        """
        jboss_dir = self.get_jboss_home()
        db1 = os.path.join(jboss_dir, 'ejbcadb.h2.db')
        db2 = os.path.join(jboss_dir, 'ejbcadb.trace.db')
        db3 = os.path.join(jboss_dir, 'ejbcadb.lock.db')

        util.make_or_verify_dir(self.DB_BACKUPS)

        backup1 = util.delete_file_backup(db1, backup_dir=self.DB_BACKUPS)
        backup2 = util.delete_file_backup(db2, backup_dir=self.DB_BACKUPS)
        backup3 = util.delete_file_backup(db3, backup_dir=self.DB_BACKUPS)

        if self.get_db_type() == 'mysql':
            self.reset_mysql_database()

        return backup1, backup2, backup3

    def jboss_fix_privileges(self):
        self.sysconfig.exec_shell('sudo chown -R %s:%s %s' % (self.JBOSS_USER, self.JBOSS_USER, self.get_jboss_home()))
        self.sysconfig.exec_shell('sudo chown -R %s:%s %s' % (self.JBOSS_USER, self.JBOSS_USER, self.get_ejbca_home()))

    def jboss_wait_after_start(self):
        """
        Waits until JBoss responds with success after start
        :return:
        """
        jboss_works = False
        max_attempts = 30

        for i in range(0, max_attempts):
            if i > 0:
                if self.print_output:
                    sys.stderr.write('.')
                time.sleep(3)

            try:
                ret, out, err = self.jboss_cmd(':read-attribute(name=server-state)')
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

    def jboss_wait_after_deploy(self):
        """
        Waits for JBoss to finish initial deployment.
        :return:
        """
        jboss_works = False
        max_attempts = 30

        for i in range(0, max_attempts):
            if i > 0:
                if self.print_output:
                    sys.stderr.write('.')
                time.sleep(3)

            try:
                ret, out, err = self.jboss_cmd('deploy -l')
                if out is None or len(out) == 0:
                    continue

                out_total = '\n'.join(out)

                if re.search(r'ejbca.ear.+?\sOK', out_total):
                    jboss_works = True
                    break

            except Exception as ex:
                continue

        return jboss_works

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
        return self.jboss_wait_after_start()

    def backup_passwords(self):
        """
        Backups the generated passwords to /root/ejbca.passwords
        :return:
        """
        util.make_or_verify_dir(self.PASSWORDS_BACKUP_DIR, mode=0o600)
        util.delete_file_backup(self.PASSWORDS_FILE, chmod=0o600, backup_dir=self.PASSWORDS_BACKUP_DIR)
        with util.safe_open(self.PASSWORDS_FILE, chmod=0o600) as f:
            f.write('httpsserver.password=%s\n' % self.http_pass)
            f.write('java.trustpassword=%s\n' % self.java_pass)
            f.write('superadmin.password=%s\n' % self.superadmin_pass)
            f.write('database.password=%s\n' % self.db_pass)
            f.write('masterp12.password=%s\n' % self.master_p12_pass)
            f.flush()
        self.audit.audit_file_write(self.PASSWORDS_FILE)

    def get_p12_file(self):
        return os.path.abspath(os.path.join(self.get_ejbca_home(), self.P12_FILE))

    def copy_p12_file(self):
        """
        Copies p12 file to the home directory & chown-s so user can download it via scp
        :return:
        """
        p12 = self.get_p12_file()
        new_p12 = os.path.abspath(os.path.join(self.USER_HOME, 'ejbca-admin.p12'))
        if os.path.exists(new_p12):
            os.remove(new_p12)
            self.audit.audit_delete(new_p12)

        # copy in a safe mode - create file non readable by others, copy
        with open(p12, 'r') as src_p12:
            with util.safe_open(new_p12, mode='w', chmod=0o600) as dst_p12:
                shutil.copyfileobj(src_p12, dst_p12)

        self.audit.audit_copy(src=p12, dst=new_p12)
        self.sysconfig.exec_shell('sudo chown %s:%s %s' % (self.SSH_USER, self.SSH_USER, new_p12))
        return new_p12

    #
    # EJBCA CLI
    #

    def ejbca_get_cwd(self):
        return os.path.join(self.get_ejbca_home(), 'bin')

    def ejbca_get_command(self, cmd):
        return 'sudo -E -H -u %s %s %s' % (self.JBOSS_USER, self.get_ejbca_sh(), cmd)

    def ejbca_cmd(self, cmd, retry_attempts=3, write_dots=False, on_out=None, on_err=None):
        """
        Executes cd $EJBCA_HOME/bin
        ./ejbca.sh $*

        :param cmd:
        :param retry_attempts:
        :return: return code, stdout, stderr
        """
        cwd = self.ejbca_get_cwd()
        ret, out, err = -1, None, None
        cmd_exec = self.ejbca_get_command(cmd)

        for i in range(0, retry_attempts):
            ret, out, err = self.cli_cmd(
                cmd_exec,
                log_obj=None, write_dots=write_dots,
                on_out=on_out, on_err=on_err,
                ant_answer=False, cwd=cwd)

            if ret == 0:
                return ret, out, err

        return ret, out, err

    #
    # PKCS 11 token operations
    #

    def ejbca_add_softhsm_token(self, softhsm=None, name='EnigmaBridge', slot_id=0):
        """
        Adds a new crypto token to the EJBCA using CLI
        https://www.ejbca.org/docs/userguide.html#New Crypto Tokens

        :param softhsm: SoftHSM object
        :param name: name of the HW crypto token used in EJBCA
        :param slot_id: slot index in the token to associate with the new EJBCA crypto token
        :return:
        """
        so_path = softhsm.get_so_path() if softhsm is not None else SoftHsmV1Config.SOFTHSM_SO_PATH
        cmd = 'cryptotoken create ' \
              '--token "%s" ' \
              '--pin 0000 ' \
              '--autoactivate TRUE ' \
              '--type "PKCS11CryptoToken" ' \
              '--lib "%s" ' \
              '--slotlabeltype SLOT_INDEX ' \
              '--slotlabel %d' % (name, so_path, slot_id)
        return self.ejbca_cmd(cmd, retry_attempts=1, write_dots=self.print_output)

    def pkcs11_get_cwd(self):
        return os.path.join(self.get_ejbca_home(), 'bin')

    def pkcs11_get_command(self, cmd):
        return 'sudo -E -H -u %s %s/pkcs11HSM.sh %s' % (self.JBOSS_USER, self.pkcs11_get_cwd(), cmd)

    def pkcs11_cmd(self, cmd, retry_attempts=3, write_dots=False, on_out=None, on_err=None):
        """
        Executes cd $EJBCA_HOME/bin
        ./pkcs11HSM.sh $*

        :param cmd:
        :param retry_attempts:
        :param write_dots:
        :param on_out:
        :param on_err:
        :return: return code, stdout, stderr
        """
        cwd = self.pkcs11_get_cwd()
        ret, out, err = -1, None, None
        cmd_exec = self.pkcs11_get_command(cmd)

        for i in range(0, retry_attempts):
            ret, out, err = self.cli_cmd(
                cmd_exec,
                log_obj=None, write_dots=write_dots,
                on_out=on_out, on_err=on_err,
                ant_answer=False, cwd=cwd)

            if ret == 0:
                return ret, out, err

        return ret, out, err

    def pkcs11_answer(self, out, feeder, p=None, *args, **kwargs):
        out = util.strip(out)
        if 'Password:' in out:
            feeder.feed('0000')
            feeder.feed('\n')

    def pkcs11_get_generate_key_cmd(self, softhsm=None, bit_size=2048, alias=None, slot_id=0):
        so_path = softhsm.get_so_path() if softhsm is not None else SoftHsmV1Config.SOFTHSM_SO_PATH
        return 'generate %s %s %s %s' % (so_path, bit_size, alias, slot_id)

    def pkcs11_get_test_key_cmd(self, softhsm=None, slot_id=0):
        so_path = softhsm.get_so_path() if softhsm is not None else SoftHsmV1Config.SOFTHSM_SO_PATH
        return 'test %s %s' % (so_path, slot_id)

    def pkcs11_generate_key(self, softhsm=None, bit_size=2048, alias=None, slot_id=0, retry_attempts=3):
        """
        Generates keys in the PKCS#11 token.
        Can be used with the EJBCA.

        cd $EJBCA_HOME/bin
        ./pkcs11HSM.sh generate /usr/lib64/softhsm/libsofthsm.so 4096 signKey 0
        :return:
        """
        cmd = self.pkcs11_get_generate_key_cmd(softhsm=softhsm, bit_size=bit_size, alias=alias, slot_id=slot_id)
        return self.pkcs11_cmd(cmd=cmd, retry_attempts=retry_attempts, write_dots=self.print_output,
                               on_out=self.pkcs11_answer, on_err=self.pkcs11_answer)

    def pkcs11_generate_default_key_set(self, softhsm=None, slot_id=0, retry_attempts=5,
                                        sign_key_alias='signKey',
                                        default_key_alias='defaultKey',
                                        test_key_alias='testKey'):
        """
        Generates a default key set to be used with EJBCA
        :param softhsm:
        :param slot_id:
        :param retry_attempts:
        :param sign_key_alias:
        :param default_key_alias:
        :param test_key_alias:
        :return: return code, stdout, stderr
        """
        aliases = [sign_key_alias, default_key_alias, test_key_alias]
        key_sizes = [2048, 2048, 2048]

        for idx, alias in enumerate(aliases):
            key_size = key_sizes[idx]
            ret, out, cmd = self.pkcs11_generate_key(softhsm=softhsm, bit_size=key_size, alias=alias,
                                                     slot_id=slot_id, retry_attempts=retry_attempts)

            if ret != 0:
                return ret, out, cmd

            if self.print_output:
                sys.stderr.write('.')
        return 0, None, None

    #
    # VPN ops
    #

    def vpn_get_ca_properties(self):
        """
        Returns contents of a property file for VPN CA. Used when creating VPN CA via comand line
        :return: string - property file
        """
        props = 'sharedLibrary %s\n' % SoftHsmV1Config.SOFTHSM_SO_PATH
        props += 'slotLabelType=SLOT_INDEX\n'
        props += 'slotLabelValue=0\n\n'
        props += '# auto-activation\n'
        props += 'pin=0000\n\n'
        props += '# CA key configuration\n'
        props += 'defaultKey defaultKey\n'
        props += 'certSignKey signKey\n'
        props += 'crlSignKey signKey\n'
        props += 'testKey testKey\n'

        return props

    def vpn_create_tmp_ca_prop_file(self):
        """
        Creates temporary property file for VPN CA CLI.
        :return: fname string
        """
        fpath = os.path.join('/tmp', 'vpn.ca.properties')
        fobj, fname = util.unique_file(fpath, mode=0o644)
        with fobj:
            fobj.write(self.vpn_get_ca_properties())
        return fname

    def vpn_create_ca_cmd(self, prop_file_path):
        """
        Returns EJBCA cmd to create VPN CA. CA Validity = 25 years
        :param prop_file_path: file path to the property file with CA properties
        :return: command string
        """
        cmd = "ca init --caname VPN "
        cmd += "--dn 'CN=%s'" % self.hostname
        cmd += " --tokenType 'org.cesecore.keys.token.PKCS11CryptoToken' "
        cmd += "--keyspec 2048 "
        cmd += "--keytype RSA "
        cmd += "-v 9150 "
        cmd += "-s SHA256WithRSA "
        cmd += "--tokenPass 0000 "
        cmd += "--policy null "
        cmd += "--tokenprop '%s'" % prop_file_path
        return cmd

    def vpn_create_ca(self):
        """
        Creates VPN CA using EJBCA CLI.
        Corresponding SoftHSM token has to be already prepared with keys generated in it.
        :return: 0 on success
        """
        fpath_prop = self.vpn_create_tmp_ca_prop_file()
        try:
            cmd = self.vpn_create_ca_cmd(fpath_prop)
            return self.ejbca_cmd(cmd, retry_attempts=1, write_dots=self.print_output)[0]

        finally:
            util.safely_remove(fpath_prop)

    def vpn_create_profiles(self):
        """
        Create required VPN certificate and end entity profiles
        VPN CA has to be created already
        :return: 0 on success
        """
        cmd = 'vpn initprofiles'
        return self.ejbca_cmd(cmd, retry_attempts=1, write_dots=self.print_output)[0]

    def vpn_create_server_certs(self, directory=None):
        """
        Creates VPN server credentials
        VPN CA and profiles have to be created already
        :param directory: if none, default directories are used.
        :return: 0 on success
        """
        cmd = 'vpn genserver --create --regenerate --pem --password \'%s\'' \
              % (util.escape_shell(self.master_p12_pass))
        if directory is not None:
            cmd += ' --directory \'%s\'' % util.escape_shell(directory)
        return self.ejbca_cmd(cmd, retry_attempts=1, write_dots=self.print_output)[0]

    def vpn_create_crl(self, force=True):
        """
        Creates a new CRL forcefully. Used to generate first CRL to start OpenVPN.
        Or to regenerate CRL.
        :return: 0 on success
        """
        cmd = 'vpn crl'
        return self.ejbca_cmd(cmd, retry_attempts=1, write_dots=self.print_output)[0]

    def vpn_create_user(self, email, device='default'):
        """
        Creates a new VPN user via EJBCA CLI.
        Credentials are sent to the user email
        :param email:
        :param device:
        :return: 0 on success
        """
        client_password = util.random_password(16)
        self.audit.add_secrets(client_password)

        cmd = "vpn genclient --email '%s' --device '%s' --password '%s' --regenerate" \
              % (util.escape_shell(email), util.escape_shell(device), util.escape_shell(client_password))
        return self.ejbca_cmd(cmd, retry_attempts=1, write_dots=self.print_output)[0]

    def vpn_get_crl_cron_file(self):
        """
        Returns contents of the cron.d file for generating a CRL
        :return: crl cron file string
        """
        crl = '# Check each half an hour if regeneration is needed\n'
        crl += '*/30 * * * * %s %s vpn crl' % (self.JBOSS_USER, self.get_ejbca_sh())
        return crl

    def vpn_install_cron(self):
        """
        Installs all cron.d files required by the VPN
        :return: 0 on success, can throw exception
        """
        crl_cron = self.vpn_get_crl_cron_file()
        if self.sysconfig is None:
            raise ValueError('Sysconfig is None, required for cron installation')

        return self.sysconfig.install_crond_file(file_name='ejbca-vpn', file_contents=crl_cron)

    def vpn_get_crl_path(self):
        """
        Returns path for the CRL file path
        :return: string CRL path
        """
        return os.path.join(self.get_ejbca_home(), 'vpn', '%s.crl' % self.hostname)

    def vpn_get_server_cert_paths(self):
        """
        Returns VPN server paths
        :return: (ca, cert, key) paths
        """
        vpn_base = os.path.join(self.get_ejbca_home(), 'vpn')
        ca = os.path.join(vpn_base, 'VPN_Server-CA.pem')
        crt = os.path.join(vpn_base, 'VPN_Server.pem')
        key = os.path.join(vpn_base, 'VPN_Server-key.pem')
        return ca, crt, key

    #
    # LetsEncrypt & Cert
    #

    def get_keystore_path(self):
        return os.path.abspath(os.path.join(self.get_jboss_home(), self.JBOSS_KEYSTORE))

    def le_dns(self, domain=None, token=None, mdns=None, p=None, done=None, abort=None, *args, **kwargs):
        """
        DNS challenge solver for LE DNS verification
        :param domain:
        :param token:
        :param mdns:
        :param p:
        :param abort:
        :param args:
        :param kwargs:
        :return:
        """
        if domain is None or token is None:
            raise ValueError('Domain or token is none')
        if done is None:
            raise ValueError('Cannot signalize done - its None')

        # Prepare DNS TXT data for LE
        domain_parts = domain.split('.', 1)
        dns_data = self.reg_svc.txt_le_validation_dns_data((domain_parts[1], token))

        # Update domain DNS settings
        self.reg_svc.refresh_domain_call(dns_data=dns_data)

        # Call done callback
        done()

    def get_le_method(self, le_method=None):
        """
        Decides which method to use.
        :param le_method:
        :return:
        """
        return self.config.get_le_method(le_method=le_method)

    def le_enroll(self, le_method=None):
        """
        Enrolls to LetsEncrypt with specified domains
        :return:
        """

        # Password need to be stored anyway for future renewal / regeneration
        self.config.ejbca_jks_password = self.http_pass

        # If hostname is none/localhost, there is no point for lets encrypt here. Maybe later.
        if self.hostname is None or self.hostname == 'localhost':
            logger.info("Hostname is none/localhost, no letsencrypt operation will be performed")
            return 1

        if not self.check_hostname_domains_consistency():
            raise ValueError('Hostname not in domains, should not happen')

        self.lets_encrypt = letsencrypt.LetsEncrypt(email=self.config.email, domains=self.domains,
                                                    print_output=self.print_output, staging=self.staging)

        le_method = self.get_le_method(le_method=le_method)

        ret, out, err = -1, None, None
        if le_method == LE_VERIFY_DNS:
            mdns = self.lets_encrypt.manual_dns(expand=True, on_domain_challenge=self.le_dns)
            ret, out, err = mdns.start()
        else:
            ret, out, err = self.lets_encrypt.certonly()

        if ret != 0:
            return 2

        # LetsEncrypt certificate is OK. Create JKS.
        # Backup previous JKS, delete the old one
        jks_path = self.get_keystore_path()
        util.make_or_verify_dir(self.DB_BACKUPS)
        util.delete_file_backup(jks_path, chmod=0o600, backup_dir=self.DB_BACKUPS)

        # Create new JKS
        cert_dir = self.lets_encrypt.get_certificate_dir(self.hostname)
        self.lets_encrypt_jks = letsencrypt.LetsEncryptToJks(
            cert_dir=cert_dir,
            jks_path=jks_path,
            jks_alias=self.hostname,
            password=self.http_pass,
            print_output=self.print_output)

        ret = self.lets_encrypt_jks.convert()
        if ret != 0:
            return 3

        self.config.ejbca_domains = self.domains
        self.config.ejbca_hostname = self.hostname
        return 0

    def le_renew(self, le_method=None):
        """
        Renews LetsEncrypt certificate
        :return: 0 if certificate was renewed and JKS recreated, 1 if OK but no renewal was needed, error otherwise
        """
        self.lets_encrypt = letsencrypt.LetsEncrypt(email=self.config.email, domains=self.domains,
                                                    print_output=self.print_output, staging=self.staging)

        if self.lets_encrypt.is_certificate_ready(domain=self.hostname) != 0:
            return 2

        priv_file, cert_file, ca_file = self.lets_encrypt.get_cert_paths(domain=self.hostname)
        cert_time_before = util.get_file_mtime(cert_file)

        # Call letsencrypt renewal
        le_method = self.get_le_method(le_method=le_method)

        ret, out, err = -1, None, None
        if le_method == LE_VERIFY_DNS:
            mdns = self.lets_encrypt.manual_dns(expand=True, on_domain_challenge=self.le_dns)
            ret, out, err = mdns.start()
        else:
            ret, out, err = self.lets_encrypt.renew()

        if ret != 0:
            return 3

        cert_time_after = util.get_file_mtime(cert_file)
        if cert_time_before >= cert_time_after:
            return 1

        # LetsEncrypt certificate is OK. Create JKS.
        jks_path = self.get_keystore_path()
        util.delete_file_backup(jks_path, chmod=0o600, backup_dir=self.DB_BACKUPS)

        # Create new JKS
        cert_dir = self.lets_encrypt.get_certificate_dir(self.hostname)
        self.lets_encrypt_jks = letsencrypt.LetsEncryptToJks(
            cert_dir=cert_dir,
            jks_path=jks_path,
            jks_alias=self.hostname,
            password=self.http_pass,
            print_output=self.print_output)

        ret = self.lets_encrypt_jks.convert()
        if ret != 0:
            return 4

        self.config.ejbca_hostname = self.hostname
        return 0

    #
    # Actions
    #

    def undeploy(self):
        """
        Undeploys EJBCA installation
        :return:
        """
        self.jboss_undeploy()
        self.jboss_remove_datasource()
        self.jboss_rollback_ejbca()
        self.jboss_reload()

    def configure(self):
        """
        Configures EJBCA for installation deployment
        :return:
        """

        # 1. update properties file
        if self.print_output:
            print(" - Updating settings")
        self.update_properties()
        self.backup_passwords()
        if self.config is not None:
            self.config.ejbca_jks_password = self.http_pass
            self.config.ejbca_db_password = self.db_pass
            self.config.ejbca_p12master_password = self.master_p12_pass
            self.config.vpn_installed = self.do_vpn

        # Restart jboss - to make sure it is running
        if self.print_output:
            print("\n - Restarting application server, please wait")
        jboss_works = self.jboss_restart()
        if not jboss_works:
            print("\n Application server (JBoss) could not be restarted. Please, resolve the problem and start again")
            return 100

        # 2. Undeploy original EJBCA, make JBoss clean
        if self.print_output:
            print("\n - Preparing environment for application server")
        self.undeploy()

        # Restart jboss - so we can delete database after removal
        if self.print_output:
            print("\n - Restarting application server, please wait")
        jboss_works = self.jboss_restart()
        if not jboss_works:
            print("\n Application server could not be restarted. Please, resolve the problem and start again")
            return 100

        # Delete & backup database, fix privileges, reload.
        self.jboss_backup_database()
        self.jboss_fix_privileges()
        self.jboss_reload()

        # 3. deploy, 5 attempts
        for i in range(0, 5):
            if self.print_output:
                print("\n - Deploying the PKI system" if i == 0 else
                      "\n - Deploying the PKI system, attempt %d" % (i+1))
            res, out, err = self.ant_deploy()
            self.ejbca_install_result = res
            if res == 0:
                break

        if self.ejbca_install_result != 0:
            return 2

        # 4. install, 3 attempts
        for i in range(0, 3):
            if self.print_output:
                print(" - Installing the PKI system" if i == 0 else
                      " - Installing the PKI system, attempt %d" % (i+1))
            self.jboss_fix_privileges()
            self.jboss_wait_after_deploy()

            res, out, err = self.ant_install()
            self.ejbca_install_result = res
            if res == 0:
                break

        self.ant_client_tools()
        self.jboss_fix_privileges()

        if self.do_vpn:
            self.jboss_configure_rewrite_vpn()
        else:
            self.jboss_configure_rewrite_ejbca()

        self.jboss_reload()
        return self.ejbca_install_result

    def test_port_open(self, host, timeout=5, attempts=3, port=None):
        """
        Tests if port is open to the public
        :return:
        """
        if port is None:
            port = self.PORT

        return util.test_port_open(host=host, port=port, timeout=timeout, attempts=attempts,
                                   test_upper_read_write=False)

    def test_environment(self):
        """
        Tests if the host we run at has necessary assets (e.g., jboss dir, ejbca dir)
        Very light check, but prevents from running and failing on hosts without our jboss installation.
        :return: true if env is OK (installation could finish successfully)
        """
        return os.path.exists(self.get_ejbca_home()) and os.path.exists(self.get_jboss_home())

    def setup_os(self):
        """
        Configures OS
        Enables packet forwarding, sets up the masquerade
        :return:
        """
        ret = self.sysconfig.allow_port(port=self.PORT, tcp=True)
        if ret != 0:
            return ret

        ret = self.sysconfig.allow_port(port=self.PORT_PUBLIC, tcp=True)
        if ret != 0:
            return ret

        return 0
