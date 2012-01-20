#!/bin/bash -ex

# Drop any harness databases found on the system.

for db in `mysql -e 'show databases' -E | grep BugzillaInstance | cut -d' ' -f2`
do
    mysqladmin --force drop $db
done
