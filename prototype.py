#!/usr/bin/env python3

import base64
import multiprocessing as mp
import subprocess
from functools import partial
from multiprocessing import Lock
from urllib.parse import urlparse

import pychrome
from abp.filters import parse_filterlist
from abp.filters.parser import Filter
from langdetect import detect
from pprint import pprint
from tranco import Tranco


class WebpageResult:
    def __init__(self, rank=None, url=''):
        self.rank = rank
        self.url = url
        self.hostname = urlparse(self.url).hostname

        self.failed = False
        self.failed_reason = None
        self.failed_exception = None

        self.skipped = False
        self.skipped_reason = None

        self.stopped_waiting = False
        self.stopped_waiting_reason = None

        self.requests = []
        self.responses = []
        self.cookies = {}
        self.screenshots = {}

        self.language = None
        self.is_cmp_defined = False

    def set_failed(self, reason, exception=None):
        self.failed = True
        self.failed_reason = reason
        self.failed_exception = exception

    def set_skipped(self, reason):
        self.skipped = True
        self.skipped_reason = reason

    def set_stopped_waiting(self, reason):
        self.stopped_waiting = True
        self.stopped_waiting_reason = reason

    def add_request(self, request_url):
        self.requests.append({
            'url': request_url,
        })

    def add_response(self, requested_url, status, mime_type, headers):
        self.responses.append({
            'url': requested_url,
            'status': status,
            'mime_type': mime_type,
            'headers': headers,
        })

    def set_cookies(self, key, cookies):
        self.cookies[key] = cookies

    def add_screenshot(self, name, screenshot):
        self.screenshots[name] = screenshot

    def set_language(self, language):
        self.language = language

    def set_cmp_defined(self, is_cmp_defined):
        self.is_cmp_defined = is_cmp_defined


