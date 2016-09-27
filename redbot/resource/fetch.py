#!/usr/bin/env python

"""
The Resource Expert Droid Fetcher.

RedFetcher fetches a single URI and analyses that response for common
problems and other interesting characteristics. It only makes one request,
based upon the provided headers.
"""

import thor
import thor.http.error as httperr

from redbot import __version__
from redbot.speak import Note, levels, categories
from redbot.message import HttpRequest, HttpResponse
from redbot.message.status import StatusChecker
from redbot.message.cache import checkCaching
from redbot.resource.robot_fetch import RobotFetcher


UA_STRING = u"RED/%s (https://redbot.org/)" % __version__

class RedHttpClient(thor.http.HttpClient):
    "Thor HttpClient for RedFetcher"
    
    def __init__(self, loop=None):
        thor.http.HttpClient.__init__(self, loop)
        self.connect_timeout = 10
        self.read_timeout = 15
        self.retry_delay = 1
        self.careful = False


class RedFetcher(thor.events.EventEmitter):
    """
    Abstract class for a fetcher.

    Fetches the given URI (with the provided method, headers and body) and:
      - emits 'status' as it progresses
      - emits 'fetch_done' when the fetch is finished.

    If provided, 'name' indicates the type of the request, and is used to
    help set notes and status events appropriately.
    """
    check_name = u"undefined"
    response_phrase = u"undefined"
    client = RedHttpClient()
    robot_fetcher = RobotFetcher()

    def __init__(self):
        thor.events.EventEmitter.__init__(self)
        self.notes = []
        self.transfer_in = 0
        self.transfer_out = 0
        self.request = HttpRequest(self.ignore_note)
        self.response = HttpResponse(self.add_note)
        self.exchange = None
        self.follow_robots_txt = True # Should we pay attention to robots file?
        self.fetch_started = False
        self.fetch_done = False

    def __getstate__(self):
        state = thor.events.EventEmitter.__getstate__(self)
        del state['exchange']
        return state

    def __repr__(self):
        status = [self.__class__.__name__]
        if self.request.uri:
            status.append("%s" % self.request.uri)
        if self.fetch_started:
            status.append("fetch_started")
        if self.fetch_done:
            status.append("fetch_done")
        return u"<%s at %#x>" % (", ".join(status), id(self))

    def add_note(self, subject, note, **kw):
        "Set a note."
        kw['response'] = self.response_phrase
        self.notes.append(note(subject, kw))

    def ignore_note(self, subject, note, **kw):
        "Ignore a note (for requests)."
        return

    def preflight(self):
        """
        Check to see if we should bother running. Return True
        if so; False if not. Can be overridden.
        """
        return True

    def set_request(self, iri, method="GET", req_hdrs=None, req_body=None):
        """
        Set the resource's request.
        """
        enc = 'utf-8'
        self.request.method = method.encode(enc)
        self.response.is_head_response = (method == "HEAD")
        self.request.set_iri(iri)
        self.response.base_uri = self.request.uri
        if req_hdrs:
            self.request.set_headers([(n.encode(enc), v.encode(enc)) for (n, v) in req_hdrs])
        self.request.payload = req_body # FIXME: encoding
        self.request.complete = True  # cheating a bit

    def check(self):
        """
        Make an asynchronous HTTP request to uri, emitting 'status' as it's
        updated and 'fetch_done' when it's done. Reason is used to explain what the
        request is in the status callback.
        """
        self.fetch_started = True
        if not self.preflight() or self.request.uri == None:
            # generally a good sign that we're not going much further.
            self._fetch_done()
            return

        if self.follow_robots_txt:
            self.robot_fetcher.once("robot-%s" % self.request.uri, self.run_continue)
            self.robot_fetcher.check_robots(self.request.uri)
        else:
            self.run_continue(True)

    def run_continue(self, allowed):
        """
        Continue after getting the robots file.
        """
        if not allowed:
            self.response.http_error = RobotsTxtError()
            self._fetch_done()
            return

        if 'user-agent' not in [i[0].lower() for i in self.request.headers]:
            self.request.headers.append(
                (u"User-Agent", UA_STRING))
        self.exchange = self.client.exchange()
        self.exchange.once('response_start', self._response_start)
        self.exchange.on('response_body', self._response_body)
        self.exchange.once('response_done', self._response_done)
        self.exchange.on('error', self._response_error)
        self.emit("status", u"fetching %s (%s)" % (self.request.uri, self.check_name))
        req_hdrs = [(k.encode('ascii', 'replace'), v.encode('ascii', 'replace'))
                    for (k, v) in self.request.headers]
        self.exchange.request_start(
            self.request.method.encode('ascii'), self.request.uri.encode('ascii'), req_hdrs)
        self.request.start_time = thor.time()
        if self.request.payload != None:
            self.exchange.request_body(self.request.payload)
            self.transfer_out += len(self.request.payload)
        self.exchange.request_done([])

    def _response_start(self, status, phrase, res_headers):
        "Process the response start-line and headers."
        self.response.start_time = thor.time()
        self.response.set_top_line(self.exchange.res_version, status, phrase)
        self.response.set_headers(res_headers)
        StatusChecker(self.response, self.request)
        checkCaching(self.response, self.request)

    def _response_body(self, chunk):
        "Process a chunk of the response body."
        self.transfer_in += len(chunk)
        self.response.feed_body(chunk)

    def _response_done(self, trailers):
        "Finish analysing the response, handling any parse errors."
        self.emit("status", u"fetched %s (%s)" % (self.request.uri, self.check_name))
        self.response.transfer_length = self.exchange.input_transfer_length
        self.response.header_length = self.exchange.input_header_length
        self.response.body_done(True, trailers)
        self._fetch_done()

    def _response_error(self, error):
        "Handle an error encountered while fetching the response."
        self.emit("status", u"fetch error %s (%s) - %s" % (
            self.request.uri, self.check_name, error.desc))
        if error.client_recoverable:
            err_sample = error.detail[:40].decode('unicode_escape').encode('unicode_escape') or u""
            if isinstance(error, httperr.ExtraDataError):
                if self.response.status_code == u"304":
                    self.add_note('body', BODY_NOT_ALLOWED, sample=err_sample)
                else:
                    self.add_note('body', EXTRA_DATA, sample=err_sample)
            elif isinstance(error, httperr.ChunkError):
                self.add_note('header-transfer-encoding', BAD_CHUNK, chunk_sample=err_sample)
        else:
            self.response.http_error = error
            self._fetch_done()

    def _fetch_done(self):
        self.fetch_done = True
        self.emit("fetch_done")


