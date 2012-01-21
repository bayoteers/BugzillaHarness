#!/usr/bin/env python
#
# The contents of this file are subject to the Mozilla Public
# License Version 1.1 (the "License"); you may not use this file
# except in compliance with the License. You may obtain a copy of
# the License at http://www.mozilla.org/MPL/
#
# Software distributed under the License is distributed on an "AS
# IS" basis, WITHOUT WARRANTY OF ANY KIND, either express or
# implied. See the License for the specific language governing
# rights and limitations under the License.
#
# The Original Code is the Dashboard Bugzilla Extension.
#
# The Initial Developer of the Original Code is "Nokia Corporation"
# Portions created by the Initial Developer are Copyright (C) 2010 the
# Initial Developer. All Rights Reserved.
#
# Contributor(s):
#   David Wilson <ext-david.3.wilson@nokia.com>
#

"""Manage setting up a Bugzilla installation from a template, creating a
database for that installation, installing extensions from Git, and running a
set of Selenium/WebDriver tests, before tearing it all down again.

Usage: bugzilla_harness.py <mode> [options] [args]

Options accepted by all modes:
    -v
        Enable debug output.

    --config=<path>
        Add the specified configuration file to the list of configuration files
        to be parsed. If no --config is specified, looks for
        "bugzilla_harness.conf" in the current directory. Subsequently
        specified configuration files override the values specified in earlier
        files.

    --extension=<name>:<revspec>
        When checking out an extension, use the specified revision rather than
        the tip of the default branch. This can be used from a post-commit
        script to force testing of the committed revision, rather than the
        whatever happens to be the new tip by the time the harness runs (i.e.
        to avoid a race condition).

    --extensions=[<name>[,...]]
        When creating a Bugzilla instance, install only the named extensions.
        If unspecified, defaults to installing all extensions listed in the
        harness configuration.

create <base_dir> [--extensions=...]
    Create a Bugzilla installation in the given path, and populate a MySQL
    database for it. If --extensions= is given, use only the specified list of
    extensions (may be empty), otherwise use the default extension list from
    the harness configuration path.

start <base_dir>
    Start an HTTP server for the given Bugzilla instance.

stop <base_dir>
    Stop the HTTP server running for the given Bugzilla instance.

destroy <base_dir>
    Destroy a Bugzilla installation in the given path, along with its
    associated MySQL database.

run [--instance=<path>] <suite_path> [...]
    Run a set of test suites against a Bugzilla instance, creating a temporary
    instance as desired. At least one test suite must be specified.

    --instance=<path>
        Rather than create a temporary Bugzilla instance, use the instance at
        <path> that was previously created using the "create" subcommand.
        Useful for speeding up repeated runs when debugging a failed test.

    <suite_path>
        Path to a Python script implementing the a suite, e.g.
        dashboard_tests.py.

"""

import atexit
import ConfigParser
import contextlib
import datetime
import hashlib
import inspect
import json
import logging
import optparse
import os
import re
import shutil
import signal
import socket
import struct
import subprocess
import sys
import tempfile
import time
import urllib
import urllib2
import urlparse
import xmlrpclib

from selenium import webdriver
from selenium.common.exceptions import NoSuchElementException
from selenium.webdriver.firefox import firefox_binary
from selenium.webdriver.firefox import firefox_profile


def find_free_port(host=None, desired=0):
    """Find a free TCP port on the local machine. If `desired` is given,
    indicates the port number specifically requested. Returns (host, port)
    tuple on success, or None on failure.
    """
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        s.bind((host or '0.0.0.0', desired))
    except socket.error, e:
        logging.warning('find_free_port: %s', e)
        return
    addr = s.getsockname()
    s.close()
    return addr