class WebpageCrawler:
    def __init__(self, tab, abp_filter, webpage):
        self.tab = tab
        self.abp_filter = abp_filter
        self.webpage = webpage

    def get_result(self):
        return self.webpage

    def crawl(self):
        # initialize `_is_loaded` variable to `False`
        # it will be set to `True` when the `loadEventFired` event occurs
        self._is_loaded = False

        # setup the tab
        self._setup_tab()
        self.requestId = None
        
        try:
            # deny notifications because they might pop-up and block detection
            self._deny_permission('notifications', self.webpage.hostname)

            # open url
            self.tab.Page.navigate(url=self.webpage.url, _timeout=15)

            # we wait for our load event to be fired (see `_event_load_event_fired`)
            waited = 0
            while not self._is_loaded and waited < 30:
                self.tab.wait(0.1)
                waited += 0.1

            if waited >= 30:
                self.webpage.set_stopped_waiting('load event')

            # wait  for JavaScript code to be run, after the page has been loaded
            self.tab.wait(5)

            # get root node of document, is needed to be sure that the DOM is loaded
            self.tab.DOM.getDocument()

            # detect cookie notices
            self.detect_cookie_notices()
        except pychrome.exceptions.TimeoutException as e:
            self.webpage.set_failed("timeout", e)
        except pychrome.exceptions.CallMethodException as e:
            self.webpage.set_failed("call_method", e)

        # stop and close the tab
        self.tab.stop()

        return self.webpage

    def _setup_tab(self):
        # set callbacks for request and response logging
        self.tab.Network.requestWillBeSent = self._event_request_will_be_sent
        self.tab.Network.responseReceived = self._event_response_received
        self.tab.Network.loadingFailed = self._event_loading_failed
        self.tab.Page.loadEventFired = self._event_load_event_fired
        
        # start our tab after callbacks have been registered
        self.tab.start()
        
        # enable network notifications for all request/response so our
        # callbacks actually receive some data
        self.tab.Network.enable()

        # enable page domain notifications so our load_event_fired
        # callback is called when the page is loaded
        self.tab.Page.enable()

        # enable DOM, Runtime and Overlay
        self.tab.DOM.enable()
        self.tab.Runtime.enable()
        self.tab.Overlay.enable()

    def _event_request_will_be_sent(self, request, requestId, **kwargs):
        """Will be called when a request is about to be sent.

        Those requests can still be blocked or intercepted and modified.
        This example script does not use any blocking or intercepting.

        Note: It does not say anything about the request being successful,
        there can still be connection issues.
        """
        url = request['url']
        self.webpage.add_request(request_url=url)

        # the request id of the first request is stored to be able to detect failures
        if self.requestId == None:
            self.requestId = requestId

    def _event_response_received(self, response, requestId, **kwargs):
        """Will be called when a response is received.

        This includes the originating request which resulted in the
        response being received.
        """
        url = response['url']
        mime_type = response['mimeType']
        status = response['status']
        headers = response['headers']
        self.webpage.add_response(requested_url=url, status=status, mime_type=mime_type, headers=headers)

        if requestId == self.requestId and (str(status).startswith('4') or str(status).startswith('5')):
            self.webpage.set_failed('status code `' + str(status) + '`')

    def _event_loading_failed(self, requestId, errorText, **kwargs):
        if requestId == self.requestId:
            self.webpage.set_failed('loading failed `' + errorText + '`')

    def _event_load_event_fired(self, timestamp, **kwargs):
        """Will be called when the page sends an load event.

        Note that this only means that all resources are loaded, the
        page may still process some JavaScript.
        """
        self._is_loaded = True

    def _deny_permission(self, permission, hostname):
        self._set_permission(permission, 'denied', 'https://' + hostname + '/*')
        self._set_permission(permission, 'denied', 'https://www.' + hostname + '/*')

    def _set_permission(self, permission, value, url):
        permission_descriptor = {'name': permission}
        self.tab.Browser.setPermission(origin=url, permission=permission_descriptor, setting=value)

    def detect_cookie_notices(self):
        global lock_m, lock_n, lock_l

        lang = self.detect_language()
        self.webpage.set_language(lang)
        if lang != 'en' and lang != 'de':
            self.webpage.set_skipped('unimplemented language `' + lang + '`')
            return

        # check whether the consent management platform is used
        # -> there should be a cookie notice
        is_cmp_defined = self.is_cmp_function_defined()
        self.webpage.set_cmp_defined(is_cmp_defined)

        # find cookie notice by using AdblockPlus rules
        cookie_notice_rule_node_ids = set(self.find_cookie_notices_by_rules())

        # find string `cookie` in nodes and store the closest parent block element
        cookie_node_ids = self.search_for_string('cookie')
        cookie_node_ids = self._filter_visible_nodes(cookie_node_ids)
        cookie_node_ids = set([self.find_parent_block_element(node_id) for node_id in cookie_node_ids])

        # find fixed parent nodes (i.e. having style `position: fixed`) with string `cookie`
        cookie_notice_fixed_node_ids = self.find_cookie_notices_by_fixed_parent(cookie_node_ids)
        cookie_notice_fixed_node_ids = self._filter_visible_nodes(cookie_notice_fixed_node_ids)

        # find full-width parent nodes with string `cookie`
        cookie_notice_full_width_node_ids = self.find_cookie_notices_by_full_width_parent(cookie_node_ids)
        cookie_notice_full_width_node_ids = self._filter_visible_nodes(cookie_notice_full_width_node_ids)

        # triple mutex
        with lock_l:
            lock_n.acquire()
            with lock_m:
                lock_n.release()
                self.tab.Page.bringToFront()
                self.take_screenshot('original')
                self.take_screenshots_of_visible_nodes(cookie_notice_rule_node_ids, 'rules')
                #self.take_screenshots_of_visible_nodes(cookie_node_ids, 'cookie-string')
                self.take_screenshots_of_visible_nodes(cookie_notice_fixed_node_ids, 'fixed-parent')
                self.take_screenshots_of_visible_nodes(cookie_notice_full_width_node_ids, 'full-width-parent')

        # save screenshots
        self.save_screenshots()

        # ocr with tesseract
        #subprocess.call(["tesseract", result.screenshot_filename, result.ocr_filename, "--oem", "1", "-l", "eng+deu"])

        # get cookies and delete them afterwards
        self.webpage.set_cookies('all', self._get_all_cookies())
        self._delete_all_cookies()

    def detect_language(self):
        result = self.tab.Runtime.evaluate(expression='document.body.innerText').get('result')
        return detect(result.get('value'))

    def search_for_string(self, search_string):
        """Searches the DOM for the given string and returns all found nodes."""

        # stop execution of scripts to ensure that results do not change during search
        self.tab.Emulation.setScriptExecutionDisabled(value=True)

        # search for the string in a text node
        # take the parent of the text node (the element that contains the text)
        # this is necessary if an element contains more than one text node!
        # see for explanation:
        # - https://stackoverflow.com/a/2994336
        # - https://stackoverflow.com/a/11744783
        search_object = self.tab.DOM.performSearch(
                query="//body//*/text()[contains(translate(., 'ABCDEFGHIJKLMNOPQRSTUVWXYZ', 'abcdefghijklmnopqrstuvwxyz'), '" + search_string + "')]/parent::*")

        node_ids = []
        if search_object.get('resultCount') != 0:
            search_results = self.tab.DOM.getSearchResults(
                    searchId=search_object.get('searchId'),
                    fromIndex=0,
                    toIndex=int(search_object.get('resultCount')))
            node_ids = search_results.get('nodeIds')

        # remove script and style nodes
        node_ids = [node_id for node_id in node_ids if not self._is_script_or_style_node(node_id)]

        # resume execution of scripts
        self.tab.Emulation.setScriptExecutionDisabled(value=False)

        # return nodes
        return node_ids

    def find_parent_block_element(self, node_id):
        """Returns the nearest parent block element or the element itself if it
        is a block element."""

        # if the node is a block element, just return it again
        if not self._is_inline_element(node_id):
            return node_id

        js_function = """
            function findClosestBlockElement(elem) {
                function isInlineElement(elem) {
                    const style = getComputedStyle(elem);
                    return style.display == 'inline';
                }

                if (!elem) elem = this;
                while(elem && elem !== document && isInlineElement(elem)) {
                    elem = elem.parentNode;
                }
                return elem;
            }"""

        # call the function `findClosestBlockElement` on the node
        remote_object_id = self._get_remote_object_id_for_node_id(node_id)
        result = self.tab.Runtime.callFunctionOn(functionDeclaration=js_function, objectId=remote_object_id, silent=True).get('result')
        return self._get_node_id_for_remote_object_id(result.get('objectId'))

    def find_cookie_notices_by_full_width_parent(self, cookie_node_ids):
        cookie_notice_full_width_node_ids = set()
        for node_id in cookie_node_ids:
            fwp_result = self.find_full_width_parent(node_id)
            if fwp_result.get('parent_node_exists'):
                cookie_notice_full_width_node_ids.add(fwp_result.get('parent_node'))
        return cookie_notice_full_width_node_ids

    def find_full_width_parent(self, node_id):
        js_function = """
            function findFullWidthParent(elem) {
                function getWidth(elem) {
                    const style = getComputedStyle(elem);
                    if (style.boxSizing == 'content-box') {
                        return parseInt(style.width) +
                            parseInt(style.paddingLeft) + parseInt(style.paddingRight) +
                            parseInt(style.borderLeftWidth) + parseInt(style.borderRightWidth) +
                            parseInt(style.marginLeft) + parseInt(style.marginRight);
                    } else {
                        return parseInt(style.width) + parseInt(style.marginLeft) + parseInt(style.marginRight);
                    }
                }

                function getHeight(elem) {
                    const style = getComputedStyle(elem);
                    if (style.boxSizing == 'content-box') {
                        return parseInt(style.height) +
                            parseInt(style.paddingTop) + parseInt(style.paddingBottom) +
                            parseInt(style.borderTopWidth) + parseInt(style.borderBottomWidth) +
                            parseInt(style.marginTop) + parseInt(style.marginBottom);
                    } else {
                        return parseInt(style.height) + parseInt(style.marginTop) + parseInt(style.marginBottom);
                    }
                }

                function getHorizontalSpacing(elem) {
                    const style = getComputedStyle(elem);
                    return parseInt(style.paddingLeft) + parseInt(style.paddingRight) +
                        parseInt(style.borderLeftWidth) + parseInt(style.borderRightWidth) +
                        parseInt(style.marginLeft) + parseInt(style.marginRight);
                }

                function getVerticalSpacing(elem) {
                    const style = getComputedStyle(elem);
                    return parseInt(style.paddingTop) + parseInt(style.paddingBottom) +
                        parseInt(style.borderTopWidth) + parseInt(style.borderBottomWidth) +
                        parseInt(style.marginTop) + parseInt(style.marginBottom);
                }

                function getWidthDiff(outerElem, innerElem) {
                    return getWidth(outerElem) - getWidth(innerElem);
                }

                function getHeightDiff(outerElem, innerElem) {
                    return getHeight(outerElem) - getHeight(innerElem);
                }

                function isParentWiderThanItsSpacing(outerElem, innerElem) {
                    return getWidthDiff(outerElem, innerElem) > getHorizontalSpacing(outerElem);
                }

                function isParentHigherThanItsSpacing(outerElem, innerElem) {
                    let allowedIncrease = Math.max(0.25*getHeight(innerElem), 20);
                    return getHeightDiff(outerElem, innerElem) > (getVerticalSpacing(outerElem) + allowedIncrease);
                }

                if (!elem) elem = this;
                while(elem && elem !== document) {
                    parent = elem.parentNode;
                    if (isParentHigherThanItsSpacing(parent, elem)) {
                        break;
                    }
                    elem = parent;
                }

                if (parseInt(getComputedStyle(document.body).width) <= getWidth(elem)) {
                    return elem;
                } else {
                    return false;
                }
            }"""

        remote_object_id = self._get_remote_object_id_for_node_id(node_id)
        result = self.tab.Runtime.callFunctionOn(functionDeclaration=js_function, objectId=remote_object_id, silent=True).get('result')

        # if a boolean is returned, we did not find a full-width small parent
        if result.get('type') == 'boolean':
            return {
                'parent_node_exists': result.get('value'),
                'parent_node': None,
            }
        # otherwise, we found one
        else:
            return {
                'parent_node_exists': True,
                'parent_node': self._get_node_id_for_remote_object_id(result.get('objectId')),
            }

    def find_cookie_notices_by_fixed_parent(self, cookie_node_ids):
        cookie_notice_fixed_node_ids = set()
        for node_id in cookie_node_ids:
            fp_result = self.find_fixed_parent(node_id)
            if fp_result.get('has_fixed_parent'):
                cookie_notice_fixed_node_ids.add(fp_result.get('fixed_parent'))
        return cookie_notice_fixed_node_ids

    def find_fixed_parent(self, node_id):
        js_function = """
            function findFixedParent(elem) {
                if (!elem) elem = this;
                while(elem && elem.parentNode !== document) {
                    let style = getComputedStyle(elem);
                    if (style.position === 'fixed') {
                        return elem;
                    }
                    elem = elem.parentNode;
                }
                return elem; // html node
            }"""

        remote_object_id = self._get_remote_object_id_for_node_id(node_id)
        result = self.tab.Runtime.callFunctionOn(functionDeclaration=js_function, objectId=remote_object_id, silent=True).get('result')
        result_node_id = self._get_node_id_for_remote_object_id(result.get('objectId'))

        # if the returned parent element is an html element,
        # no fixed parent element was found
        if self._is_html_node(result_node_id):
            html_node_id = result_node_id
            html_node = self.tab.DOM.describeNode(nodeId=html_node_id).get('node')

            # if the html element is the root html element, we have not found
            # a fixed parent
            if self._get_root_frame_id() == html_node.get('frameId'):
                return {
                    'has_fixed_parent': False,
                    'fixed_parent': None,
                }
            # otherwise, the frame is considered to be the fixed parent
            else:
                frame_node_id = self.tab.DOM.getFrameOwner(frameId=html_node.get('frameId')).get('nodeId')
                return {
                    'has_fixed_parent': True,
                    'fixed_parent': frame_node_id,
                }
        # otherwise, the returned parent element is a fixed element
        else:
            return {
                'has_fixed_parent': True,
                'fixed_parent': result_node_id,
            }

    def find_cookie_notices_by_rules(self):
        """Returns the node ids of the found cookie notices.

        The function uses the AdblockPlus ruleset of the browser plugin
        `I DON'T CARE ABOUT COOKIES`.
        See: https://www.i-dont-care-about-cookies.eu/
        """
        node_ids = []
        root_node_id = self.tab.DOM.getDocument().get('root').get('nodeId')
        for rule in self.abp_filter.get_applicable_rules(self.webpage.hostname):
            search_results = self.tab.DOM.querySelectorAll(nodeId=root_node_id, selector=rule.selector.get('value'))
            node_ids = node_ids + search_results.get('nodeIds')

        return node_ids

    def is_cmp_function_defined(self):
        """Checks whether the function `__cmp` is defined on the JavaScript
        `window` object."""

        result = self.tab.Runtime.evaluate(expression="typeof window.__cmp !== 'undefined'").get('result')
        return result.get('value')

    def find_clickables_in_node(self, node):
        pass
        #getEventListeners()
        # https://developers.google.com/web/tools/chrome-devtools/console/utilities?utm_campaign=2016q3&utm_medium=redirect&utm_source=dcc#geteventlistenersobject

    def is_node_visible(self, node_id):
        # Source: https://stackoverflow.com/a/41698614
        # adapted to also look at child nodes (especially important for fixed 
        # elements as they might not be "visible" themselves when they have no 
        # width or height)
        js_function = """
            function isVisible(elem) {
                if (!elem) elem = this;
                let visible = true;
                if (!(elem instanceof Element)) return false;
                const style = getComputedStyle(elem);

                // for these rules the childs cannot be visible, directly return
                if (style.display === 'none') return false;
                if (style.opacity < 0.1) return false;
                if (style.visibility !== 'visible') return false;

                // for these rules a child element might still be visible,
                // we need to also look at the childs, no direct return
                if (elem.offsetWidth + elem.offsetHeight + elem.getBoundingClientRect().height +
                    elem.getBoundingClientRect().width === 0) {
                    visible = false;
                }
                if (elem.offsetWidth === 0 || elem.offsetHeight === 0) {
                    visible = false;
                }
                const elemCenter = {
                    x: elem.getBoundingClientRect().left + elem.offsetWidth / 2,
                    y: elem.getBoundingClientRect().top + elem.offsetHeight / 2
                };
                if (elemCenter.x < 0) visible = false;
                if (elemCenter.x > (document.documentElement.clientWidth || window.innerWidth)) visible = false;
                if (elemCenter.y < 0) visible = false;
                if (elemCenter.y > (document.documentElement.clientHeight || window.innerHeight)) visible = false;

                if (visible === true) {
                    let pointContainer = document.elementFromPoint(elemCenter.x, elemCenter.y);
                    do {
                        if (pointContainer === elem) return elem;
                        if (!pointContainer) break;
                    } while (pointContainer = pointContainer.parentNode);
                }

                // check the child nodes
                if (!visible) {
                    let childrenCount = elem.childNodes.length;
                    for (var i = 0; i < childrenCount; i++) {
                        let isChildVisible = isVisible(elem.childNodes[i]);
                        if (isChildVisible) {
                            return isChildVisible;
                        }
                    }
                }

                return false;
            }"""

        # the function `isVisible` is calling itself recursively, 
        # therefore it needs to be defined beforehand
        self.tab.Runtime.evaluate(expression=js_function)

        # call the function `isVisible` on the node
        remote_object_id = self._get_remote_object_id_for_node_id(node_id)
        result = self.tab.Runtime.callFunctionOn(functionDeclaration=js_function, objectId=remote_object_id, silent=True).get('result')

        # if a boolean is returned, the object is not visible
        if result.get('type') == 'boolean':
            return {
                'is_visible': result.get('value'),
                'visible_node': None,
            }
        # otherwise, the object or one of its children is visible
        else:
            return {
                'is_visible': True,
                'visible_node': self._get_node_id_for_remote_object_id(result.get('objectId')),
            }

    def take_screenshots_of_visible_nodes(self, node_ids, name):
        # filter only visible nodes
        # and replace the original node_id with their visible children if the node itself is not visible
        node_ids = [visibility.get('visible_node') for visibility in (self.is_node_visible(node_id) for node_id in node_ids) if visibility.get('is_visible')]
        self.take_screenshots_of_nodes(node_ids, name)

    def take_screenshots_of_nodes(self, node_ids, name):
        # take a screenshot of the page with every node highlighted
        for index, node_id in enumerate(node_ids):
            self._highlight_node(node_id)
            self.take_screenshot(name + '-' + str(index))
            self._hide_highlight()

    def take_screenshot(self, name):
        # get the width and height of the viewport
        layout_metrics = self.tab.Page.getLayoutMetrics()
        viewport = layout_metrics.get('layoutViewport')
        width = viewport.get('clientWidth')
        height = viewport.get('clientHeight')
        x = viewport.get('pageX')
        y = viewport.get('pageY')
        screenshot_viewport = {'x': x, 'y': y, 'width': width, 'height': height, 'scale': 1}

        # take screenshot and store it
        self.webpage.add_screenshot(name, self.tab.Page.captureScreenshot(clip=screenshot_viewport)['data'])

    def _highlight_node(self, node_id):
        """Highlight the given node with an overlay."""

        color_content = {'r': 152, 'g': 196, 'b': 234, 'a': 0.5}
        color_padding = {'r': 184, 'g': 226, 'b': 183, 'a': 0.5}
        color_margin = {'r': 253, 'g': 201, 'b': 148, 'a': 0.5}
        highlightConfig = {'contentColor': color_content, 'paddingColor': color_padding, 'marginColor': color_margin}
        self.tab.Overlay.highlightNode(highlightConfig=highlightConfig, nodeId=node_id)

    def _hide_highlight(self):
        self.tab.Overlay.hideHighlight()

    def save_screenshots(self):
        for name, screenshot in self.webpage.screenshots.items():
            self._save_screenshot(name, screenshot)

    def _save_screenshot(self, name, screenshot):
        with open(self._get_filename_for_screenshot(name), "wb") as file:
            file.write(base64.b64decode(screenshot))

    def _get_filename_for_screenshot(self, name):
        return "screenshots/" + self.webpage.hostname + "-" + name + ".png"

    def _scroll_down(self, delta_y):
        self.tab.Input.emulateTouchFromMouseEvent(type="mouseWheel", x=1, y=1, button="none", deltaX=0, deltaY=-1*delta_y)
        self.tab.wait(0.1)

    def _get_all_cookies(self):
        return self.tab.Network.getAllCookies().get('cookies')

    def _delete_all_cookies(self):
        while(len(self._get_all_cookies()) != 0):
            for cookie in self._get_all_cookies():
                self.tab.Network.deleteCookies(name=cookie.get('name'), domain=cookie.get('domain'), path=cookie.get('path'))

    def _get_root_frame_id(self):
        return self.tab.Page.getFrameTree().get('frameTree').get('frame').get('id')

    def _get_node_id_for_remote_object_id(self, remote_object_id):
        return self.tab.DOM.requestNode(objectId=remote_object_id).get('nodeId')

    def _get_remote_object_id_for_node_id(self, node_id):
        return self.tab.DOM.resolveNode(nodeId=node_id).get('object').get('objectId')

    def _filter_visible_nodes(self, node_ids):
        return [node_id for node_id in node_ids if self.is_node_visible(node_id).get('is_visible')]

    def _get_node_name(self, node_id):
        return self.tab.DOM.describeNode(nodeId=node_id).get('node').get('nodeName').lower()

    def _is_script_or_style_node(self, node_id):
        node_name = self._get_node_name(node_id)
        return node_name == 'script' or node_name == 'style'

    def _is_html_node(self, node_id):
        return self._get_node_name(node_id) == 'html'

    def _is_inline_element(self, node_id):
        inline_elements = [
            'a', 'abbr', 'acronym', 'b', 'bdo', 'big', 'br', 'button', 'cite',
            'code', 'dfn', 'em', 'i', 'img', 'input', 'kbd', 'label', 'map',
            'object', 'output', 'q', 'samp', 'script', 'select', 'small',
            'span', 'strong', 'sub', 'sup', 'textarea', 'time', 'tt', 'var'
        ]
        return self._get_node_name(node_id) in inline_elements


