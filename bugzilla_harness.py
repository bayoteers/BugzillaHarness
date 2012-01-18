#!/usr/bin/env python

"""Bugzilla test harness.

Manages setting up a skeleton Bugzilla installation from a template, creating a
database for that installation, installing various modules for it from Git,
then running a set of Selenium/WebDriver tests against the instance, before
tearing it all down again.

Usage: bugzilla_harness.py <mode> [args]

Where <mode> is "run":

    Execute one or more test suites.

"""

import ConfigParser
import contextlib
import hashlib
import inspect
import json
import logging
import optparse
import os
import re
import shutil
import socket
import struct
import subprocess
import sys
import tempfile
import time
import urllib
import urlparse

from selenium import webdriver
from selenium.webdriver.firefox import firefox_binary
from selenium.webdriver.remote.command import Command


def find_free_port(host=None, desired=0):
    """Find a free TCP port on the local machine. If `desired` is given,
    indicates the port number specifically requested. Returns (host, port)
    tuple on success, or None on failure.
    """
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        s.bind((host, desired))
    except socket.error:
        return
    addr = s.getsockname()
    s.close()
    return addr


def get_public_ip():
    """Return the 'public' IP address of this machine. That is the address a
    socket binds to by default when trying to talk to far away networks.
    """
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    with contextlib.closing(sock):
        # Address doesn't really matter; using Google DNS here. Since UDP is
        # connectionless, this call doesn't do aynthing except force the OS to
        # make a routing decision and assign an interface.
        sock.connect(('8.8.8.8', 53))
        return sock.getsockname()[0]


def wait_port_listen(host, port, tries=5, delay=1, initial=0.125, expect=True):
    """Repeatedly try connecting to a TCP port until it is in the LISTEN state.
    Returns True on success.
    """
    log = logging.getLogger('wait_port_listen')
    time.sleep(initial)

    for _ in xrange(tries):
        with contextlib.closing(socket.socket()) as sock:
            try:
                sock.connect((host, port))
                if expect:
                    return True
            except socket.error, e:
                log.warn('connect(%r, %d): %s', host, port, e)
                if not expect:
                    return False

        time.sleep(delay)


def search_path(filename):
    search_list = os.environ.get('PATH')
    if not search_list:
        return

    for dirname in search_list.split(os.path.pathsep):
        path = os.path.join(dirname, filename)
        if os.path.exists(path):
            return path


def get_elf_arch(path):
    """Return the architecture of some ELF object ('amd64', 'i386'). Used to
    detect the type of the perl interpreter (os.uname() returns the kernel's
    architecture, which may be wrong). Returns None on failure or unknown.
    """
    log = logging.getLogger('get_elf_arch')

    machs = {
        3: 'i386', # EM_386
        62: 'amd64', # EM_X86_64
    }

    fmt = '4s12sHH'
    with open(path, 'rb') as fp:
        data = fp.read(struct.calcsize(fmt))

    magic, _, obj_type, mach = struct.unpack(fmt, data)
    if magic != '\x7fELF':
        log.warn('%r is not an ELF object.', path)
        return

    return machs.get(mach)


def run(args, return_code=0, cwd=None):
    """Run a command specified as an argv array `args`, logging stdout/stderr
    and throwing an exception if its return code doesn't match `return_code`.
    Optionally run the command from the directory `cwd`. Returns (stdout,
    stderr) tuple on success.
    """
    log = logging.getLogger('run')
    proc = subprocess.Popen(args,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, cwd=cwd)

    stdout, stderr = proc.communicate()
    if proc.returncode != return_code:
        msg = 'Subprocess %r return code was %d, not %d as expected' %\
            (args, proc.returncode, return_code)
        msg += '\nStdout: ' + stdout
        msg += '\nStderr: ' + stderr
        raise Exception(msg)

    return stdout, stderr


def get_bugzilla_version(path):
    """Get the version of Bugzilla installed in some directory.
    """
    code = 'use Bugzilla::Constants; print BUGZILLA_VERSION;'
    stdout, stderr = run(['perl', '-e', code], cwd=path)
    log = logging.getLogger('get_bugzilla_version')
    log.debug('Bugzilla version in %r: %r', path, stdout)
    return stdout


def edit_file(path, pattern, new_line, insert_at=0):
    """Read a file, passing it as a string to the `editor` function, then
    replace the file's contents with the return value of the function.

    Example:
        # Make contents of /etc/passwd uppercase.
        edit_file('/etc/passwd', lambda s: s.upper())
    """
    match = re.compile(pattern).match

    with open(path, 'r+') as fp:
        lines = fp.readlines()
        if any(match(l) for l in lines):
            lines = [new_line if match(line) else line
                     for line in lines]
        else:
            lines.insert(len(lines) - insert_at, new_line + '\n')
        fp.seek(0)
        fp.truncate(0)
        fp.write(''.join(lines))


