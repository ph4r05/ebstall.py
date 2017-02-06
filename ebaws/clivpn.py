#!/usr/bin/env python
# -*- coding: utf-8 -*-

from cmd2 import Cmd
import argparse
import sys
import os
import math
import types
import traceback
import pid
import time
import util
import errors
import textwrap
import openvpn
from blessed import Terminal
from consts import *
from core import Core
from config import Config, EBSettings
from registration import Registration, InfoLoader
from softhsm import SoftHsmV1Config
from ejbca import Ejbca
from ebsysconfig import SysConfig
from letsencrypt import LetsEncrypt
from ebclient.registration import ENVIRONMENT_PRODUCTION, ENVIRONMENT_DEVELOPMENT, ENVIRONMENT_TEST
from pkg_resources import get_distribution, DistributionNotFound
from cli import Installer
import logging
import coloredlogs


logger = logging.getLogger(__name__)
coloredlogs.install(level=logging.ERROR)


class VpnInstaller(Installer):
    """
    Extended installer - with VPN.
    """

    def __init__(self, *args, **kwargs):
        """
        Init core
        :param args:
        :param kwargs:
        :return:
        """
        Installer.__init__(self, *args, **kwargs)
        self.ovpn = None

    def init_argparse(self):
        """
        Adding new VPN related arguments
        :return:
        """
        parser = Installer.init_argparse(self)
        return parser

    def ask_for_email_reason(self, is_required=None):
        """
        Reason why we need email - required in VPN case.
        :param is_required:
        :return:
        """
        if is_required:
            self.tprint('We need your email address for:\n'
                        '   a) identity verification for EnigmaBridge account \n'
                        '   b) LetsEncrypt certificate registration'
                        '   c) PKI setup - VPN configuration')
            self.tprint('We will send you a verification email.')
            self.tprint('Without a valid e-mail address you won\'t be able to continue with the installation\n')
        else:
            raise ValueError('Email is required in VPN case')

    def do_init(self, line):
        self.tprint('Going to install VPN server backed by Enigma Bridge FIPS140-2 encryption service.\n')

        # EJBCA installation
        init_res = Installer.do_init(self, line)
        return init_res

    def init_main_try(self):
        """
        Main installer block, called from the global try:
        :return:
        """
        self.init_services()
        self.ovpn = openvpn.OpenVpn(sysconfig=self.syscfg)

        # Get registration options and choose one - network call.
        self.reg_svc.load_auth_types()

        # Show email prompt and intro text only for new initializations.
        res = self.init_prompt_user()
        if res != 0:
            self.return_code(res)

        # System check proceeds (mem, network).
        # We do this even if we continue with previous registration, to have fresh view on the system.
        # Check if we have EJBCA resources on the drive
        res = self.init_test_environment()
        if res != 0:
            self.return_code(res)

        # Determine if we have enough RAM for the work.
        # If not, a new swap file is created so the system has at least 2GB total memory space
        # for compilation & deployment.
        res = self.install_check_memory(syscfg=self.syscfg)
        if res != 0:
            return self.return_code(res)

        # Preferred LE method? If set...
        self.last_is_vpc = False

        # Lets encrypt reachability test, if preferred method is DNS - do only one attempt.
        # We test this to detect VPC also. If 443 is reachable, we are not in VPC
        res, args_le_preferred_method = self.init_le_vpc_check(self.get_args_le_verification(),
                                                               self.get_args_vpc(), reg_svc=self.reg_svc)
        if res != 0:
            return self.return_code(res)

        # User registration may be multi-step process.
        res, new_config = self.init_enigma_registration()
        if res != 0:
            return self.return_code(res)

        # Custom hostname for EJBCA - not yet supported
        new_config.ejbca_hostname_custom = False
        new_config.is_private_network = self.last_is_vpc
        new_config.le_preferred_verification = args_le_preferred_method

        # Assign a new dynamic domain for the host
        res, self.domain_is_ok = self.init_domains_check(reg_svc=self.reg_svc)
        new_config = self.reg_svc.config
        if res != 0:
            return self.return_code(res)

        # Install to the OS - cron job & on boot service
        res = self.init_install_os_hooks()
        if res != 0:
            return self.return_code(res)

        # Dump config & SoftHSM
        conf_file = Core.write_configuration(new_config)
        self.tprint('New configuration was written to: %s\n' % conf_file)

        # SoftHSMv1 reconfigure
        res = self.init_softhsm(new_config=new_config)
        if res != 0:
            return self.return_code(res)

        # EJBCA configuration
        res = self.init_install_ejbca(new_config=new_config)
        if res != 0:
            return self.return_code(res)

        # VPN setup
        self.ejbca.vpn_create_ca()
        self.ejbca.vpn_create_profiles()
        self.ejbca.vpn_create_server_certs()
        self.ejbca.vpn_create_crl()
        vpn_ca, vpn_cert, vpn_key = self.ejbca.vpn_get_server_cert_paths()

        # VPN server
        self.tprint('Installing & configuring VPN server')
        self.ovpn.install()
        self.ovpn.generate_dh_group()
        self.ovpn.configure_server()
        self.ovpn.store_server_cert(ca=vpn_ca, cert=vpn_cert, key=vpn_key)

        # VPN CRL
        crl_path = self.ejbca.vpn_get_crl_path()
        self.ovpn.configure_crl(crl_path=crl_path)

        # Starting VPN server
        self.ovpn.enable()
        self.ovpn.switch(start=True)
        self.ejbca.vpn_install_cron()

        # LetsEncrypt enrollment
        res = self.init_le_install()
        if res != 0:
            return self.return_code(res)

        self.tprint('')
        self.init_celebrate()
        self.cli_sleep(3)
        self.cli_separator()

        # Finalize, P12 file & final instructions
        new_p12 = self.ejbca.copy_p12_file()
        self.init_show_p12_info(new_p12=new_p12, new_config=new_config)

        # Test if main admin port of EJBCA is reachable.
        self.init_test_admin_port_reachability()

        self.cli_sleep(5)
        return self.return_code(0)


def main():
    app = VpnInstaller()
    app.app_main()


if __name__ == '__main__':
    main()

