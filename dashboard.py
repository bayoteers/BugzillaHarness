#!/usr/bin/env python

"""
Simple test suite for Dashboard.
"""

from bugzilla_harness import Suite

from selenium.webdriver.remote.command import Command



class DashboardSuite(Suite):
    REQUIRE_EXTENSIONS = ['Dashboard']

    def testRequiresLogin(self):
        self.assertRequiresLogin('page.cgi', id='dashboard.html')


    '''
    DB = url('page.cgi?id=dashboard.html')
    print driver.get(DB)
    el = driver.find_element_by_class_name('throw_error')
    help(type(el))
    assert el.text == 'You must log in before using this part of Bugzilla.'
    '''
