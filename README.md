# PST email search

Search a local PST for a literal substring, ignoring letter case. For example,
`1231231231` matches `S1231231231`. No Outlook installation is required.

## Run

Requires Python 3.12 or newer. In PowerShell, from the project directory:

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
.\.venv\Scripts\python.exe main.py
```

A virtual environment with the dependencies is already prepared in this workspace;
you can use the last command directly.

First edit `env.txt` beside `main.py`:

```ini
PST_PATH=C:\Users\Heorhii\Documents\archive.pst
SEARCH_TEXT=1231231231
```

Use an absolute path to an existing file. Backslashes are literal, so don't double
them. Values can be quoted; quotes preserve leading/trailing spaces in search text.
`#` and `=` inside a value remain literal. Empty search text is rejected.
Settings and results are located beside the script even when launched elsewhere.

The app searches all folders, including stored Sent and Deleted Items. It checks
subjects, plain text, HTML source and decoded HTML text, decompressed rich text,
transport headers (including encoded words), all textual MAPI properties,
recipient details, and attachment names/metadata. It also searches plain text
attachments and embedded email messages. Each parent email counts once, even if
several fields or embedded messages match. PDFs, Office documents, images, ZIPs,
encrypted contents and other binary attachments are not text-extracted or OCR'd.
Permanently deleted items outside the folder tree are not recovered.

After a successful scan with matches, the console displays:

```text
found n emails containing this text do you want to write results to file? [ y | N ]
```

Enter `y` (or `Y`) to save. Enter, `n`, or any other response declines.
With zero matches it only reports `No emails found containing this text.` and
does not prompt. Progress appears every 100 scanned emails.

Results are UTF-8 text containing the matching emails' readable fields, bodies,
headers, folder locations and the names of fields that matched. Output filenames
use local time: `14-30-05_2026-10-22.txt`. Windows forbids `:` in filenames, so the
requested time/date separator is represented by `_` and time separators by `-`.
If a filename already exists, a numeric suffix prevents overwriting it.
Only matching emails are retained in memory; a very broad query can use significant RAM.

The reader opens the PST for reading. A parse/read/decode failure stops the scan
with a nonzero exit code instead of presenting an incomplete count as a complete
result. A file-writing error may leave a partial output file; the app reports failure.

Supported format: Unicode PST (Outlook 2003 onward). Legacy Outlook 97–2002 ANSI
PST files are not supported by the selected reader. See the
[pypstreader documentation](https://pypi.org/project/pypstreader/).
Rich-text decoding uses [striprtf](https://pypi.org/project/striprtf/).

## Permission errors

If Windows reports `Permission denied` while reading the PST, close Outlook
completely and check Task Manager for a remaining `OUTLOOK.EXE` process before
retrying. Outlook can lock the file even when another program can open a read
handle. If needed, check Windows file permissions or copy the PST to a local
folder after closing Outlook, then update `PST_PATH`. The ordinary Outlook PST
password is not needed by this reader; it is not a data encryption key. See
[Microsoft's PST password documentation](https://learn.microsoft.com/en-us/openspecs/office_file_formats/ms-pst/48468b1e-cc81-4e2b-82a7-9bf61adc948e).

## Tests

```powershell
.\.venv\Scripts\python.exe -m unittest discover -s tests -v
```