def make_id(prefix):
    """Return a 'unique' ID with the given prefix, based on the current system
    time.
    """
    return '%s_%s' % (prefix, int(time.time() * 1000))


def get_extensions(config):
    exts = []
    for section, items in config['section_items'].iteritems():
        if section.startswith('extension: '):
            name = section[11:]
            dct = dict(items)
            exts.append((name, dct['url'], dct['branch']))
    return exts


class Repository:
    """Manages a cache of remote repositories, primarily to speed up creation
    of local Bugzilla instances.
    """
    def __init__(self, cache_dir, refresh_cache=True):
        """Create an instance, storing cached repositories in `cache_dir`. If
        `refresh_cache` is False, make no attempt to refresh cached
        repositories (to speed up repeat runs while debugging).
        """
        self.cache_dir = cache_dir
        self.refresh_cache = refresh_cache
        self.log = logging.getLogger('Repository')

    def _cache_dir(self, url):
        """Given some repository URL, return the path to the directory its
        cached copy should be stored in.
        """
        return os.path.join(self.cache_dir,
            '%s.git' % hashlib.md5(url).hexdigest())

    def mirror(self, url, dest_dir):
        """Create a mirror of the Git repository at `url` in `dest_dir`.
        """
        self.log.debug('Making mirror of %r into %r', url, dest_dir)
        run(['git', 'clone', '--mirror', '-q', url, dest_dir])

    def fetch(self, repo, repo_dir):
        """Refresh the mirror of the Git repository at `url` in `dest_dir`.
        """
        self.log.debug('Updating mirror of %r in %r', repo, repo_dir)
        run(['git', 'fetch', '-q'], cwd=repo_dir)

    def clone(self, repo, branch, dest_dir):
        """Make a shallow clone of a git repository into `dest_dir`. Before
        making the clone, update a mirror of the source repository `repo`.
        """
        cache_dir = self._cache_dir(repo)

        if not os.path.exists(cache_dir):
            self.mirror(repo, cache_dir)
        elif self.refresh_cache:
            self.fetch(repo, cache_dir)

        self.log.debug('Shallow cloning %r branch %r into %r',
            repo, branch, dest_dir)
        run(['git', 'clone', '-q', '-b', branch, '--depth', '1',
            cache_dir, dest_dir])


class X11Server:
    """Wraps Xvfb (X virtual framebuffer server) configuration up.
    """
    def _find_free_display(self):
        """Return the first unused X11 display number.
        """
        for display in range(20):
            if find_free_port(desired=6000 + display):
                return display

    def __init__(self):
        """Create an instance.
        """
        self.display = self._find_free_display()
        self.display_name = ':%s' % (self.display,)

    def start(self):
        self.proc = subprocess.Popen(['Xvfb', self.display_name])
        self.log.debug('Xvfb runing on display %s', self.display_name)

    def stop(self):
        self.proc.terminate()


class CgiServer:
    """Wraps lighttpd up as a simple class that serves CGI scripts from some
    directory.
    """
    CONF_TEMPLATE = """
        server.document-root = "%(doc_root)s"
        server.errorlog = "%(state_dir)s/lighttpd.errors"
        server.pid-file = "%(state_dir)s/lighttpd.pid"
        server.bind = "%(host)s"
        server.port = %(port)s
        mimetype.assign = (".css" => "text/css")
        index-file.names = ("index.cgi",)
        static-file.exclude-extensions = (".pl",)
        server.modules += ("mod_cgi")
        cgi.assign = (".pl"  => "", ".cgi"  => "",)
    """

    def __init__(self, state_dir, doc_root, public):
        """Create an instance, storing sundry files in `state_dir`, and serving
        `doc_root` via HTTP.
        """
        self.state_dir = state_dir
        self.doc_root = doc_root
        self.public = public
        self.log = logging.getLogger('CgiServer')

        host = get_public_ip() if public else None
        self.host, self.port = find_free_port(host)
        self.root_url = 'http://%s:%d/' % (self.host, self.port)
        self.conf_path = os.path.join(self.state_dir, 'lighttpd.conf')

    def start(self):
        """Start the server.
        """
        with open(self.conf_path, 'w') as fp:
            fp.write(self.CONF_TEMPLATE % vars(self))

        args = ['lighttpd', '-f', self.conf_path, '-D']
        self.proc = subprocess.Popen(args)

        if not wait_port_listen(self.host, self.port):
            raise Exception('httpd not listening on %s' % self.root_url)

        self.log.info('httpd listening on %s using PID %d',
            self.root_url, self.proc.pid)

    def stop(self):
        """Stop the server.
        """
        self.log.debug('Killing httpd PID %d', self.proc.pid)
        self.proc.terminate()

    def url(self, suffix, **kwargs):
        url = urlparse.urljoin(self.root_url, suffix)
        if kwargs:
            url += '?' + urllib.urlencode(kwargs)
        return url