def get_public_ip():
    """Return the 'public' IP address of this machine. That is the address a
    socket binds to when talking to remote networks.
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
    """Search the system PATH for a file named `filename`, returning the full
    path on success, or None if not found.
    """
    s = os.environ.get('PATH')
    if not s:
        return

    for dirname in s.split(os.path.pathsep):
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


def mysql(config, prog, *args):
    """Run a MySQL utility program `prog` with authorization parameters
    added to the command line. Returns (stdout, stderr) tuple.
    """
    args = [prog,
            '-u' + config['mysql.username'],
            '-p' + config['mysql.password'],
            '-h' + config['mysql.hostname']] + list(args)
    logging.debug('Running %r', args)
    return run(args)


class TempJanitor(object):
    """Selenium developers seem to assume magical tree fairies come and clean
    up the directories created by tempfile.mkdtemp(), which in fact they don't.
    So here we monkey-patch mkdtemp() to keep a list of all directories
    created, so we can come along later and clean up.
    """
    def __init__(self):
        self.paths = []
        self.real_mkdtemp = tempfile.mkdtemp
        self.log = logging.getLogger('TempJanitor')

    def _fake_mkdtemp(self, *args, **kwargs):
        path = self.real_mkdtemp(*args, **kwargs)
        self.paths.append(path)
        return path

    def _cleanup(self):
        if not self.paths:
            return
        self.log.debug('Deleting %d temp directories...', len(self.paths))
        for path in self.paths:
            shutil.rmtree(path, ignore_errors=True)

    def install(self):
        tempfile.mkdtemp = self._fake_mkdtemp
        atexit.register(self._cleanup)


class Repository(object):
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

        # We use a file:// URL here to avoid a git-fetch warning when using
        # --depth.
        url = 'file://%s' % os.path.abspath(cache_dir)

        self.log.debug('Shallow cloning %r branch %r into %r',
            repo, branch, dest_dir)
        run(['git', 'clone', '-q', '-b', branch, '--depth', '1',
            url, dest_dir])


class X11Server(object):
    """Wraps Xvfb (X virtual framebuffer server) configuration up.
    """

    def __init__(self):
        """Create an instance.
        """
        self.log = logging.getLogger('X11Server')
        self.display = self._find_free_display()
        self.display_name = ':%s' % (self.display,)
        self.proc = None

    def _find_free_display(self):
        """Return the first unused X11 display number.
        """
        for display in range(20):
            if find_free_port(desired=6000 + display):
                return display

    def start(self):
        self.proc = subprocess.Popen(['Xvfb', self.display_name])
        self.log.debug('Xvfb runing on display %s', self.display_name)

    def stop(self):
        if self.proc:
            self.log.debug('Killing Xvfb PID %d', self.proc.pid)
            self.proc.terminate()


class CgiServer(object):
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
        self.state_dir = os.path.abspath(state_dir)
        self.doc_root = os.path.abspath(doc_root)
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
        proc = subprocess.Popen(args)

        if not wait_port_listen(self.host, self.port):
            raise Exception('httpd not listening on %s' % self.root_url)

        self.log.info('httpd listening on %s using PID %d',
            self.root_url, proc.pid)

    def stop(self):
        """Stop the server.
        """
        path = os.path.join(self.state_dir, 'lighttpd.pid')
        if not os.path.exists(path):
            self.log.warning("No PID file, can't stop.")
            return

        with file(path) as fp:
            pid = int(fp.read().strip())

        self.log.debug('Killing httpd PID %d', pid)
        os.kill(pid, signal.SIGTERM)

    def url(self, suffix, **kwargs):
        url = urlparse.urljoin(self.root_url, suffix)
        if kwargs:
            url += '?' + urllib.urlencode(kwargs)
        return url


class InstanceBuilder(object):
    """Manage everything to do with creating a Bugzilla installation.
    """
    def __init__(self, config, instance_id=None, base_dir=None):
        self.config = config
        self.instance_id = instance_id or make_id('BugzillaInstance')
        self.base_dir = base_dir or os.path.join(
            tempfile.gettempdir(), self.instance_id)

        self.repo = Repository(cache_dir='repo_cache',
            refresh_cache=not config['offline'])
        self.bz_dir = os.path.join(self.base_dir, 'bugzilla')

        self.perl_path = search_path('perl')
        self.arch = get_elf_arch(self.perl_path)

        self.log = logging.getLogger('InstanceBuilder')
        self.log.debug('Base directory: %r; ID: %r',
            self.base_dir, self.instance_id)

        if not os.path.exists(self.base_dir):
            os.mkdir(self.base_dir, 0755)

    def build(self):
        self.repo.clone(self.config['bugzilla.url'],
            self.config['bugzilla.branch'], self.bz_dir)
        self._write_state()

        self.instance = BugzillaInstance(self.config, self.base_dir)

        self._create_db()
        self._create_extensions()
        self._create_bz_lib()
        self._install_modules()
        self._run_checksetup()
        self._setup_localconfig()

        self.instance.set_param('upgrade_notification', 'disabled')
        self.log.debug('Bugzilla completely configured.')

        return self.instance

    def _write_state(self):
        path = os.path.join(self.base_dir, 'state.json')
        with file(path, 'wb') as fp:
            json.dump({
                'instance_id': self.instance_id
            }, fp)

    def _create_extensions(self):
        for name, repo, branch in get_extensions(self.config):
            path = os.path.join(self.instance.ext_dir, name)
            self.repo.clone(repo, branch, path)

    def _get_snapshot_path(self):
        """Return the path of the MySQL database snapshot matching this
        instance's Bugzilla version, or throw an exception if none exists.
        """
        path = os.path.join('db_snapshots', 'snapshot_%s.sql' %\
            (self.instance.version,))
        if os.path.exists(path):
            return path
        self.log.error('Snapshot for version %r not found at %r',
            self.instance.version, path)
        raise Exception('Missing database snapshot (see log).')

    def _create_db(self):
        """Create a MySQL database named after this Bugzilla instance, and
        restore an initial snapshot from the snapshots directory. Fails if no
        suitable snapshot is found.
        """
        mysql(self.config, 'mysqladmin', 'create', self.instance_id)
        mysql(self.config, 'mysql', self.instance_id,
            '-e', '\\. ' + self._get_snapshot_path())

    def _create_bz_lib(self):
        filename = 'lib_cache_%s_%s.zip' % (self.instance.version, self.arch)
        path = os.path.join(self.config['bugzilla.lib_cache_dir'], filename)

        if not os.path.exists(path):
            self.log.warning('No cached libdir %r found, install-module.pl '
                'will take longer than necessary!', path)
            return

        dest_dir = os.path.join(self.bz_dir, 'lib')
        self.log.debug('Extracting %r to %r', path, dest_dir)
        run(['unzip', path, '-d', dest_dir])

    def _install_modules(self):
        """Install any modules missing from the lib directory necessary for
        Bugzilla (and any installed extensions) to function.
        """
        stdout, stderr = run([self.perl_path, 'install-module.pl', '--all'],
            cwd=self.bz_dir)

        evil_strings = ['NOT OK']
        for s in evil_strings:
            if s in stdout or s in stderr:
                self.log.error('install_modules: evil string %r found in '
                    'stdout/stderr of install-module.pl')
                raise Exception('install-module.pl failed.')

    def _run_checksetup(self):
        """Run checksetup.pl, testing to see if the 'localconfig' settings file
        was created successfully at the end of the run.
        """
        stdout, stderr = run([self.perl_path, 'checksetup.pl', '-t'],
            cwd=self.bz_dir)
        if not os.path.exists(self.instance.bz_localconfig):
            self.log.error('_run_checksetup: %r was not created; '
                'stdout: %s\n\nstderr: %s', path, stdout, stderr)
            raise Exception('checksetup.pl failed.')
        return stdout, stderr

    def _setup_localconfig(self):
        """Update localconfig's DB connection parameters to match the MySQL
        connection specified in the harness configuration.
        """
        self.instance.set_config('db_name', self.instance_id)
        self.instance.set_config('db_user', self.config['mysql.username'])
        self.instance.set_config('db_pass', self.config['mysql.password'])
        self.instance.set_config('webservergroup', '')
        stdout, stderr = self._run_checksetup()

        must_have = 'Now that you have installed Bugzilla'
        if must_have not in stdout:
            self.log.error('setup_localconfig: string %r not found in '
                'checksetup.pl output.\nstdout: %s\n\nstderr: %s',
                must_have, stdout, stderr)
            raise Exception('setup_localconfig() failed.')


class BugzillaInstance(object):
    def __init__(self, config, base_dir):
        """Create an instance.
        """
        self.config = config
        self.base_dir = os.path.abspath(base_dir)

        self._read_state()

        self.log = logging.getLogger('BugzillaInstance')
        self.log.debug('Base directory: %r; ID: %r',
            self.base_dir, self.instance_id)

        self.bz_dir = os.path.join(self.base_dir, 'bugzilla')
        self.ext_dir = os.path.join(self.bz_dir, 'extensions')
        self.bz_localconfig = os.path.join(self.bz_dir, 'localconfig')

        self.version = get_bugzilla_version(self.bz_dir)

    def _read_state(self):
        path = os.path.join(self.base_dir, 'state.json')
        with file(path, 'rb') as fp:
            state = json.load(fp)
        self.instance_id = state['instance_id']

    def destroy(self):
        """If this is a temporary instance, destroy all its resources.
        """
        self.log.info('Destroying %r', self.base_dir)
        shutil.rmtree(self.base_dir, ignore_errors=True)
        mysql(self.config, 'mysqladmin', '--force', 'drop', self.instance_id)

    def set_config(self, key, value):
        """Add or update the value of a configuration variable in
        <bz_dir/localconfig>.
        """
        pattern = r'^\$' + re.escape(key)
        new_line = '$%s = %r;' % (key, value)
        edit_file(self.bz_localconfig, pattern, new_line)

    def set_param(self, key, value):
        """Insert (or update) a data/params key.
        """
        pattern = "'%s' => .*," % (key,)
        new_line = ",%r => %r," % (key, value)
        path = os.path.join(self.bz_dir, 'data/params')
        edit_file(path, pattern, new_line, 1)


class Suite(object):
    """Represents a suite of tests to run against a Bugzilla instance. This
    class has a similar interface to unittest.TestCase:

        setUp(): invoked before each test.
        tearDown(): invoked after each *successful* test.
        test*(): functions beginning with "test" are the actual tests: they are
            enumerated and executed by the TestRunner running the test.
        get*(): convenience functions that wrap "self.driver" methods to
            simplify navigation and fetching DOM elements.
        assert*(): functions that raise AssertionFailure if their condition is
            not met.

    Data members:
        self.config: Configuration dict parsed from the bugzilla_harness.conf
            files passed on the command line.
            
        self.server: CgiServer instance that's hosting the Bugzilla install.
            Useful for creating URLs (self.server.url(...)).

        self.instance: BugzillaInstance under test. Useful for access to
            set_config() and set_param() methods.

        self.driver: WebDriver instance controlling the web browser used for
            the test. Refer to "pydoc selenium.webdriver.Firefox" for the
            methods available.

        self.By: convenience alias for selenium.webdriver.common.by.By, to
            avoid having to import this manually.

        self.Command: convenience alias for
            selenium.webdriver.remote.command.Command.

    Example:
        class IndexPageSuite(bugzilla_harness.Suite):
            '''Test some features of index.cgi.
            '''
            def testLogin(self):
                self.loginAs('user@example.com', 'letmein')

            WELCOME_TEXT = 'Welcome to Bugzilla'
            def testWelcomeText(self):
                self.get('/')
                assert self.getByCss('h1').text == self.WELCOME_TEXT
    """
    # Allow "self.By" and "self.Command" instead of importing a morass of cack
    # into every test.
    from selenium.webdriver.common.by import By
    from selenium.webdriver.remote.command import Command

    def __init__(self, config, server, instance, driver):
        """Create an instance, using the configuration dict `config`, CgiServer
        `server`, BugzillaInstance `instance`, and WebDriver `driver`.
        """
        self.log = logging.getLogger(self.__class__.__name__)
        self.config = config
        self.server = server
        self.instance = instance
        self.driver = driver

    def setUp(self):
        """Called before each test* method; the default implementation simply
        ensures the Bugzilla instance is logged out.
        """
        self.driver.delete_all_cookies()

    def tearDown(self):
        """Called after each test* method, but only if the test didn't fail;
        the default implementation does nothing.
        """

    def _exec(self, cmd, **kwargs):
        # For commands, see code.google.com/p/selenium/wiki/JsonWireProtocol
        # and webdriver/remote/remote_connection.py.
        return self.driver.execute(getattr(self.Command, cmd), kwargs)['value']

    def screenshot(self, name, prefix='SCREENSHOT'):
        """Save a screenshot to the Bugzilla instance's base_dir.
        """
        now = datetime.datetime.now()
        ymd = now.strftime('%Y-%m-%d-%H%M%S')
        filename = '%s-%s-%s-%s.png' % (
            prefix, ymd, self.__class__.__name__, name)
        path = os.path.join(self.instance.base_dir, filename)
        self.driver.save_screenshot(path)

    #
    # Helpers.
    #

    def get_error_text(self):
        """Get the text of a Bugzilla error message as rendered by
        ThrowUserError() etc, otherwise None.
        """
        try:
            return self.getById('error_msg').text
        except NoSuchElementException:
            pass

    def get(self, path, **kwargs):
        """Navigate the browser to a URL belonging to the Bugzilla
        instance. `path` refers to the script or file to load that lives under
        BugzillaInstance.base_dir. `kwargs` are appended as GET query
        parameters.
        """
        return self.driver.get(self.server.url(suffix, **kwargs))

    def getById(self, elem_id):
        """Return a WebElement by DOM id= attribute.
        """
        return self.driver.find_element(self.By.ID, elem_id)

    def getByCss(self, sel):
        """Return a WebElement by CSS selector.
        """
        return self.driver.find_element(self.By.CSS_SELECTOR, sel)

    def allByCss(self, sel):
        """Return all WebElements matching a CSS selector.
        """
        return self.driver.find_elements(self.By.CSS_SELECTOR, sel)

    def assertEqual(self, a, b):
        """Fail the test if `a` != `b`.
        """
        assert a == b, '%r != %r' % (a, b)

    def assertNotEqual(self, a, b):
        """Fail the test if `a` == `b`.
        """
        assert a != b, '%r == %r' % (a, b)

    def assertError(self, msg):
        """Fail the test if the currently rendered web page does not include a
        Bugzilla error message whose text is `msg`.
        """
        text = self.get_error_text()
        assert text, 'Expected error %r, got none.' % (msg,)

    def assertNoError(self):
        """Fail the test if the currently rendered web page includes a Bugzilla
        error message.
        """
        text = self.get_error_text()
        assert not text, 'Expected no error, got: %r' % (text,)

    def assertRequiresLogin(self, suffix, **kwargs):
        """Navigate to a URL, then verify Bugzilla insisted on a logged in
        account.
        """
        self.get(suffix, **kwargs)
        self.assertError('You must log in before using this part of Bugzilla.')

    def assertRaises(self, klass, fn, *args, **kwargs):
        """Fail the test if `fn(*args, **kwargs)` does not raise an exception
        of class `klass`.
        """
        try:
            fn(*args, **kwargs)
        except klass, e:
            pass

    def assertFault(self, code, fn, *args, **kwargs):
        """Fail the test if `fn(*args, **kwargs)` does not raise an
        xmlrpclib.Fault with a faultCode == `code`.

        Example:
            self.assertFault(410, self.rpc_proxy.Bugzilla.extensions)
        """
        try:
            fn(*args, **kwargs)
        except xmlrpclib.Fault, e:
            assert e.faultCode == code, \
                '%r != %r' % (e.faultCode, code)
            return
        assert False, 'Function did not raise a fault.'

    @property
    def rpc_proxy(self):
        """An xmlrpclib.ServerProxy instance configured to talk to the Bugzilla
        XML-RPC server.
        """
        url = self.server.url('xmlrpc.cgi')
        return xmlrpclib.ServerProxy(url)

    def logout(self):
        """Destroy any logged in session by deleting any cookies.
        """
        self.driver.delete_all_cookies()

    def loginAs(self, username, password):
        """Create a Bugzilla session for `username` and `password`, failing the
        test if login couldn't be completed.
        """
        self.logout()
        self.driver.get(self.server.url(''))
        self.getById('login_link_top').click()

        elem = self.getById('Bugzilla_login_top')
        elem.click()
        elem.send_keys(username)

        elem = self.getById('Bugzilla_password_top')
        elem.click()
        elem.send_keys(password)

        self.getById('log_in_top').submit()
        self.assertNoError()

    def login(self):
        """Create a Bugzilla session for a regular user account. The user does
        not belong to any groups by default, therefore it's useful for testing
        permission checks, etc.
        """
        self.loginAs(self.config['bugzilla.user_login'],
                     self.config['bugzilla.user_password'])

    def loginAsAdmin(self):
        """Create a Bugzila session for an administrator user account. The
        administrator usually belongs to all groups by default.
        """
        self.loginAs(self.config['bugzilla.admin_login'],
                     self.config['bugzilla.admin_password'])


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
        sys.stderr.write('ERROR: %s\n' % (fmt,))
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
    add('--instance', help='Instance to use.')

    options, args = parser.parse_args(args)

    config = {}
    for path in options.config:
        config.update(parse_config(path))

    config['verbose'] = options.verbose
    config['offline'] = options.offline
    config['instance'] = options.instance

    return config, args


def create_instance(config, args):
    """'create' mode implementation: create a persistent Bugzilla install at
    the given path.
    """
    if len(args) != 1:
        usage('Create expects exactly one parameter.')
    base_dir, = args

    builder = InstanceBuilder(config, base_dir=base_dir)
    builder.build()


def start_instance(config, args):
    """'start' mode implementation: create a BugzillaInstance for the instance
    passed on the command line, then a CgiServer, call the server's start(),
    then modify the Bugzilla install's 'urlbase' parameter to point at the
    CgiServer's root URL.
    """
    if len(args) != 1:
        usage('Must specify exactly one instance path.')
    base_dir, = args

    instance = BugzillaInstance(config, base_dir=base_dir)
    server = CgiServer(instance.base_dir, instance.bz_dir,
        public=int(config['lighttpd.public']))
    server.start()
    instance.set_param('urlbase', server.root_url)


def stop_instance(config, args):
    """'stop' mode implementation: create a BugzillaInstance for the instance
    passed on the command line, then a CgiServer, then call the server's stop()
    method.
    """
    if len(args) != 1:
        usage('Must specify exactly one instance path.')
    base_dir, = args

    instance = BugzillaInstance(config, base_dir=base_dir)
    server = CgiServer(instance.base_dir, instance.bz_dir,
        public=int(config['lighttpd.public']))
    server.stop()


def destroy_instance(config, args):
    """'destroy' mode implementation: create a BugzillaInstance for the
    instance passed on the command line, then call its destroy() method.
    """
    if len(args) != 1:
        usage('Must specify exactly one instance path.')
    base_dir, = args

    instance = BugzillaInstance(config, base_dir=base_dir)
    instance.destroy()


def die(fmt, *args):
    if args:
        fmt %= args
    sys.stderr.write('%s: %s\n' % (sys.argv[0], fmt))
    raise SystemExit(1)


def get_suites(path):
    # Artificially inject this script as 'bugzilla_harness' module, otherwise a
    # separate copy will end up getting loaded by the test suite scripts, which
    # breaks the issubclass() tests before.
    sys.modules['bugzilla_harness'] = sys.modules['__main__']

    with file(path, 'rb') as fp:
        compiled = compile(fp.read(), path, 'exec')

    ns = {}
    eval(compiled, ns, ns)
    return [v for k, v in ns.iteritems()
            if inspect.isclass(v) and issubclass(v, Suite)]


class TestRunner(object):
    """Responsible for executing any configured test suites and collating their
    results. In addition to setting up the environment, launching the web
    browser and so on, also records failures and timing information.
    """
    def __init__(self, config, instance, suites):
        self.log = logging.getLogger('TestRunner')
        self.config = config
        self.instance = instance
        self.suites = suites
        self.failures = []

        TempJanitor().install()

        # Use a per-instance TMP because WebDriver is so ill-behaved (it calls
        # mkdtemp() continually without ever cleaning up, and it writes log
        # files with fixed names to TMP).
        self.temp_dir = tempfile.mkdtemp(prefix='TestRunner')
        # Same deal for firefox.WebDriver: it calls mkdtemp() indiscriminately.
        self.profile_dir = os.path.join(self.temp_dir, 'profile')

        # Initialized to None in case setup() doesn't complete.
        self.x11 = None
        self.server = None
        self.driver = None

    def setup(self):
        if os.path.exists(self.temp_dir):
            shutil.rmtree(self.temp_dir)
        os.mkdir(self.temp_dir, 0755)

        self.x11 = X11Server()
        self.x11.start()

        self.server = CgiServer(self.instance.base_dir,
            self.instance.bz_dir, public=int(self.config['lighttpd.public']))
        self.server.start()
        self.instance.set_param('urlbase', self.server.root_url)

        os.environ['DISPLAY'] = self.x11.display_name
        os.environ['TMP'] = self.temp_dir
        os.environ.pop('http_proxy', None)
        os.environ.pop('https_proxy', None)

        self._setup_firefox()

    def _setup_firefox(self):
        os.mkdir(self.profile_dir, 0755)
        self.binary = firefox_binary.FirefoxBinary('/usr/local/firefox/firefox-bin')
        self.profile = firefox_profile.FirefoxProfile(self.profile_dir)
        self.driver = webdriver.Firefox(firefox_binary=self.binary)

    def stop(self):
        if self.x11:
            self.x11.stop()
        if self.server:
            self.server.stop()
        if self.driver:
            try:
                self.driver.quit()
            except urllib2.URLError, e:
                self.log.debug('Caught dumb exception: %s', e)
        if os.path.exists(self.temp_dir):
            shutil.rmtree(self.temp_dir)

    def run_test(self, suite, method):
        self.log.info('Running %s..', method.func_name)
        try:
            suite.setUp()
            method()
            suite.tearDown()
        except Exception, e:
            self.log.exception('Method %s failed.', method.func_name)
            suite.screenshot(method.func_name, prefix='FAIL')
            self.failures.append((method.func_name, e))

    def _get_methods(self, suite):
        return [obj for name, obj in inspect.getmembers(suite)
                if name.startswith('test') and inspect.ismethod(obj)]

    def run_suite(self, suite):
        for method in self._get_methods(suite):
            self._run_one(method)

    def run(self):
        for klass in self.suites:
            suite = klass(self.config, self.server, self.instance, self.driver)
            suite.run()


def run_suite(config, args):
    """'run' mode implementation: load the Python scripts passed on the command
    line, create a temporary BugzillaInstance if necessary, then use TestRunner
    to execute the suites.
    """
    if not len(args):
        usage('Must specify at least one test suite path.')

    suites = []
    for path in args:
        suites.extend(get_suites(path))

    if not suites:
        die('None of the provided test suites export any Suite '
            'subclasses.')

    if config.get('instance'):
        instance = BugzillaInstance(config,
            base_dir=config['instance'])
    else:
        builder = InstanceBuilder(config)
        instance = builder.build()

    runner = TestRunner(config, instance, suites)
    try:
        runner.setup()
        runner.run()
    finally:
        runner.stop()
        # If no --instance= given on command line, then the instance we have
        # must be temporary, so destroy it.
        if not config.get('instance'):
            instance.destroy()


MODES = {
    'create': create_instance,
    'start': start_instance,
    'stop': stop_instance,
    'run': run_suite,
    'destroy': destroy_instance
}

def main():
    """Main program implementation. Parse the command line and configuration
    file, set up logging, then run one of the mode functions.
    """
    config, args = parse_args(sys.argv[1:])
    level = logging.DEBUG if config['verbose'] else logging.INFO
    logging.basicConfig(level=level)

    if not args:
        usage('Please specify a mode.')

    mode = args.pop(0)
    func = MODES.get(mode)
    if not func:
        usage('Invalid mode: %r', mode)

    func(config, args)

if __name__ == '__main__':
    main()
