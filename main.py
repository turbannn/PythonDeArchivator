"""Search a PST using env.txt beside this script. Run with Python 3.12+."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from email.header import decode_header
from html.parser import HTMLParser
from pathlib import Path
import sys

try:
    import pypstreader
    from pypstreader.eml import folder_paths
    from striprtf.striprtf import rtf_to_text
except ImportError as exc:
    raise SystemExit(
        f"Missing dependency: {exc.name}. Run: python -m pip install -r requirements.txt"
    ) from exc


PROJECT_DIR = Path(__file__).resolve().parent
TEXT_TYPES = {0x001E, 0x001F, 0x101E, 0x101F}
PROPERTY_NAMES = {
    0x001A: "Message class", 0x0037: "Subject", 0x007D: "Headers",
    0x0C1A: "Sender name", 0x0C1F: "Sender address",
    0x0E02: "Bcc", 0x0E03: "Cc", 0x0E04: "To",
    0x1000: "Body", 0x3001: "Display name", 0x3003: "Email address",
    0x3704: "Attachment filename", 0x3707: "Attachment long filename",
    0x39FE: "SMTP address", 0x5D01: "Sender SMTP address",
}


@dataclass
class Settings:
    pst_path: Path
    search_text: str


@dataclass
class Match:
    folder: str
    message_id: str
    fields: list[tuple[str, str]]
    matched_fields: list[str]


def read_settings(path: Path) -> Settings:
    """Parse literal values, preserving Windows backslashes, # and = characters."""
    values = {}
    for number, line in enumerate(path.read_text(encoding="utf-8-sig").splitlines(), 1):
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        key, separator, value = line.partition("=")
        key, value = key.strip(), value.strip()
        if not separator or key not in {"PST_PATH", "SEARCH_TEXT"}:
            raise ValueError(f"env.txt line {number}: expected PST_PATH=... or SEARCH_TEXT=...")
        if key in values:
            raise ValueError(f"env.txt: duplicate {key}")
        if value[:1] in {"'", '"'}:
            if len(value) < 2 or value[-1] != value[0]:
                raise ValueError(f"env.txt line {number}: unmatched quote")
            value = value[1:-1]
        values[key] = value
    for key in ("PST_PATH", "SEARCH_TEXT"):
        if not values.get(key, "").strip():
            raise ValueError(f"env.txt: {key} must not be empty")
    pst_path = Path(values["PST_PATH"])
    if not pst_path.is_absolute():
        raise ValueError("PST_PATH must be a direct, absolute path to a PST file")
    if not pst_path.is_file():
        raise ValueError(f"PST file does not exist: {pst_path}")
    if pst_path.suffix.lower() != ".pst":
        raise ValueError("PST_PATH must point to a .pst file")
    return Settings(pst_path, values["SEARCH_TEXT"])


def decode_bytes(data: bytes, encoding: str = "utf-8", fallback: str | None = None) -> str:
    """Decode imperfect message data without aborting the whole PST scan."""
    if data.startswith((b"\xff\xfe", b"\xfe\xff")):
        return data.decode("utf-16", errors="replace")
    if data.startswith(b"\xef\xbb\xbf"):
        return data.decode("utf-8-sig", errors="replace")

    codecs = list(dict.fromkeys(codec for codec in (encoding, fallback, "utf-8") if codec))
    known_codecs = []
    for codec in codecs:
        try:
            known_codecs.append(codec)
            return data.decode(codec)
        except LookupError:
            known_codecs.pop()
        except UnicodeDecodeError:
            continue

    # A malformed byte in a body or text attachment should become U+FFFD,
    # rather than discarding all matches found before that email.
    replacement_codec = fallback if fallback in known_codecs else (known_codecs[0] if known_codecs else "utf-8")
    return data.decode(replacement_codec, errors="replace")


def decoded_header(value: str) -> str:
    return "".join(
        decode_bytes(part, charset or "ascii") if isinstance(part, bytes) else part
        for part, charset in decode_header(value)
    )


class HTMLText(HTMLParser):
    """Keep inline text contiguous, but separate paragraphs, rows and breaks."""

    BLOCKS = {"br", "p", "div", "li", "tr", "td", "th", "hr", "h1", "h2", "h3"}

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts = []
        self.hidden = 0

    def handle_starttag(self, tag, attrs):
        if tag in {"script", "style"}:
            self.hidden += 1
        if tag in self.BLOCKS:
            self.parts.append("\n")

    def handle_endtag(self, tag):
        if tag in {"script", "style"}:
            self.hidden = max(0, self.hidden - 1)
        if tag in self.BLOCKS:
            self.parts.append("\n")

    def handle_data(self, data):
        if not self.hidden:
            self.parts.append(data)


def html_text(value: str) -> str:
    parser = HTMLText()
    parser.feed(value)
    parser.close()
    return "".join(parser.parts)


def property_fields(properties, prefix=""):
    """Read all textual MAPI properties, including custom and multi-value fields."""
    for prop_id, record in properties.records.items():
        if record.is_null or int(record.value_type) not in TEXT_TYPES:
            continue
        value = properties.read(record)
        label = prefix + PROPERTY_NAMES.get(prop_id, f"Property 0x{prop_id:04X}")
        for text in value if isinstance(value, (tuple, list)) else [value]:
            if text:
                yield label, text
                if "=?" in text:
                    yield label + " (decoded)", decoded_header(text)


