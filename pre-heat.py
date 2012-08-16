#!/usr/bin/env python2.7
# vi: se nu ts=4 sw=4 et:

import argparse
import ConfigParser
import json
import multiprocessing
import os
import sgmllib
import signal
import StringIO
import sys
import time
import urllib2
import urlparse

from datetime import datetime, timedelta

# config defaults
defaults = StringIO.StringIO("""\
[files]
to_scan: to_scan.urls
scanned: scanned.csv
errors : errors.csv

[workers]
pool_size: 4

[criteria]
url_roots:
    /

[display]
urls   : True
scanned: True
to_scan: True
errors : True
every  : 1
timing : True
""")

class LinkParser(sgmllib.SGMLParser):
    def __init__(self, verbose=0):
        sgmllib.SGMLParser.__init__(self, verbose)
        self.hyperlinks = []

    def get_hyperlinks(self):
        return self.hyperlinks

    def parse(self, s):
        self.feed(s)
        self.close()

    def start_a(self, attributes):
        for name, value in attributes:
            if name == 'href':
                self.hyperlinks.append(value)


def read_in():
    global ftoscan, fscanned, ferrors, error_count
    print 'Reading input files...'
    try:
        if not os.path.exists(errors_filename):
            ferrors = open(errors_filename, 'w')
        else:
            ferrors = open(errors_filename, 'r+')
            for line in ferrors:
                error_count += 1
        if not os.path.exists(scanned_filename):
            fscanned = open(scanned_filename, 'w')
        else:
            fscanned = open(scanned_filename, 'r+')
            for line in fscanned:
                target, duration = line.strip().split(',')
                scanned.add(target)
        ftoscan = open(to_scan_filename, 'r+')
        for line in ftoscan:
            target = line.strip()
            if not target in scanned and not target in toscan:
                toscan.append(target)
    except Exception as e:
        print 'Error reading input file: {0}'.format(e)


def write_out():
    print 'Writing output files...'
    exception = False
    try:
        fscanned.flush()
        fscanned.close()
    except Exception as e:
        exception = True
        print 'Error writing scanned urls: {0}'.format(e)
    try:
        ftoscan.flush()
        ftoscan.close()
    except Exception as e:
        exception = True
        print 'Error updating to_scan urls: {0}'.format(e)
    try:
        ferrors.flush()
        ferrors.close()
    except Exception as e:
        exception = True
        print 'Error writing error log file: {0}'.format(e)
    if exception:
        sys.exit(1)
    print 'Done.'


def signal_handler(signal, frame):
    print '\n\nCaught Shutdown Signal\n'
    write_out()
    sys.exit(1)


def criteria_check(link):
    # verify link begins with at least one root url
    for root in url_roots:
        if link.startswith(root):
            return True
    return False


def pull_html(target):
    '''
        this is the slow function we distribute to multiple worker processes
    '''
    url = urlparse.urlparse(target)
    found_links = []
    error = None
    pull_start = datetime.now()
    try:
        response = urllib2.urlopen(target)
        page = response.read()
        parser.parse(page)
        found_links = parser.get_hyperlinks()
    except Exception as e:
        error = '{0}'.format(e)

    pull_duration = datetime.now() - pull_start
    return (target, found_links, error, pull_duration)


def start_worker():
    signal.signal(signal.SIGINT, signal.SIG_IGN)

def kill_worker():
    sys.exit(1)


if __name__ == '__main__':

    parser = argparse.ArgumentParser()
    parser.add_argument('-c',  '--config',  help='path to config file')
    args = parser.parse_args()

    if args.config:
        config_file = args.config
    else:
        config_file = 'pre-heat.cfg'

    # config overrides
    config = ConfigParser.ConfigParser()
    config.readfp(defaults)
    config.read(config_file)
    to_scan_filename = config.get('files', 'to_scan')
    scanned_filename = config.get('files', 'scanned')
    errors_filename  = config.get('files', 'errors')
    pool_size = int(config.get('workers', 'pool_size'))
    config_url_roots = config.get('criteria', 'url_roots').split()
    display_urls = 'True' == config.get('display', 'urls')
    display_scanned = 'True' == config.get('display', 'scanned')
    display_to_scan = 'True' == config.get('display', 'to_scan')
    display_errors = 'True' == config.get('display', 'errors')
    display_every = int(config.get('display', 'every'))
    display_timing = 'True' == config.get('display', 'timing')

    print "Spinning up {0} workers".format(pool_size)

    toscan = []
    scanned = set([])
    errors = []
    parser = LinkParser()

    signal.signal(signal.SIGINT, signal_handler)

    ftoscan = None
    fscanned = None
    ferrors = None
    error_count = 0
    read_in()
    if not len(toscan) > 0:
        print "nothing to scan."
        sys.exit(1)
    else:
        url = urlparse.urlparse(toscan[0])
        url_roots = []
        print 'Gathering URLs beginning with any of the following roots:'
        for root in config_url_roots:
            root = url[0] + '://' + url[1] + root
            url_roots.append(root)
            print '  {0}'.format(root)
        print ''

    pool = multiprocessing.Pool(processes=pool_size,
                                initializer=start_worker,
                                maxtasksperchild=256,
                                )

    start = datetime.now()
    count = len(scanned)
    longest = timedelta(0)
    shortest = timedelta(1)
    average = timedelta(0)

    while toscan:
        chunk_size = pool_size
        chunk = []
        new_list = []

        for i in range(chunk_size):
            try:
                address = toscan.pop()
            except IndexError:
                chunk_size = len(chunk)
                break
            chunk.append(address)

        it = pool.imap(pull_html, chunk)
        for i in range(chunk_size):
            target, found_links, error, target_duration = it.next()

            url = urlparse.urlparse(target)
            if error:
                ferrors.write(','.join([target, str(error), str(target_duration)]) + '\n')
                error_count += 1
                print target
                print '  ** Error: {0}'.format(error)
                print ''
                continue
            for link in found_links:
                # convert links to full urls for criteria checking
                if link.startswith('/'):
                    link = url[0] + '://' + url[1] + link
                elif link.startswith('#'):
                    continue
                elif link.startswith('http') or link.startswith('https'):
                    link = link
                else:
                    link = urlparse.urljoin(url.geturl(),link)

                # verify link meets criteria before adding to list
                if not criteria_check(link):
                    continue

                if not link in scanned and not link in toscan and not link in chunk:
                    ftoscan.write(link + '\n')
                    toscan.append(link)

            fscanned.write(','.join([target, str(target_duration)]) + '\n')
            scanned.add(target)
            count += 1
            if target_duration < shortest:
                shortest = target_duration
            if target_duration > longest:
                longest = target_duration
            average += (target_duration - average) / count
            if display_urls:
                print target
            if count % display_every == 0:
                if display_scanned:
                    print '  scanned: {0}'.format(count)
                if display_to_scan:
                    print '  to-scan: {0}'.format(len(toscan))
                if display_errors:
                    if error_count > 0:
                        print '  errors:  {0}'.format(error_count)
                if display_timing:
                    print '  link time  : {0}'.format(str(target_duration)[:-4])
                    print '    average  : {0}'.format(str(average)[:-4])
                    print '    fastest  : {0}'.format(str(shortest)[:-4])
                    print '    slowest  : {0}'.format(str(longest)[:-4])
                    print '  total time : {0}'.format(str(datetime.now() - start)[:-4])
                fscanned.flush()
                ftoscan.flush()
                ferrors.flush()
                print ''

    pool.close()
    pool.join()
    write_out()
    print "*** total time: {0}".format(str(datetime.now() - start)[:-4])