class Browser:
    def __init__(self, abp_filter_filename, debugger_url='http://127.0.0.1:9222'):
        # create a browser instance which controls chromium
        self.browser = pychrome.Browser(url=debugger_url)

        # create helpers
        self.abp_filter = AdblockPlusFilter(abp_filter_filename)

    def crawl_page(self, webpage):
        global lock_m, lock_n, lock_l

        # triple mutex
        lock_n.acquire()
        with lock_m:
            lock_n.release()
            tab = self.browser.new_tab()

        page_crawler = WebpageCrawler(tab=tab, abp_filter=self.abp_filter, webpage=webpage)
        page_crawler.crawl()

        self.browser.close_tab(tab)
        return page_crawler.get_result()


class AdblockPlusFilter:
    def __init__(self, rules_filename):
        with open(rules_filename) as filterlist:
            # we only need filters with type css
            # other instances are Header, Metadata, etc.
            # other type is url-pattern which is used to block script files
            self._rules = [rule for rule in parse_filterlist(filterlist) if isinstance(rule, Filter) and rule.selector.get('type') == 'css']

    def get_applicable_rules(self, hostname):
        return [rule for rule in self._rules if self._is_rule_applicable(rule, hostname)]

    def _is_rule_applicable(self, rule, hostname):
        domain_options = [(key, value) for key, value in rule.options if key == 'domain']
        if len(domain_options) == 0:
            return True

        # there is only one domain option
        _, domains = domain_options[0]

        # filter exclusion rules as they should be ignored:
        # the cookie notices do exist, the ABP plugin is just not able 
        # to remove them correctly
        domains = [(opt_domain, opt_applicable) for opt_domain, opt_applicable in domains if opt_applicable == True]
        if len(domains) == 0:
            return True

        # the list of domains now only consists of domains for which the rule 
        # is applicable, we check for the domain and return False otherwise
        for opt_domain, _ in domains:
            if opt_domain in hostname:
                return True
        return False


