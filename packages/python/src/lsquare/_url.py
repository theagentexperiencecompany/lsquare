"""An endpoint read as TypeScript's `new URL` reads it: the WHATWG URL Standard's basic URL parser,
without a base, as far as the client uses it (sdk-0.2-python-spec.md section 3).

Whether the endpoint parses at all, and for `http` and `https` its scheme, host, port and path,
which is what the client posts to. Standard library only, so one part is left out: a host that is
internationalized once percent-decoded (a non-ASCII character, or a label starting `xn--`) needs
UTS #46's mapping and validity tables, which Python doesn't ship, and is refused (`Unsupported`).
Every other host is read exactly: percent-decoded as UTF-8, ASCII-lowercased (which the standard
says UTS #46 comes to for such a host), checked for forbidden code points, and read as an IPv4
address in any of the forms the standard accepts (`127.1`, `0x7f.0.0.1`) when it ends in a number;
an IPv6 one in brackets is checked by the standard's parser, without a zone, and written in its
canonical form. The path is split on `/` and `\\`, its dot segments resolved (`%2e` included) and
percent-encoded as UTF-8 where the standard encodes it; what follows `?` or `#` is dropped.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from urllib.parse import unquote_to_bytes

#: The special schemes but `file`, with their default ports: their hosts are domains or addresses.
SPECIAL = {"ftp": 21, "http": 80, "https": 443, "ws": 80, "wss": 443}
#: What the standard trims from both ends of the input: C0 controls and the space, nothing else
#: (not a no-break space, which `str.strip()` would take too).
_C0_OR_SPACE = "".join(map(chr, range(0x21)))
#: What it removes everywhere in the input.
_TAB_OR_NEWLINE = str.maketrans("", "", "\t\n\r")
_SCHEME = re.compile(r"[A-Za-z][A-Za-z0-9+\-.]*:")
FORBIDDEN_HOST = frozenset("\x00\t\n\r #/:<>?@[\\]^|")
FORBIDDEN_DOMAIN = FORBIDDEN_HOST | frozenset(map(chr, range(0x20))) | frozenset("%\x7f")
#: Percent-encoded in a path besides the C0 controls and everything past `~`: the standard's path
#: percent-encode set (`?` and `#` end the path before it is encoded).
_PATH_ENCODED = frozenset(' "#<>?^`{}')
_DIGITS = {8: frozenset("01234567"), 10: frozenset("0123456789"), 16: frozenset("0123456789abcdefABCDEF")}
_HEX = _DIGITS[16]
#: Past this many significant digits an IPv4 part is larger than any address (2^32 - 1 has 10
#: decimal, 8 hexadecimal and 11 octal digits): read as too large, never converted.
_LONGEST_PART = 12


class NotURL(ValueError):
    """`new URL` throws for it."""


class Unsupported(ValueError):
    """A host this parser can't read as UTS #46 would: internationalized."""

    def __init__(self, scheme: str) -> None:
        super().__init__(scheme)
        self.scheme = scheme


@dataclass(frozen=True)
class URL:
    """What the client uses of a parsed URL. For a scheme that isn't `http` or `https`, only that it
    parsed: its host, port and path are left empty."""

    scheme: str
    #: As `url.hostname` writes it: a lowercase domain, a dotted IPv4 address or `[IPv6]`.
    host: str = ""
    #: None when it's the scheme's default, as `url.port` leaves it empty then.
    port: int | None = None
    #: As `url.pathname` writes it.
    pathname: str = ""


def parse(raw: str) -> URL:
    """Raises NotURL where `new URL(raw)` throws, Unsupported for an internationalized host."""
    # A JavaScript string's lone surrogates are U+FFFD to `new URL`; a pair is one character.
    text = raw.encode("utf-16-le", "surrogatepass").decode("utf-16-le", "replace")
    text = text.strip(_C0_OR_SPACE).translate(_TAB_OR_NEWLINE)
    match = _SCHEME.match(text)
    if match is None:
        raise NotURL("no scheme")
    scheme = match.group()[:-1].lower()
    rest = text[match.end() :]
    if scheme == "file":
        _file(rest, scheme)
        return URL(scheme)
    special = scheme in SPECIAL
    if special:
        # Any number of slashes and backslashes, none included, comes before the authority.
        rest = rest.lstrip("/\\")
    elif rest.startswith("//"):
        rest = rest[2:]
    else:
        # A path, opaque or not: it always parses.
        return URL(scheme)
    end = _end(rest, "/\\?#" if special else "/?#")
    authority, rest = rest[:end], rest[end:]
    at = authority.rfind("@")
    if at >= 0:
        # The credentials, up to the last @, which the client never sends.
        authority = authority[at + 1 :]
        if not authority:
            raise NotURL("no host after the credentials")
    host_text, port_text = _split_port(authority)
    if not host_text and (special or port_text is not None):
        raise NotURL("no host")
    if not special:
        _opaque_host(host_text)
        _port(port_text, None)
        return URL(scheme)
    unsupported: Unsupported | None = None
    try:
        host = _host(host_text, scheme)
    except Unsupported as error:
        # Said once the rest is known to parse: a port that doesn't makes it no URL at all.
        unsupported, host = error, ""
    port = _port(port_text, SPECIAL[scheme])
    if unsupported is not None:
        raise unsupported
    return URL(scheme, host, port, _path(rest))


