#!/usr/bin/python
# -*- coding: utf-8 -*-
# Copyright (c) 2015 SUSE Linux GmbH
#
# Permission is hereby granted, free of charge, to any person obtaining a copy
# of this software and associated documentation files (the "Software"), to deal
# in the Software without restriction, including without limitation the rights
# to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
# copies of the Software, and to permit persons to whom the Software is
# furnished to do so, subject to the following conditions:
#
# The above copyright notice and this permission notice shall be included in
# all copies or substantial portions of the Software.
#
# THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
# IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
# FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
# AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
# LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
# OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
# SOFTWARE.

import argparse
import itertools
import logging
import sys
from xml.etree import cElementTree as ET

import osc.conf
import osc.core
import urllib2
import sys

from osclib.memoize import memoize

OPENSUSE = 'openSUSE:42'

makeurl = osc.core.makeurl
http_GET = osc.core.http_GET
http_DELETE = osc.core.http_DELETE
http_PUT = osc.core.http_PUT
http_POST = osc.core.http_POST

# TODO:
# before deleting a package, search for it's links and delete them
# as well. See build-service/src/api/app/models/package.rb -> find_linking_packages()

class UpdateCrawler(object):
    def __init__(self, from_prj, caching = True):
        self.from_prj = from_prj
        self.caching = caching
        self.apiurl = osc.conf.config['apiurl']
        self.project_preference_order = [
                'SUSE:SLE-12-SP1:Update',
                'SUSE:SLE-12-SP1:GA',
                'SUSE:SLE-12:Update',
                'SUSE:SLE-12:GA',
                'openSUSE:Factory',
                ]
        self.subprojects = [
                '%s:SLE-Pkgs-With-Overwrites' % self.from_prj,
                '%s:Factory-Copies' % self.from_prj,
                '%s:SLE12-Picks' % self.from_prj,
                ]
        self.projects = [self.from_prj] + self.subprojects

        self.project_mapping = {}
        for prj in self.project_preference_order:
            if prj.startswith('SUSE:'):
                self.project_mapping[prj] = self.from_prj + ':SLE12-Picks'
            else:
                self.project_mapping[prj] = self.from_prj + ':Factory-Copies'

        self.packages = dict()
        for project in self.projects + self.project_preference_order:
            self.packages[project] = self.get_source_packages(project)

    @memoize()
    def _cached_GET(self, url):
        return http_GET(url).read()

    def cached_GET(self, url):
        if self.caching:
            return self._cached_GET(url)
        return http_GET(url).read()

    def get_source_packages(self, project, expand=False):
        """Return the list of packages in a project."""
        query = {'expand': 1} if expand else {}
        root = ET.fromstring(
            self.cached_GET(makeurl(self.apiurl,
                             ['source', project],
                             query=query)))
        packages = [i.get('name') for i in root.findall('entry')]
        return packages

    def _get_source_package(self, project, package, revision):
        opts = { 'view': 'info' }
        if revision:
            opts['rev'] = revision
        return self.cached_GET(makeurl(self.apiurl,
                                ['source', project, package], opts))


    def get_latest_request(self, project, package):
        history = self.cached_GET(makeurl(self.apiurl,
                                   ['source', project, package, '_history']))
        root = ET.fromstring(history)
        requestid = None
        # latest commit's request - if latest commit is not a request, ignore the package
        for r in root.findall('revision'):
            requestid = r.find('requestid')
        if requestid is None:
            return None
        return requestid.text

    def get_request_infos(self, requestid):
        request = self.cached_GET(makeurl(self.apiurl, ['request', requestid]))
        root = ET.fromstring(request)
        action = root.find('.//action')
        source = action.find('source')
        target = action.find('target')
        project = source.get('project')
        package = source.get('package')
        rev = source.get('rev')
        return ( project, package, rev, target.get('package') )

    def remove_packages(self, project, packages):
        for package in packages:
            if not package in self.packages[project]:
                continue
            logging.info("deleting %s/%s", project, package)
            url = makeurl(self.apiurl, ['source', project, package])
            try:
                http_DELETE(url)
                self.packages[project].remove(package)
            except urllib2.HTTPError, err:
                if err.code == 404:
                    # not existant package is ok, we delete them all
                    pass
                else:
                    # If the package was there but could not be delete, raise the error
                    raise

    # copied from stagingapi - but the dependencies are too heavy
    def create_package_container(self, project, package):
        """
        Creates a package container without any fields in project/package
        :param project: project to create it
        :param package: package name
        """
        dst_meta = '<package name="{}"><title/><description/></package>'
        dst_meta = dst_meta.format(package)

        url = makeurl(self.apiurl, ['source', project, package, '_meta'])
        logging.debug("create %s/%s", project, package)
        http_PUT(url, data=dst_meta)

    def _link_content(self, sourceprj, sourcepkg, rev):
        root = ET.fromstring(self._get_source_package(sourceprj, sourcepkg, rev))
        srcmd5 = root.get('srcmd5')
        vrev = root.get('vrev')
        if vrev is None:
            vrev = ''
        else:
            vrev = " vrev='{}'".format(vrev)
        link = "<link project='{}' package='{}' rev='{}'{}/>"
        return link.format(sourceprj, sourcepkg, srcmd5, vrev)

    def upload_link(self, project, package, link_string):
        url = makeurl(self.apiurl, ['source', project, package, '_link'])
        http_PUT(url, data=link_string)

    def link_packages(self, packages, sourceprj, sourcepkg, sourcerev, targetprj, targetpkg):
        logging.info("update link %s/%s -> %s/%s@%s [%s]", targetprj, targetpkg, sourceprj, sourcepkg, sourcerev, ','.join(packages))
        self.remove_packages('%s:SLE12-Picks'%self.from_prj, packages)
        self.remove_packages('%s:Factory-Copies'%self.from_prj, packages)
        self.remove_packages('%s:SLE-Pkgs-With-Overwrites'%self.from_prj, packages)

        self.create_package_container(targetprj, targetpkg)
        link = self._link_content(sourceprj, sourcepkg, sourcerev)
        self.upload_link(targetprj, targetpkg, link)

        for package in [ p for p in packages if p != targetpkg ]:
            # FIXME: link packages from factory that override sle
            # ones to different projecg
            logging.debug("linking %s -> %s", package, targetpkg)
            link = "<link cicount='copy' package='{}' />".format(targetpkg)
            self.create_package_container(targetprj, package)
            self.upload_link(targetprj, package, link)

        self.remove_packages(self.from_prj, packages)

    def crawl(self, packages = None):
        """Main method of the class that run the crawler."""

        if packages:
            packages = [p for p in packages if p in self.packages[self.from_prj]]
        else:
            packages = self.get_source_packages(self.from_prj, expand=False)
            packages = [ p for p in packages if not p.startswith('_') ]
        requests = dict()

        left_packages = []

        for package in packages:
            requestid = self.get_latest_request(self.from_prj, package)
            if requestid is None:
                logging.warn("%s is not from request", package)
                left_packages.append(package)
                continue
            if requestid in requests:
                requests[requestid].append(package)
            else:
                requests[requestid] = [package]

        for request, packages in requests.items():
            sourceprj, sourcepkg, sourcerev, targetpkg = self.get_request_infos(request)
            if not sourceprj in self.project_mapping:
                logging.warn("unrecognized source project %s for [%s] in request %s", sourceprj, packages, request)
                left_packages = left_packages + packages
                continue
            logging.debug(" ".join((request, ','.join(packages), sourceprj, sourcepkg, sourcerev, targetpkg)))
            targetprj = self.project_mapping[sourceprj]
            self.link_packages(packages, sourceprj, sourcepkg, sourcerev, targetprj, targetpkg)

        return left_packages

    def check_source_in_project(self, project, package, verifymd5):
        if not package in self.packages[project]:
            return None

        try:
            his = self.cached_GET(makeurl(self.apiurl,
                                   ['source', project, package, '_history']))
        except urllib2.HTTPError:
            return None

        his = ET.fromstring(his)
        revs = list()
        for rev in his.findall('revision'):
            revs.append(rev.find('srcmd5').text)
        revs.reverse()
        for i in range(min(len(revs), 5)): # check last 5 commits
            srcmd5=revs.pop(0)
            root = self.cached_GET(makeurl(self.apiurl,
                                    ['source', project, package], { 'rev': srcmd5, 'view': 'info'}))
            root = ET.fromstring(root)
            if root.get('verifymd5') == verifymd5:
                return srcmd5
        return None

    # check if we can find the srcmd5 in any of our underlay
    # projects
    def try_to_find_left_packages(self, packages):
        for package in packages:
            root = ET.fromstring(self._get_source_package(self.from_prj, package, None))
            linked = root.find('linked')
            if not linked is None and linked.get('package') != package:
                logging.warn("link mismatch: %s <> %s, subpackage?", linked.get('package'), package)
                continue

            logging.debug("check where %s came", package)
            foundit = False
            for project in self.project_preference_order:
                srcmd5 = self.check_source_in_project(project, package, root.get('verifymd5'))
                if srcmd5:
                    logging.debug('%s -> %s', package, project)
                    self.link_packages([ package ], project, package, srcmd5, self.project_mapping[project], package)
                    foundit = True
                    break
            if not foundit:
                logging.debug('%s is a fork', package)

    def check_inner_link(self, project, package, link):
        if not link.get('cicount'):
            return
        if link.get('package') not in self.packages[project]:
            self.remove_packages(project, [package])

    def get_link(self, project, package):
        try:
            link = self.cached_GET(makeurl(self.apiurl,
                                    ['source', project, package, '_link']))
        except urllib2.HTTPError:
            return None
        return ET.fromstring(link)

    def check_link(self, project, package):
        link = self.get_link(project, package)
        if link is None:
            return
        rev = link.get('rev')
        # XXX: magic number?
        if rev and len(rev) > 5:
            return True
        if not link.get('project'):
            self.check_inner_link(project, package, link)
            return True
        opts = { 'view': 'info' }
        if rev:
            opts['rev'] = rev
        root = self.cached_GET(makeurl(self.apiurl,
                                ['source', link.get('project'), link.get('package')], opts ))
        root = ET.fromstring(root)
        self.link_packages([package], link.get('project'), link.get('package'), root.get('srcmd5'), project, package)

    def find_invalid_links(self, prj):
        for package in self.packages[prj]:
            self.check_link(prj, package)

    def check_dups(self):
        """ walk through projects in order of preference and delete
        duplicates in overlayed projects"""
        mypackages = dict()
        for project in self.projects:
            for package in self.packages[project]:
                if package in mypackages:
                    logging.debug("duplicate %s/%s, in %s", project, package, mypackages[package])
                    # TODO: detach only if actually a link to the deleted package
                    requestid = self.get_latest_request(self.from_prj, package)
                    if not requestid is None:
                        url = makeurl(self.apiurl, ['source', self.from_prj, package], { 'opackage': package, 'oproject': self.from_prj, 'cmd': 'copy', 'requestid': requestid, 'expand': '1'})
                        try:
                            http_POST(url)
                        except urllib2.HTTPError, err:
                            pass
                    self.remove_packages(project, [package])
                else:
                    mypackages[package] = project

    def freeze_candidates(self):
        url = makeurl(self.apiurl, ['source', 'openSUSE:Factory'], { 'view': 'info' } )
        root = ET.fromstring(self.cached_GET(url))
        flink = ET.Element('frozenlinks')
        fl = ET.SubElement(flink, 'frozenlink', {'project': 'openSUSE:Factory'})

        for package in root.findall('sourceinfo'):
            exists = False
            if package.get('package').startswith('_product'):
                continue
            for prj in self.projects:
                if package.get('package') in self.packages[prj]:
                    exists = True
            if exists:
                continue
            ET.SubElement(fl, 'package', { 'name': package.get('package'),
                                           'srcmd5': package.get('srcmd5'),
                                           'vrev': package.get('vrev') })

        url = makeurl(self.apiurl, ['source', '%s:Factory-Candidates-Check'%self.from_prj, '_project', '_frozenlinks'], {'meta': '1'})
        try:
            http_PUT(url, data=ET.tostring(flink))
        except urllib2.HTTPError, err:
            logging.error(err)

    def check_multiple_specs(self, project):
        logging.debug("check for multiple specs in %s", project)
        for package in self.packages[project]:
            url = makeurl(self.apiurl, ['source', project, package], { 'expand': '1' } )
            root = ET.fromstring(self.cached_GET(url))
            files = [ entry.get('name').replace('.spec', '') for entry in root.findall('entry') if entry.get('name').endswith('.spec') ]
            if len(files) == 1:
                continue
            mainpackage = None
            for subpackage in files[:]:
                link = self.get_link(project, subpackage)
                if link is not None:
                    if link.get('project') and link.get('project') != project:
                        mainpackage = subpackage
                        files.remove(subpackage)
                    if link.get('cicount'):
                        files.remove(subpackage)

            for subpackage in files:
                if subpackage in self.packages[project]:
                    self.remove_packages(project, [subpackage])

                link = "<link cicount='copy' package='{}' />".format(mainpackage)
                self.create_package_container(project, subpackage)
                self.upload_link(project, subpackage, link)