if __name__ == '__main__':
    tranco = Tranco(cache=True, cache_dir='tranco')
    tranco_list = tranco.list(date='2020-03-01')
    tranco_top_100 = tranco_list.top(20)

    #urls = []
    #with open('resources/urls.txt') as f:
    #    urls = [line.strip() for line in f]

    #tranco_top_100 = ['cnn.com', 'twitch.tv', 'microsoft.com', 'reddit.com', 'zeit.de', 'godaddy.com', 'dropbox.com']

    # triple mutex:
    # https://stackoverflow.com/a/11673600
    # https://stackoverflow.com/a/28721419
    lock_m = Lock()
    lock_n = Lock()
    lock_l = Lock()

    # create multiprocessor pool: ten tabs are processed in parallel at most
    pool = mp.Pool(10)

    # create the browser and a helper function to crawl pages
    browser = Browser(abp_filter_filename='resources/cookie-notice-css-rules.txt')
    f_crawl_page = partial(Browser.crawl_page, browser)

    results = []
    def f_page_crawled(result):
        global results
        results.append(result)

        print('#' + str(result.rank) + ': ' + result.url)
        if result.stopped_waiting:
            print('-> stopped waiting for ' + result.stopped_waiting_reason)
        if result.failed:
            print('-> failed: ' + result.failed_reason)
        if result.skipped:
            print('-> skipped: ' + result.skipped_reason)

    # crawl the pages in parallel
    for rank, url in enumerate(tranco_top_100, start=1):
        webpage = WebpageResult(rank=rank, url='https://' + url)
        pool.apply_async(f_crawl_page, args=(webpage,), callback=f_page_crawled)
    pool.close()
    pool.join()

    # get results
    #results = [result.get() for result in results]