def _end(text: str, stops: str) -> int:
    for index, char in enumerate(text):
        if char in stops:
            return index
    return len(text)


def _split_port(authority: str) -> tuple[str, str | None]:
    """The host, and the port after the first `:` outside brackets, if there is one."""
    inside = False
    for index, char in enumerate(authority):
        if char == "[":
            inside = True
        elif char == "]":
            inside = False
        elif char == ":" and not inside:
            return authority[:index], authority[index + 1 :]
    return authority, None


def _port(text: str | None, default: int | None) -> int | None:
    if not text:
        return None
    if any(char not in _DIGITS[10] for char in text):
        raise NotURL("a port that isn't a number")
    digits = text.lstrip("0") or "0"
    if len(digits) > 5 or int(digits) > 65535:
        raise NotURL("a port out of range")
    port = int(digits)
    return None if port == default else port


def _file(rest: str, scheme: str) -> None:
    """A `file` URL fails only on a host given after two slashes that can't be read."""
    if rest[:1] not in ("/", "\\") or rest[1:2] not in ("/", "\\"):
        return
    host = rest[2 : 2 + _end(rest[2:], "/\\?#")]
    windows_drive = len(host) == 2 and host[0].isascii() and host[0].isalpha() and host[1] in ":|"
    if host and not windows_drive:
        _host(host, scheme)


def _host(text: str, scheme: str) -> str:
    if text.startswith("["):
        if not text.endswith("]"):
            raise NotURL("an unclosed IPv6 address")
        return f"[{_ipv6_text(_ipv6(text[1:-1]))}]"
    try:
        domain = unquote_to_bytes(text).decode("utf-8")
    except UnicodeDecodeError:
        # U+FFFD to the standard, which UTS #46 disallows.
        raise NotURL("a host that isn't UTF-8") from None
    if "�" in domain:
        raise NotURL("U+FFFD in the host")
    if not domain.isascii() or any(label[:4].lower() == "xn--" for label in domain.split(".")):
        if _kept_forbidden(domain):
            raise NotURL("a forbidden code point in the host")
        raise Unsupported(scheme)
    ascii_domain = domain.lower()
    if not ascii_domain or any(char in FORBIDDEN_DOMAIN for char in ascii_domain):
        raise NotURL("a forbidden code point in the host")
    if _ends_in_number(ascii_domain):
        return _ipv4_text(_ipv4(ascii_domain))
    return ascii_domain


def _kept_forbidden(domain: str) -> bool:
    """A forbidden code point that UTS #46 keeps as it is, whatever the rest: any ASCII one, but a
    `<` or `>` before a combining mark, which NFC can compose with it (`<` and U+0338 make `≮`)."""
    for index, char in enumerate(domain):
        if char in FORBIDDEN_DOMAIN and not (char in "<>" and unicodedata.combining(domain[index + 1 : index + 2] or "a")):
            return True
    return False


def _opaque_host(text: str) -> None:
    if text.startswith("["):
        if not text.endswith("]"):
            raise NotURL("an unclosed IPv6 address")
        _ipv6(text[1:-1])
    elif any(char in FORBIDDEN_HOST for char in text):
        raise NotURL("a forbidden code point in the host")


def _ends_in_number(domain: str) -> bool:
    parts = domain.split(".")
    if parts[-1] == "":
        if len(parts) == 1:
            return False
        parts.pop()
    last = parts[-1]
    if last and all(char in _DIGITS[10] for char in last):
        return True
    return _ipv4_number(last) is not None


def _ipv4_number(text: str) -> int | None:
    """None when it isn't one."""
    if not text:
        return None
    radix = 10
    if len(text) >= 2 and text[:2] in ("0x", "0X"):
        text, radix = text[2:], 16
    elif len(text) >= 2 and text[0] == "0":
        text, radix = text[1:], 8
    if not text:
        return 0
    if any(char not in _DIGITS[radix] for char in text):
        return None
    significant = text.lstrip("0") or "0"
    if len(significant) > _LONGEST_PART:
        return 1 << 64
    return int(significant, radix)


def _ipv4(domain: str) -> int:
    parts = domain.split(".")
    if parts[-1] == "" and len(parts) > 1:
        parts.pop()
    if len(parts) > 4:
        raise NotURL("an IPv4 address of more than four parts")
    numbers: list[int] = []
    for part in parts:
        number = _ipv4_number(part)
        if number is None:
            raise NotURL("an IPv4 address with a part that isn't a number")
        numbers.append(number)
    if any(number > 255 for number in numbers[:-1]) or numbers[-1] >= 256 ** (5 - len(numbers)):
        raise NotURL("an IPv4 address out of range")
    address = numbers[-1]
    for index, number in enumerate(numbers[:-1]):
        address += number * 256 ** (3 - index)
    return address


