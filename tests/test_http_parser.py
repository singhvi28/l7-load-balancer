"""Unit tests for the HTTP parser."""

import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from http_parser import (
    HttpRequest,
    try_parse_request,
    inject_forwarded_for,
    serialise_request,
    parse_response_status,
    get_content_length,
    CONTENT_LENGTH_ABSENT,
    get_response_content_length,
    is_chunked_response,
    try_consume_chunked_body,
)


class TestTryParseRequest:
    """Tests for try_parse_request()."""

    def test_incomplete_headers(self):
        buf = b"GET /foo HTTP/1.1\r\nHost: localhost"
        assert try_parse_request(buf) is None

    def test_simple_get(self):
        buf = (
            b"GET /hello HTTP/1.1\r\n"
            b"Host: localhost\r\n"
            b"\r\n"
        )
        req = try_parse_request(buf)
        assert req is not None
        assert req.method == "GET"
        assert req.path == "/hello"
        assert req.version == "HTTP/1.1"
        assert req.headers["Host"] == "localhost"
        assert req.body == b""

    def test_post_with_body(self):
        body = b'{"key": "value"}'
        buf = (
            b"POST /api HTTP/1.1\r\n"
            b"Host: localhost\r\n"
            b"Content-Length: " + str(len(body)).encode() + b"\r\n"
            b"\r\n" + body
        )
        req = try_parse_request(buf)
        assert req is not None
        assert req.method == "POST"
        assert req.body == body

    def test_incomplete_body(self):
        buf = (
            b"POST /api HTTP/1.1\r\n"
            b"Content-Length: 100\r\n"
            b"\r\n"
            b"partial"
        )
        assert try_parse_request(buf) is None

    def test_multiple_headers(self):
        buf = (
            b"GET / HTTP/1.1\r\n"
            b"Host: example.com\r\n"
            b"Accept: text/html\r\n"
            b"User-Agent: TestBot/1.0\r\n"
            b"\r\n"
        )
        req = try_parse_request(buf)
        assert req is not None
        assert req.headers["Accept"] == "text/html"
        assert req.headers["User-Agent"] == "TestBot/1.0"


class TestInjectForwardedFor:
    def test_adds_header(self):
        req = HttpRequest()
        req.headers = {"Host": "localhost"}
        inject_forwarded_for(req, "10.0.0.1")
        assert req.headers["X-Forwarded-For"] == "10.0.0.1"

    def test_appends_to_existing(self):
        req = HttpRequest()
        req.headers = {"X-Forwarded-For": "192.168.1.1"}
        inject_forwarded_for(req, "10.0.0.5")
        assert req.headers["X-Forwarded-For"] == "192.168.1.1, 10.0.0.5"


class TestSerialiseRequest:
    def test_roundtrip(self):
        original = (
            b"GET /path HTTP/1.1\r\n"
            b"Host: example.com\r\n"
            b"\r\n"
        )
        req = try_parse_request(original)
        assert req is not None
        rebuilt = serialise_request(req)
        # Verify it can be re-parsed
        req2 = try_parse_request(rebuilt)
        assert req2 is not None
        assert req2.method == "GET"
        assert req2.path == "/path"

    def test_with_body(self):
        body = b"hello world"
        original = (
            b"POST /upload HTTP/1.1\r\n"
            b"Content-Length: 11\r\n"
            b"\r\n" + body
        )
        req = try_parse_request(original)
        rebuilt = serialise_request(req)
        req2 = try_parse_request(rebuilt)
        assert req2.body == body


class TestParseResponseStatus:
    def test_basic_200(self):
        code, reason = parse_response_status(b"HTTP/1.1 200 OK\r\n")
        assert code == 200
        assert reason == "OK"

    def test_404(self):
        code, reason = parse_response_status(b"HTTP/1.1 404 Not Found\r\n")
        assert code == 404

    def test_incomplete(self):
        code, reason = parse_response_status(b"HTTP/1.1 20")
        assert code is None


class TestGetContentLength:
    def test_present(self):
        assert get_content_length({"Content-Length": "42"}) == 42

    def test_absent(self):
        assert get_content_length({"Host": "x"}) == 0

    def test_case_insensitive(self):
        assert get_content_length({"content-length": "10"}) == 10


class TestGetResponseContentLength:
    def test_present(self):
        buf = b"HTTP/1.1 200 OK\r\nContent-Length: 5\r\n\r\nhello"
        assert get_response_content_length(buf) == 5

    def test_zero_length(self):
        buf = b"HTTP/1.1 204 No Content\r\nContent-Length: 0\r\n\r\n"
        assert get_response_content_length(buf) == 0

    def test_absent(self):
        buf = b"HTTP/1.1 200 OK\r\nContent-Type: text/plain\r\n\r\nhello"
        assert get_response_content_length(buf) == CONTENT_LENGTH_ABSENT

    def test_no_headers_complete(self):
        buf = b"HTTP/1.1 200 OK\r\nContent"
        assert get_response_content_length(buf) == -1


class TestChunkedResponse:
    def test_is_chunked_true(self):
        buf = b"HTTP/1.1 200 OK\r\nTransfer-Encoding: chunked\r\n\r\n"
        assert is_chunked_response(buf) is True

    def test_is_chunked_with_content_length_still_chunked(self):
        buf = (
            b"HTTP/1.1 200 OK\r\n"
            b"Transfer-Encoding: chunked\r\n"
            b"Content-Length: 999\r\n"
            b"\r\n"
        )
        assert is_chunked_response(buf) is True

    def test_is_chunked_false(self):
        buf = b"HTTP/1.1 200 OK\r\nContent-Length: 5\r\n\r\nhello"
        assert is_chunked_response(buf) is False

    def test_is_chunked_incomplete_headers(self):
        assert is_chunked_response(b"HTTP/1.1 200 OK\r\nTransfer") is None

    def test_single_chunk(self):
        body = b"5\r\nhello\r\n0\r\n\r\n"
        assert try_consume_chunked_body(body) == len(body)

    def test_multiple_chunks(self):
        body = b"5\r\nhello\r\n6\r\n world\r\n0\r\n\r\n"
        assert try_consume_chunked_body(body) == len(body)

    def test_incomplete_mid_size_line(self):
        assert try_consume_chunked_body(b"5\r\nhello\r\n") is None
        assert try_consume_chunked_body(b"5") is None

    def test_incomplete_mid_data(self):
        assert try_consume_chunked_body(b"5\r\nhel") is None

    def test_with_trailers(self):
        body = b"4\r\nWiki\r\n0\r\nExpires: Wed, 21 Oct 2015\r\n\r\n"
        assert try_consume_chunked_body(body) == len(body)

    def test_chunk_ext(self):
        body = b"5;foo=bar\r\nhello\r\n0\r\n\r\n"
        assert try_consume_chunked_body(body) == len(body)

    def test_extra_bytes_after_complete(self):
        complete = b"5\r\nhello\r\n0\r\n\r\n"
        body = complete + b"GARBAGE"
        assert try_consume_chunked_body(body) == len(complete)