class BugzillaInstance:
    """Manage everything to do with creating a Bugzilla installation.
    """
    def __init__(self, config, repo):
        """Create an instance.
        """
        self.config = config
        self.repo = repo
        self.instance_id = make_id('BugzillaHarness')
        self.base_dir = os.path.join(tempfile.gettempdir(),
            self.instance_id)

        if not os.path.exists(self.base_dir):
            os.mkdir(self.base_dir, 0755)

        self.log = logging.getLogger('BugzillaInstance')
        self.log.debug('Base directory: %r; ID %s',
            self.base_dir, self.instance_id)

        self.perl_path = search_path('perl')
        self.bz_dir = os.path.join(self.base_dir, 'bugzilla')
        self.ext_dir = os.path.join(self.bz_dir, 'extensions')
        self.bz_localconfig = os.path.join(self.bz_dir, 'localconfig')

    def destroy(self):
        """If this is a temporary instance, destroy all its resources.
        """
        self.log.info('Destroying %r', self.base_dir)
        shutil.rmtree(self.base_dir, ignore_errors=True)
        self._mysql('mysqladmin', '--force', 'drop', self.instance_id)

    def create(self):
        self.repo.clone(self.config['bugzilla.url'],
            self.config['bugzilla.branch'], self.bz_dir)
        self.version = get_bugzilla_version(self.bz_dir)
        self.arch = get_elf_arch(self.perl_path)
        self._create_db()
        self._create_extensions()
        self._create_bz_lib()
        self._install_modules()
        self._run_checksetup()
        self._setup_localconfig()
        self.log.debug('Bugzilla completely configured.')

    def _create_extensions(self):
        for name, repo, branch in get_extensions(self.config):
            path = os.path.join(self.ext_dir, name)
            self.repo.clone(repo, branch, path)

    def _get_snapshot_path(self):
        path = os.path.join('db_snapshots', 'snapshot_%s.sql' % (self.version,))
        if os.path.exists(path):
            return path
        self.log.error('Snapshot for version %r not found at %r',
            self.version, path)
        raise Exception('Missing database snapshot (see log).')

    def _mysql(self, prog, *args):
        args = [prog,
                '-u' + self.config['mysql.username'],
                '-p' + self.config['mysql.password'],
                '-h' + self.config['mysql.hostname']] + list(args)
        self.log.debug('Running %r', args)
        return run(args)

    def _create_db(self):
        self._mysql('mysqladmin', 'create', self.instance_id)
        self._mysql('mysql', self.instance_id,
            '-e', '\\. ' + self._get_snapshot_path())

    def _create_bz_lib(self):
        filename = 'lib_cache_%s_%s.zip' % (self.version, self.arch)
        path = os.path.join(self.config['bugzilla.lib_cache_dir'], filename)

        if not os.path.exists(path):
            self.log.warning('No cached libdir %r found, install-module.pl '
                'will take longer than necessary!', path)
            return

        dest_dir = os.path.join(self.bz_dir, 'lib')
        self.log.debug('Extracting %r to %r', path, dest_dir)
        run(['unzip', path, '-d', dest_dir])

    def _install_modules(self):
        '''/usr/bin/perl install-module.pl --all'''
        stdout, stderr = run([self.perl_path, 'install-module.pl', '--all'],
            cwd=self.bz_dir)

        evil_strings = ['NOT OK']
        for s in evil_strings:
            if s in stdout or s in stderr:
                self.log.error('install_modules: evil string %r found in '
                    'stdout/stderr of install-module.pl')
                raise Exception('install-module.pl failed.')

    def _run_checksetup(self):
        stdout, stderr = run([self.perl_path, 'checksetup.pl', '-t'],
            cwd=self.bz_dir)
        if not os.path.exists(self.bz_localconfig):
            self.log.error('create_localconfig: %r was not created; '
                'stdout: %s\n\nstderr: %s', path, stdout, stderr)
            raise Exception('checksetup.pl failed.')
        return stdout, stderr

    def set_config(self, key, value):
        """Add or update the value of a configuration variable in
        <bz_dir/localconfig>.
        """
        pattern = r'^\$' + re.escape(key)
        new_line = '$%s = %r;' % (key, value)
        edit_file(self.bz_localconfig, pattern, new_line)

    def _setup_localconfig(self):
        self.set_config('db_name', self.instance_id)
        self.set_config('db_user', self.config['mysql.username'])
        self.set_config('db_pass', self.config['mysql.password'])
        self.set_config('webservergroup', '')
        stdout, stderr = self._run_checksetup()

        must_have = 'Now that you have installed Bugzilla'
        if must_have not in stdout:
            self.log.error('setup_localconfig: string %r not found in '
                'checksetup.pl output.\nstdout: %s\n\nstderr: %s',
                must_have, stdout, stderr)
            raise Exception('setup_localconfig() failed.')

    def set_param(self, key, value):
        """Insert (or update) a data/params key.
        """
        pattern = "'%s' => .*," % (key,)
        new_line = ",%r => %r," % (key, value)
        path = os.path.join(self.bz_dir, 'data/params')
        edit_file(path, pattern, new_line, 1)


