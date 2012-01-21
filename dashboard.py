#!/usr/bin/env python

"""Simple test suite for Dashboard.
"""

import bugzilla_harness

URL = 'page.cgi?id=dashboard.html'
FAULT_REQUIRES_LOGIN = 410


class DashboardTestCase(bugzilla_harness.TestCase):
    def testRequiresLogin(self):
        """Ensure the Dashboard page.cgi template insists on a logged in
        account.
        """
        self.assertRequiresLogin(URL)

    def testRpcRequiresLogin(self):
        """Ensure all the Dashboard web service methods require a logged in
        account.
        """
        methods = ['delete_overlay', 'get_overlay', 'get_overlays',
                   'publish_overlay', 'set_overlay', 'clone_overlay',
                   'get_feed']
        for method in methods:
            self.assertFault(FAULT_REQUIRES_LOGIN,
                getattr(self.xmlrpc.Dashboard, method))

    def testPageLoads(self):
        self.login()
        self.get(URL)
        assert self.getByCss('title').text == 'Bugzilla Dashboard'
