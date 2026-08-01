"""
Custom HTTP/1.x parser that operates on raw byte buffers.

Responsibilities
────────────────
• Locate the \\r\\n\\r\\n header/body boundary.
• Parse the request line (method, path, version).
• Extract headers into a dict (case-insensitive lookup).
• Determine body length via Content-Length (request bodies: Content-Length only).
• Detect chunked Transfer-Encoding on *responses* and know when framing ends.
• Inject / append the ``X-Forwarded-For`` header with the client's IP.
• Re-serialise a modified request back to bytes for forwarding.
• Parse response status lines for access-log purposes.
"""

from __future__ import annotations

from typing import Optional, Tuple, Dict


HEADER_DELIM = b"\r\n\r\n"
LINE_DELIM = b"\r\n"

# get_response_content_length sentinels:
#   -1  headers not yet complete
#   -2  headers complete, Content-Length absent (close-delimited body)
#  >=0  explicit Content-Length (including 0)
CONTENT_LENGTH_ABSENT = -2


# ─── Request Parsing ─────────────────────────────────────────────────────────

class HttpRequest:
    """Parsed representation of an HTTP request."""

    __slots__ = (
        "method", "path", "version",
        "headers", "body",
        "raw_header_bytes", "header_length",
    )

    def __init__(self) -> None:
        self.method: str = ""
        self.path: str = ""
        self.version: str = ""
        self.headers: Dict[str, str] = {}
        self.body: bytes = b""
        self.raw_header_bytes: bytes = b""
        self.header_length: int = 0


def try_parse_request(buf: bytes) -> Optional[HttpRequest]:
    """Attempt to parse a complete HTTP request from *buf*.

    Returns ``None`` when the buffer does not yet contain a full request
    (headers incomplete or body not fully received).
    """
    hdr_end = buf.find(HEADER_DELIM)
    if hdr_end == -1:
        return None  # headers not fully received yet

    header_section = buf[:hdr_end]
    header_length = hdr_end + len(HEADER_DELIM)

    lines = header_section.split(LINE_DELIM)
    if not lines:
        return None

    # ── Request line ──────────────────────────────────────────────────────
    request_line = lines[0].decode("latin-1")
    parts = request_line.split(" ", 2)
    if len(parts) != 3:
        return None

    req = HttpRequest()
    req.method, req.path, req.version = parts

    # ── Headers ───────────────────────────────────────────────────────────
    for line in lines[1:]:
        decoded = line.decode("latin-1")
        colon = decoded.find(":")
        if colon == -1:
            continue
        name = decoded[:colon].strip()
        value = decoded[colon + 1:].strip()
        # Store with lower-cased key for easy lookup, but keep original casing
        req.headers[name] = value

    # ── Body (Content-Length only) ────────────────────────────────────────
    content_length = get_content_length(req.headers)
    total_needed = header_length + content_length
    if len(buf) < total_needed:
        return None  # body not fully received

    req.body = buf[header_length:total_needed]
    req.raw_header_bytes = buf[:header_length]
    req.header_length = header_length
    return req


def get_content_length(headers: Dict[str, str]) -> int:
    """Return Content-Length as int, defaulting to 0."""
    for key, value in headers.items():
        if key.lower() == "content-length":
            try:
                return int(value)
            except ValueError:
                return 0
    return 0


# ─── Header Injection ────────────────────────────────────────────────────────

def inject_forwarded_for(req: HttpRequest, client_ip: str) -> None:
    """Add or append to the ``X-Forwarded-For`` header."""
    existing = None
    for key in req.headers:
        if key.lower() == "x-forwarded-for":
            existing = key
            break

    if existing is not None:
        req.headers[existing] += f", {client_ip}"
    else:
        req.headers["X-Forwarded-For"] = client_ip


# ─── Serialisation ────────────────────────────────────────────────────────────

def serialise_request(req: HttpRequest) -> bytes:
    """Re-build the raw bytes of an HTTP request from its parsed form."""
    lines = [f"{req.method} {req.path} {req.version}"]
    for name, value in req.headers.items():
        lines.append(f"{name}: {value}")
    header_block = "\r\n".join(lines) + "\r\n\r\n"
    return header_block.encode("latin-1") + req.body


# ─── Response Helpers ─────────────────────────────────────────────────────────

