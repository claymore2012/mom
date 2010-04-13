#! /usr/bin/env python

import signal
import time
import os
import re
from optparse import OptionParser
import ConfigParser
import logging
import logging.handlers
from libvirtInterface import libvirtInterface
from HostMonitor import HostMonitor
from GuestManager import GuestManager
from Controllers.SystemController import SystemController
from Controllers.Rules import read_rules

config = None
def read_config(fname, options):
    global config
    config = ConfigParser.SafeConfigParser()
    # Set defaults
    config.add_section('main')
    config.set('main', 'main-loop-interval', '60')
    config.set('main', 'host-monitor-interval', '5')
    config.set('main', 'guest-manager-interval', '5')
    config.set('main', 'guest-monitor-interval', '5')
    config.set('main', 'system-controller-interval', '10')
    config.set('main', 'sample-history-length', '10')
    config.set('main', 'libvirt-hypervisor-uri', '')
    config.set('main', 'controllers', 'Balloon')
    config.set('main', 'plot-dir', '')
    config.add_section('logging')
    config.set('logging', 'log', 'stdio')
    config.set('logging', 'verbosity', 'info')
    config.set('logging', 'max-bytes', '2097152')
    config.set('logging', 'backup-count', '5')
    config.add_section('host')
    config.set('host', 'collectors', 'HostMemory')
    config.add_section('guest')
    config.set('guest', 'collectors', 'GuestQemuProc, GuestLibvirt')
    config.read(fname)
    
    # Process command line overrides
    if options.plot_dir is not None:
        config.set('main', 'plot-dir', options.plot_dir)
    if options.log is not None:
        config.set('logging', 'log', options.log)
    if options.verbosity is not None:
        config.set('logging', 'verbosity', options.verbosity)

    # Add non-customizable thread-global variables
    config.set('main', 'running', '0')
    plot_subdir = get_plot_subdir(config.get('main', 'plot-dir'))
    config.set('main', 'plot-subdir', plot_subdir)

def configure_logger(config):    
    logger = logging.getLogger()
    
    verbosity = config.get('logging', 'verbosity').lower()
    if verbosity == '5' or verbosity == 'debug':
        level = logging.DEBUG
    elif verbosity == '4' or verbosity == 'info':
        level = logging.INFO
    elif verbosity == '3' or verbosity == 'warn':
        level = logging.WARN
    elif verbosity == '2' or verbosity == 'error':
        level = logging.ERROR
    elif verbosity == '1' or verbosity == 'critical':
        level = logging.CRITICAL
    else:
        level = logging.DEBUG
    logger.setLevel(level)

    log = config.get('logging', 'log')
    if log.lower() == 'stdio':
        handler = logging.StreamHandler()
    else:
        print "logging to file %s" % log
        bytes = config.getint('logging', 'max-bytes')
        backups = config.getint('logging', 'backup-count')
        handler = logging.handlers.RotatingFileHandler(log, 'a', bytes, backups)
    handler.setLevel(level)
    formatter = logging.Formatter("%(asctime)s - %(name)s - %(levelname)s - %(message)s")
    handler.setFormatter(formatter)
    logger.addHandler(handler)

def get_plot_subdir(basedir):
    """
    Create a new directory for plot files inside basedir.  The name is in the
    format: momplot-NNN where NNN is an ascending sequence number.
    Return: The new directory name or '' on error.
    """
    if basedir == '':
        return ''

    logger = logging.getLogger('mom')
    regex = re.compile('^momplot-(\d{3})$')
    try:
        names = os.listdir(basedir)
    except OSError as e:
        logger.warn("Cannot read plot-basedir %s: %s", basedir, e.strerror)
        return ''
    seq_num = -1
    for name in names:
        m = regex.match(name)
        if m is not None:
            num =  int(m.group(1))
            if num > seq_num:
                seq_num = num
    seq_num = seq_num + 1
    dir = "%s/momplot-%03d" % (basedir, seq_num)
    if seq_num > 999:
        logger.warn("Cannot create plot-dir because the sequence number "\
              "is out of range.  Clear the directory or choose a different one")
        return ''
    try:
        os.mkdir(dir)
    except OSError as e:
        logger.warn("Cannot create plot-dir %s: %s", dir, e.strerror)
        return ''
    return dir

def signal_quit(signum, frame):
    global config
    logger = logging.getLogger('mom')
    logger.info("Received signal %i shutting down.", signum)
    config.set('main', 'running', '0')

def threads_ok(threads):
    """
    Check to make sure a list of expected threads are still alive
    """
    for t in threads:
        if not t.is_alive():
            return False
    return True
    
def wait_for_thread(t, timeout):
    """
    Join a thread only if it is still running
    """
    if t.is_alive():
        t.join(timeout)

def main():
    global config

    cmdline = OptionParser()
    cmdline.add_option('-c', '--config-file', dest='config_file',
                       help='Load configuration from FILE', metavar='FILE',
                       default='/etc/mom.conf')
    cmdline.add_option('-r', '--rules-file', dest='rules_file', default='',
                       help='Load rules from FILE', metavar='FILE')
    cmdline.add_option('-p', '--plot-dir', dest='plot_dir',
                       help='Save data plot files in DIR', metavar='DIR')
    cmdline.add_option('-l', '--log', dest='log', metavar='TARGET',
                       help='Set the log to TARGET (stdout, or <file>')
    cmdline.add_option('-v', '--verbose', dest='verbosity', metavar='LEVEL',
                       help='Set logging verbosity to LEVEL (0-4)')
    
    (options, args) = cmdline.parse_args()
    read_config(options.config_file, options)
    configure_logger(config)
    logger = logging.getLogger('mom')
    rules = read_rules(options.rules_file)

    signal.signal(signal.SIGINT, signal_quit)
    signal.signal(signal.SIGTERM, signal_quit)

    # Set up a shared libvirt connection
    uri = config.get('main', 'libvirt-hypervisor-uri')
    libvirt_iface = libvirtInterface(uri)

    # Start threads
    logger.debug("Daemon starting")
    config.set('main', 'running', '1')
    host_monitor = HostMonitor(config)
    guest_manager = GuestManager(config, libvirt_iface)
    system_controller = SystemController(config, rules, libvirt_iface, \
                            host_monitor, guest_manager)

    interval = config.getint('main', 'main-loop-interval')
    while config.getint('main', 'running') == 1:
        time.sleep(interval)
        if not threads_ok((host_monitor,guest_manager,system_controller)):
            config.set('main', 'running', '0')

    wait_for_thread(system_controller, 10)
    wait_for_thread(guest_manager, 5)
    wait_for_thread(host_monitor, 5)
    logger.info("Daemon ending")
    exit(0)

if __name__ == "__main__":
    main()
