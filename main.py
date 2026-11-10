"""Search a PST using env.txt beside this script. Run with Python 3.12+."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
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


def format_person(name: str | None, email: str | None, smtp: str | None = None) -> str:
    address = smtp or email
    if name and address and name.casefold() != address.casefold():
        return f"{name} <{address}>"
    return address or name or ""


def message_fields(message):
    """Yield only reader-facing email content, without raw MAPI metadata."""
    if message.subject:
        yield "Subject", message.subject

    sender = format_person(message.sender_name, message.sender_email, message.sender_smtp)
    recipients = {"To": [], "Cc": [], "Bcc": []}
    for recipient in message.recipients():
        person = format_person(recipient.name, recipient.email, recipient.smtp)
        label = {0: "From", 1: "To", 2: "Cc", 3: "Bcc"}.get(int(recipient.type))
        if label == "From" and not sender:
            sender = person
        elif label in recipients and person and person not in recipients[label]:
            recipients[label].append(person)

    if sender:
        yield "From", sender
    for label, people in recipients.items():
        if people:
            yield label, "; ".join(people)

    for label, value in (("Sent", message.client_submit_time), ("Received", message.delivery_time)):
        if value is not None:
            yield label, str(value)

    body = message.body_text
    codepage = message.get(0x3FDE)  # PidTagInternetCodepage
    codec = {65001: "utf-8", 1200: "utf-16-le", 1201: "utf-16-be"}.get(
        codepage, f"cp{codepage}" if codepage else message.store.codepage
    )
    if not body and message.body_html:
        html = decode_bytes(message.body_html, codec)
        body = html_text(html)
    if not body and (rtf := message.body_rtf_decompressed()):
        # Latin-1 preserves bytes; striprtf interprets RTF's own codepage/escapes.
        body = rtf_to_text(rtf.decode("latin-1"))
    if body:
        yield "Text", body


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
            output.write(f"\n{'=' * 72}\nEmail {index}\n")
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