def parse_response_status(buf: bytes) -> Tuple[Optional[int], Optional[str]]:
    """Extract the status code and reason from a response buffer.

    Returns ``(None, None)`` if the status line is not yet complete.
    """
    end = buf.find(LINE_DELIM)
    if end == -1:
        return None, None
    status_line = buf[:end].decode("latin-1", errors="replace")
    parts = status_line.split(" ", 2)
    if len(parts) < 2:
        return None, None
    try:
        code = int(parts[1])
    except ValueError:
        return None, None
    reason = parts[2] if len(parts) == 3 else ""
    return code, reason


def get_response_content_length(buf: bytes) -> int:
    """Peek at the Content-Length in a response buffer (best-effort).

    Returns ``-1`` if headers are incomplete, ``CONTENT_LENGTH_ABSENT`` (-2)
    if headers are complete but the header is missing, or ``>= 0`` for an
    explicit length (including a genuine ``Content-Length: 0``).
    """
    hdr_end = buf.find(HEADER_DELIM)
    if hdr_end == -1:
        return -1  # headers not complete
    header_section = buf[:hdr_end].decode("latin-1", errors="replace")
    for line in header_section.split("\r\n")[1:]:
        colon = line.find(":")
        if colon == -1:
            continue
        name = line[:colon].strip()
        if name.lower() == "content-length":
            try:
                return int(line[colon + 1:].strip())
            except ValueError:
                return 0
    return CONTENT_LENGTH_ABSENT


# ─── Chunked Transfer-Encoding (responses) ───────────────────────────────────
# Request bodies remain Content-Length-only; streaming would need a state machine.


def is_chunked_response(buf: bytes) -> Optional[bool]:
    """Return whether the response uses ``Transfer-Encoding: chunked``.

    ``None`` if response headers are not yet complete.
    """
    hdr_end = buf.find(HEADER_DELIM)
    if hdr_end == -1:
        return None
    header_section = buf[:hdr_end].decode("latin-1", errors="replace")
    for line in header_section.split("\r\n")[1:]:
        colon = line.find(":")
        if colon == -1:
            continue
        name = line[:colon].strip().lower()
        if name == "transfer-encoding":
            value = line[colon + 1:].strip().lower()
            # TE can be a list; "chunked" must be present (usually last)
            return "chunked" in [p.strip() for p in value.split(",")]
    return False


def try_consume_chunked_body(body: bytes) -> Optional[int]:
    """Scan a chunked body buffer; return total framed length when complete.

    Pure function over the full body so far (fits the current full-buffer
    model). Returns ``None`` if more bytes are needed. On success, the return
    value is the number of body bytes that constitute a complete chunked
    message (including size lines, data, trailers, and the final CRLF).
    """
    pos = 0
    length = len(body)

    while True:
        # Need a complete size line
        line_end = body.find(LINE_DELIM, pos)
        if line_end == -1:
            return None

        size_line = body[pos:line_end].decode("latin-1", errors="replace")
        # Strip chunk-ext (`;...`)
        size_token = size_line.split(";", 1)[0].strip()
        try:
            chunk_size = int(size_token, 16)
        except ValueError:
            return None  # malformed — treat as incomplete / caller may 502

        pos = line_end + len(LINE_DELIM)

        if chunk_size == 0:
            # Last-chunk: trailers then final CRLF
            # Trailers are zero or more header lines ending with a blank line
            trailer_end = body.find(HEADER_DELIM, pos)
            if trailer_end == -1:
                # Could be just a final CRLF with no trailer fields —
                # HEADER_DELIM is \r\n\r\n; a bare final CRLF after last-chunk
                # means body[pos:] should start with \r\n for empty trailers,
                # i.e. we need LINE_DELIM at pos (empty trailer-part = CRLF)
                if body[pos:pos + len(LINE_DELIM)] == LINE_DELIM:
                    return pos + len(LINE_DELIM)
                return None
            return trailer_end + len(HEADER_DELIM)

        # Need chunk-data (chunk_size bytes) + trailing CRLF
        need = chunk_size + len(LINE_DELIM)
        if pos + need > length:
            return None
        # Verify CRLF after data (best-effort; still advance)
        pos += chunk_size
        if body[pos:pos + len(LINE_DELIM)] != LINE_DELIM:
            return None
        pos += len(LINE_DELIM)