class Suite:
    """A test suite.
    """
    def __init__(self, config, server, bz):
        self.log = logging.getLogger(self.__class__.__name__)
        self.config = config
        self.server = server
        self.bz = bz
        self.failures = []

    def assertRequiresLogin(self, suffix, **kwargs):
        url = self.server.url(suffix, **kwargs)
        self.driver.get(url)
        text = self.driver.execute(Command.GET_ELEMENT_TEXT, 'error_msg')
        from pprint import pprint
        pprint(text)
        assert text == 'You must log in before using this part of Bugzilla.'
        exit()

    def _get_methods(self):
        return [obj for name, obj in inspect.getmembers(self)
                if name.startswith('test') and inspect.ismethod(obj)]

    def _run_one(self, method):
        try:
            method()
        except Exception, e:
            self.log.exception('Method %s failed.', method.func_name)
            self.failures.append((method.func_name, e))

    def run(self):
        for method in self._get_methods():
            self._run_one(method)


def parse_config(path):
    """Parse an INI file into a dictionary like:
    
        {'section.option': 'value',
         'section.option2': 'value2'}

    This eases writing tests simply by passing regular dicts as configuration,
    instead of manually populating a ConfigParser. Returned dict contains a
    magical 'section_items' key which contains all items for a given section,
    to allow enumeration.
    """
    parser = ConfigParser.RawConfigParser()
    with open(path) as fp:
        parser.readfp(fp)

    items = {}
    dct = {'section_items': items}

    for section in parser.sections():
        items[section] = parser.items(section)
        dct.update(('%s.%s' % (section, option), value)
                   for (option, value) in items[section]
                   if value)

    return dct


def usage(fmt, *args):
    sys.stderr.write(__doc__)
    if args:
        fmt %= args
    if fmt:
        sys.stderr.write('%s: ERROR: %s\n' % (sys.argv[0], fmt))
    raise SystemExit(1)


def parse_args(args):
    """Parse command-line arguments, returning a dict whose keys should match
    those found in the configuration file.
    """
    parser = optparse.OptionParser()
    add = parser.add_option

    add('-c', '--config', help='Pass an additional config file. Options '
        'specified in this file will override the defaults', metavar='file',
        action="append", default=["bugzilla_harness.conf"])
    add('-o', '--offline', help='Work offline (i.e. don\'t try to refresh '
        'git repositories)', metavar='offline', action='store_true')
    add('-v', '--verbose', help='Increase log verbosity (debug mode)',
        action="store_true", default=False)

    options, args = parser.parse_args(args)

    config = {}
    for path in options.config:
        config.update(parse_config(path))

    config['verbose'] = options.verbose
    config['offline'] = options.offline

    if not args:
        usage('Please specify a mode.')

    mode = args.pop(0)
    if mode == 'run':
        if not len(args):
            usage('Must specify at least one test suite filename with "run".')
    else:
        usage('Mode %r is invalid.', mode)

    config['mode'] = mode
    config['args'] = args
    return config


def run_tests(config):
    x11 = X11Server()
    x11.start()

    os.environ['DISPLAY'] = x11.display_name
    os.environ.pop('http_proxy', None)
    os.environ.pop('https_proxy', None)

    binary = firefox_binary.FirefoxBinary('/usr/local/firefox/firefox-bin')
    driver = webdriver.Firefox(firefox_binary=binary)

    binary.kill()


def main():
    """Main program implementation.
    """
    config = parse_args(sys.argv[1:])

    level = logging.DEBUG if config['verbose'] else logging.INFO
    logging.basicConfig(level=level)

    repo = Repository(cache_dir='repo_cache',
        refresh_cache=not config['offline'])

    bz = BugzillaInstance(config, repo)
    bz.create()

    server = CgiServer(bz.base_dir, bz.bz_dir,
        public=int(config['lighttpd.public']))
    server.start()

    bz.set_param('upgrade_notification', 'disabled')
    bz.set_param('urlbase', server.root_url)

    try:
        run_tests(config, server, bz)
    finally:
        server.stop()
        bz.destroy()

if __name__ == '__main__':
    main()