def _ipv4_text(address: int) -> str:
    return ".".join(str((address >> shift) & 255) for shift in (24, 16, 8, 0))


def _ipv6(text: str) -> list[int]:
    """The standard's IPv6 parser: eight pieces, or NotURL."""
    address = [0] * 8
    piece = 0
    compress: int | None = None
    pointer = 0
    end = len(text)

    def at(index: int) -> str:
        return text[index] if index < end else ""

    if at(0) == ":":
        if at(1) != ":":
            raise NotURL("an IPv6 address starting with one colon")
        pointer = 2
        piece = 1
        compress = piece
    while pointer < end:
        if piece == 8:
            raise NotURL("an IPv6 address of more than eight pieces")
        if at(pointer) == ":":
            if compress is not None:
                raise NotURL("an IPv6 address compressed twice")
            pointer += 1
            piece += 1
            compress = piece
            continue
        value = length = 0
        while length < 4 and at(pointer) in _HEX and at(pointer):
            value = value * 16 + int(at(pointer), 16)
            pointer += 1
            length += 1
        if at(pointer) == ".":
            if length == 0:
                raise NotURL("an IPv4 part with no number")
            pointer -= length
            if piece > 6:
                raise NotURL("an IPv4 part too late in an IPv6 address")
            seen = 0
            while pointer < end:
                part: int | None = None
                if seen > 0:
                    if at(pointer) == "." and seen < 4:
                        pointer += 1
                    else:
                        raise NotURL("an IPv4 part of the wrong shape")
                if not at(pointer) or at(pointer) not in _DIGITS[10]:
                    raise NotURL("an IPv4 part of the wrong shape")
                while at(pointer) and at(pointer) in _DIGITS[10]:
                    number = int(at(pointer))
                    if part is None:
                        part = number
                    elif part == 0:
                        raise NotURL("an IPv4 part with a leading zero")
                    else:
                        part = part * 10 + number
                    if part > 255:
                        raise NotURL("an IPv4 part out of range")
                    pointer += 1
                assert part is not None
                address[piece] = address[piece] * 0x100 + part
                seen += 1
                if seen in (2, 4):
                    piece += 1
            if seen != 4:
                raise NotURL("an IPv4 part of the wrong shape")
            break
        if at(pointer) == ":":
            pointer += 1
            if pointer >= end:
                raise NotURL("an IPv6 address ending in one colon")
        elif pointer < end:
            raise NotURL("a character that isn't IPv6")
        address[piece] = value
        piece += 1
    if compress is not None:
        swaps = piece - compress
        piece = 7
        while piece != 0 and swaps > 0:
            other = compress + swaps - 1
            address[piece], address[other] = address[other], address[piece]
            piece -= 1
            swaps -= 1
    elif piece != 8:
        raise NotURL("an IPv6 address of fewer than eight pieces")
    return address


def _ipv6_text(address: list[int]) -> str:
    """The standard's serializer: the first longest run of two or more zero pieces compressed."""
    compress: int | None = None
    longest = 1
    index = 0
    while index < 8:
        if address[index] == 0:
            start = index
            while index < 8 and address[index] == 0:
                index += 1
            if index - start > longest:
                compress, longest = start, index - start
        else:
            index += 1
    out = ""
    skipping = False
    for index, piece in enumerate(address):
        if skipping and piece == 0:
            continue
        skipping = False
        if compress == index:
            out += "::" if index == 0 else ":"
            skipping = True
            continue
        out += f"{piece:x}"
        if index != 7:
            out += ":"
    return out


def _path(rest: str) -> str:
    """`url.pathname` of a special URL, from what follows its authority."""
    rest = rest[: _end(rest, "?#")]
    if rest[:1] in ("/", "\\"):
        rest = rest[1:]
    segments: list[str] = []
    parts = re.split(r"[/\\]", rest)
    for index, part in enumerate(parts):
        last = index == len(parts) - 1
        segment = "".join(_encoded(char) for char in part)
        lowered = segment.lower()
        if lowered in ("..", ".%2e", "%2e.", "%2e%2e"):
            if segments:
                segments.pop()
            if last:
                segments.append("")
        elif lowered in (".", "%2e"):
            if last:
                segments.append("")
        else:
            segments.append(segment)
    return "/" + "/".join(segments)


def _encoded(char: str) -> str:
    code = ord(char)
    if code < 0x20 or code > 0x7E or char in _PATH_ENCODED:
        return "".join(f"%{byte:02X}" for byte in char.encode("utf-8"))
    return char


__all__ = ["URL", "NotURL", "Unsupported", "parse"]