class RobotsTxtError(httperr.HttpError):
    desc = "Forbidden by robots.txt"
    server_status = ("502", "Gateway Error")


class BODY_NOT_ALLOWED(Note):
    category = categories.CONNECTION
    level = levels.BAD
    summary = u"%(response)s is not allowed to have a body."
    text = u"""\
HTTP defines a few special situations where a response does not allow a body. This includes 101,
204 and 304 responses, as well as responses to the `HEAD` method.

%(response)s had data after the headers ended, despite it being disallowed. Clients receiving it
may treat the body as the next response in the connection, leading to interoperability and security
issues.

The extra data started with:

    %(sample)s
"""

class EXTRA_DATA(Note):
    category = categories.CONNECTION
    level = levels.BAD
    summary = u"%(response)s had extra data after it."
    text = u"""\
The server sent data after the message ended. This can be caused by an incorrect `Content-Length`
header, or by a programming error in the server itself.

The extra data started with:

    %(sample)s
"""

class BAD_CHUNK(Note):
    category = categories.CONNECTION
    level = levels.BAD
    summary = u"%(response)s had chunked encoding errors."
    text = u"""\
The response indicates it uses HTTP chunked encoding, but there was a problem decoding the
chunking.

A valid chunk looks something like this:

`[chunk-size in hex]\\r\\n[chunk-data]\\r\\n`

However, the chunk sent started like this:

`%(chunk_sample)s`

This is a serious problem, because HTTP uses chunking to delimit one response from the next one;
incorrect chunking can lead to interoperability and security problems.

This issue is often caused by sending an integer chunk size instead of one in hex, or by sending
`Transfer-Encoding: chunked` without actually chunking the response body."""




if __name__ == "__main__":
    import sys
    T = RedFetcher()
    T.set_request(sys.argv[1], req_hdrs=[(u'Accept-Encoding', u"gzip")])
    @thor.events.on(T)
    def fetch_done():
        print 'done'
        thor.stop()
    @thor.events.on(T)
    def status(msg):
        print msg
    T.check()
    thor.run()