def main(args):
    # Configure OSC
    osc.conf.get_config(override_apiurl=args.apiurl)
    osc.conf.config['debug'] = args.debug

    uc = UpdateCrawler(args.from_prj, caching = args.no_cache )
    uc.check_dups()
    if not args.skip_sanity_checks:
        for prj in uc.subprojects:
            uc.check_multiple_specs(prj)
    lp = uc.crawl(args.package)
    uc.try_to_find_left_packages(lp)
    if not args.skip_sanity_checks:
        for prj in uc.projects:
            uc.find_invalid_links(prj)
    if args.no_update_candidates == False:
        uc.freeze_candidates()

if __name__ == '__main__':
    description = 'maintain sort openSUSE:42 packages into subprojects'
    parser = argparse.ArgumentParser(description=description)
    parser.add_argument('-A', '--apiurl', metavar='URL', help='API URL')
    parser.add_argument('-d', '--debug', action='store_true',
                        help='print info useful for debuging')
    parser.add_argument('-f', '--from', dest='from_prj', metavar='PROJECT',
                        help='project where to get the updates (default: %s)' % OPENSUSE,
                        default=OPENSUSE)
    parser.add_argument('--skip-sanity-checks', action='store_true',
                        help='don\'t do slow check for broken links (only for testing)')
    parser.add_argument('-n', '--dry', action='store_true',
                        help='dry run, no POST, PUT, DELETE')
    parser.add_argument('--no-cache', action='store_false', default=True,
                        help='do not cache GET requests')
    parser.add_argument('--no-update-candidates', action='store_true',
                        help='don\'t update Factory candidates project')
    parser.add_argument("package", nargs='*', help="package to check")

    args = parser.parse_args()

    # Set logging configuration
    logging.basicConfig(level=logging.DEBUG if args.debug
                        else logging.INFO)

    if args.dry:
        def dryrun(t, *args, **kwargs):
            return lambda *args, **kwargs: logging.debug("dryrun %s %s %s", t, args, str(kwargs)[:30])

        http_POST = dryrun('POST')
        http_PUT = dryrun('PUT')
        http_DELETE = dryrun('DELETE')

    sys.exit(main(args))

# vim:sw=4 et