def message_fields(message, prefix=""):
    yield prefix + "Subject", message.subject or ""
    yield from property_fields(message.properties, prefix)
    for label, value in (("Sent", message.client_submit_time), ("Received", message.delivery_time)):
        if value is not None:
            yield prefix + label, str(value)

    codepage = message.get(0x3FDE)  # PidTagInternetCodepage
    codec = {65001: "utf-8", 1200: "utf-16-le", 1201: "utf-16-be"}.get(
        codepage, f"cp{codepage}" if codepage else message.store.codepage
    )
    if message.body_html:
        html = decode_bytes(message.body_html, codec)
        yield prefix + "HTML source", html
        yield prefix + "HTML text", html_text(html)
    rtf = message.body_rtf_decompressed()
    if rtf:
        # Latin-1 preserves bytes; striprtf interprets RTF's own codepage/escapes.
        yield prefix + "Rich text", rtf_to_text(rtf.decode("latin-1"))

    for index, recipient in enumerate(message.recipients(), 1):
        for prop_id, value in recipient.properties.items():
            for text in value if isinstance(value, (tuple, list)) else [value]:
                if isinstance(text, str) and text:
                    yield prefix + f"Recipient {index} / 0x{prop_id:04X}", text

    for index, attachment in enumerate(message.attachments(), 1):
        label = prefix + f"Attachment {index} / "
        yield from property_fields(attachment.properties, label)
        embedded = attachment.embedded_message()
        if embedded is not None:
            yield from message_fields(embedded, label + "Embedded email / ")
        else:
            name = attachment.long_filename or attachment.filename or ""
            mime = (attachment.mime_tag or "").lower()
            if mime.startswith("text/") or Path(name).suffix.lower() in {".txt", ".csv", ".html", ".htm", ".xml", ".json"}:
                data = attachment.data()
                if data:
                    text = decode_bytes(data, fallback=message.store.codepage)
                    yield label + "Text content", text
                    if "html" in mime or Path(name).suffix.lower() in {".html", ".htm"}:
                        yield label + "HTML text", html_text(text)


def find_matches(settings: Settings) -> list[Match]:
    needle = settings.search_text.casefold()
    matches = []
    seen = set()
    with pypstreader.open(settings.pst_path) as store:
        for folder, folder_name in folder_paths(store.root_folder, strict=True):
            for message in folder.messages():
                message_id = str(message.node)
                if message_id in seen:
                    continue
                seen.add(message_id)
                fields = list(message_fields(message))
                matched = list(dict.fromkeys(label for label, value in fields if needle in value.casefold()))
                if matched:
                    matches.append(Match(folder_name, message_id, fields, matched))
                if len(seen) % 100 == 0:
                    print(f"Scanned {len(seen)} emails; {len(matches)} matches...", file=sys.stderr)
    return matches


def write_results(matches: list[Match], settings: Settings, directory: Path) -> Path:
    now = datetime.now().astimezone()
    stem = now.strftime("%H-%M-%S_%Y-%m-%d")
    target = directory / f"{stem}.txt"
    suffix = 0
    while True:
        try:
            output = target.open("x", encoding="utf-8", newline="\n")
            break
        except FileExistsError:
            suffix += 1
            target = directory / f"{stem}_{suffix}.txt"
    with output:
        output.write(f"PST: {settings.pst_path}\nSearch text: {settings.search_text}\n")
        output.write(f"Created: {now.isoformat()}\nMatching emails: {len(matches)}\n")
        for index, match in enumerate(matches, 1):
            output.write(f"\n{'=' * 72}\nEmail {index}\nFolder: {match.folder}\n")
            output.write(f"Message ID: {match.message_id}\nMatched fields: {', '.join(match.matched_fields)}\n")
            for label, value in match.fields:
                output.write(f"\n--- {label} ---\n{value}\n")
    return target


def main() -> int:
    operation = "reading settings"
    active_path = PROJECT_DIR / "env.txt"
    try:
        settings = read_settings(active_path)
        operation = "reading the PST"
        active_path = settings.pst_path
        matches = find_matches(settings)
        if not matches:
            print("No emails found containing this text.")
            return 0
        try:
            answer = input(
                f"found {len(matches)} emails containing this text do you want to write results to file? [ y | N ] "
            )
        except EOFError:
            answer = ""
        if answer.strip().lower() == "y":
            operation = "writing results"
            active_path = PROJECT_DIR
            target = write_results(matches, settings, PROJECT_DIR)
            print(f"Results written to: {target}")
        else:
            print("Results were not saved.")
        return 0
    except PermissionError as exc:
        path = exc.filename or active_path
        print(f"Permission denied while {operation}: {path}", file=sys.stderr)
        if operation == "reading the PST":
            print(
                "Windows blocked file access. This is not a PST password error. "
                "Close Outlook completely (check Task Manager for OUTLOOK.EXE), "
                "then retry. If it still fails, check your Windows read permission "
                "for this file, or copy it to a readable local folder after closing Outlook "
                "and update PST_PATH.",
                file=sys.stderr,
            )
        elif operation == "writing results":
            print("Check write permission for the project folder.", file=sys.stderr)
        else:
            print("Check read permission for env.txt and access to the configured PST path.", file=sys.stderr)
        print("The job did not complete; no complete result is available.", file=sys.stderr)
        return 1
    except (OSError, ValueError, LookupError, pypstreader.PstError) as exc:
        print(f"Error: {exc}\nThe job did not complete; no complete result is available.", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("\nCancelled.", file=sys.stderr)
        return 130


if __name__ == "__main__":
    sys.exit(main())
