# bugzilla_harness

bugzilla_harness is a tool for running a suite of
[Selenium](http://www.seleniumhq.com/) tests using Firefox, against a set of
Bugzilla extensions and a Bugzilla instance configured for MySQL. The Bugzilla
code, along with extension code, are expected to be stored in Git repositories.
The harness creates a totally fresh installation for each test invocation, to
ensure repeatability of test results.

A web server is needed to host the CGIs; bugzilla_harness uses lighttpd for
this purpose, as it is possible to run it without root privileges.
Unfortunately Apache *insists* on setuid() calls, even when configured to run
under the current UID.


## Requirements

The following software needs to be installed and available on the system
path (depending on operating system).

 * **lighttpd**

   Required to host the Bugzilla CGIs.

       apt-get install lighttpd ; rm /etc/rc2.d/*light* # lighttpd

 * **Xvfb**

   On Linux, required in order to execute Firefox without a real X11
   server.

        apt-get install xvfb # Xvfb

 * **MySQL**

   Required in order to host the Bugzilla MySQL database. Any MySQLd will
   do, as long as you have an account that can create new databases.

        apt-get install mysql-server # Needs a MySQLd somewhere
        apt-get install mysql-client # mysqladmin, mysql

 * **Info-Zip**

   Required for unpacking cached library snapshots.

        apt-get install unzip

  * **Selenium**

    The selenium.webdriver Python package is required.

        easy_install selenium

  * **Python >= 2.6**

    bugzilla_harness is written in Python.


## Synopsis

The general plan is to have everything required by a test run to live under
configuration control, and to collect any logs (including screenshots) during
such a run. When a failure occurs, it should be easy to recreate the state of
the failed test.

Runs should execute automatically as part of a CI system, however the tool


## Writing Tests

Suites are written just like you'd write a unittest.TestCase, the API is almost
identical. Various convenience methods are provided, refer to "pydoc
bugzilla_harness.Suite" for full documentation.


## Running Tests

Here are some example approaches to running the harness.


### Running against the latest version directly from the command line.

This is useful for debugging a failing test, or while writing tests. First of
all, use the "create" option to create a persistent Bugzilla installation. This
saves a lot of time during test runs, as a temporary install doesn't need to be
configured first.

After "create" completes, use "run" to execute your modified tests. When you're
done, use "destroy" to destroy the Bugzilla installation.

    $ ./bugzilla_harness.py create my_bz
    $ ./bugzilla_harness.py run --instance=my_bz -v dashboard.py
    ...
    # Test failed, so lets fix some stuff..
    $ vim my_bz/bugzilla/extensions/Dashboard/Extension.pm
    $ git commit -m "Fix bug." my_bz/bugzilla/extensions/Dashboard
    $ git push my_bz/bugzilla/extensions/Dashboard
    ...
    $ ./bugzilla_harnesss.py destroy my_bz


### Running against a specific extension version, in isolation, from CI.

This is useful for testing an extension at each revision from a continuous
integration system. In this case, a shell script will probably wrap
bugzilla_harness.py to convert the CI's parameters into a form useful for
bugzilla_harness.py, e.g.:

    $ cat run_dashboard_isolated_test.py
    #!/bin/bash
    REV=$1 # Revision to test passed by CI system.

    ./bugzilla_harness.py run \
      --extensions=Dashboard \
      --extension=Dashboard:$REV


### Running against a specific extension version, from CI.

This is designed to test a specific version, but running with all other
extension modules configured. This is useful to discover template and database
conflicts resulting from mutually unaware extensions. In this mode, all
extensions are checked out at their HEAD revision, except the one under test.

    $ cat run_dashboard_integrated_test.py
    #!/bin/bash
    REV=$1 # Revision to test passed by CI system.

    ./bugzilla_harness.py run \
      --extension=Dashboard:$REV

