"""
Parse bank e-statement PDFs into transaction dicts.

Shared by the GUI (Settings -> Import Transactions) and the legacy
statement_manager.py CLI, so both read statements identically.

Tuned to the Westpac credit card e-statement layout. A transaction page has
a table header like:

    Date of  Description   Debits  Credits (-)
    Transaction

Rows start with a "DD Mon YY" date and end with an amount; credits carry a
trailing "-" and are stored as negative amounts. Any deeper-indented line
that follows a row (foreign fee breakdowns, merchant details on refunds) is
part of that row's description and is appended with " | ".

Pages without the table header (cover letters, points summaries, marketing)
are ignored, so whole statements can be passed in as-is.

Statements carry no card reference number, but the transactions table's
unique key (trans_date, reference_number, amount) needs one. Each row gets a
stable hash of its date, description, amount and occurrence count, so
re-importing a statement is detected as duplicates while two identical
purchases on one day both survive.

Transaction dicts have the same shape as the clipboard parsers produce:
    {"date": "YYYY-MM-DD", "description": str, "reference": str, "amount": float}
"""

import hashlib
import re
from datetime import datetime
from pathlib import Path
from typing import Callable, Iterable, List, Tuple

HEADER_RE = re.compile(r'Date of\s+Description\s+Debits\s+Credits', re.I)
ROW_RE = re.compile(r'^(?P<indent>\s*)(?P<date>\d{2} [A-Za-z]{3} \d{2})\s+(?P<rest>.*?)\s*$')
AMOUNT_RE = re.compile(
    r'^(?P<desc>.*?)\s+(?P<amount>\d{1,3}(?:,\d{3})*\.\d{2}|\d+\.\d{2})\s*(?P<credit>-)?$')
# Lines that end a transaction block even without a blank line in between.
STOP_RE = re.compile(r'^Page \d+ of \d+$|balance|total|continued', re.I)

Transaction = dict
Warn = Callable[[str], None]


def _noop(_msg: str) -> None:
    pass


def collect_pdf_files(paths: Iterable, warn: Warn = _noop) -> List[Path]:
    """Expand a mix of PDF files and folders into a sorted list of PDF paths.

    Folders are scanned one level deep for *.pdf in any letter case.
    """
    files: List[Path] = []
    for raw in paths:
        path = Path(raw).expanduser()
        if path.is_dir():
            found = sorted(f for f in path.iterdir()
                           if f.is_file() and f.suffix.lower() == '.pdf')
            if not found:
                warn(f"No PDF files in folder {path}")
            files.extend(found)
        elif path.is_file():
            files.append(path)
        else:
            warn(f"{path} is not a file or folder, skipped")
    return files


def parse_pdf(pdf_path, warn: Warn = _noop) -> Tuple[List[Transaction], List[int]]:
    """Parse a statement PDF. Returns (transactions, page_numbers_used).

    Raises ImportError if pdfplumber is not installed.
    """
    import pdfplumber  # imported lazily: only the PDF path needs it

    transactions: List[Transaction] = []
    pages_used: List[int] = []
    with pdfplumber.open(str(pdf_path)) as pdf:
        for page_no, page in enumerate(pdf.pages, 1):
            text = page.extract_text(layout=True) or ""
            if not HEADER_RE.search(text):
                continue
            found = parse_page_lines(text.splitlines(), warn)
            if found:
                pages_used.append(page_no)
                transactions.extend(found)

    assign_references(transactions)
    return transactions, pages_used


def parse_page_lines(lines: List[str], warn: Warn = _noop) -> List[Transaction]:
    """Parse the transaction table on one page of layout-preserved text."""
    # Skip everything up to and including the two-line table header.
    start = 0
    for i, line in enumerate(lines):
        if HEADER_RE.search(line):
            start = i + 1
            break
    if lines[start:start + 1] and lines[start].strip().lower() == 'transaction':
        start += 1

    transactions: List[Transaction] = []
    current = None
    current_indent = 0

    for line in lines[start:]:
        stripped = line.strip()
        if not stripped:
            current = None
            continue

        row = ROW_RE.match(line)
        if row:
            current = None
            amount_match = AMOUNT_RE.match(row.group('rest'))
            if not amount_match:
                warn(f"No amount on line, skipped: {stripped[:60]}")
                continue
            try:
                date_obj = datetime.strptime(row.group('date'), '%d %b %y')
            except ValueError:
                warn(f"Bad date on line, skipped: {stripped[:60]}")
                continue

            amount = float(amount_match.group('amount').replace(',', ''))
            if amount_match.group('credit'):
                amount = -amount

            current = {
                'date': date_obj.strftime('%Y-%m-%d'),
                'description': collapse_ws(amount_match.group('desc')),
                'reference': '',
                'amount': amount,
            }
            current_indent = len(row.group('indent'))
            transactions.append(current)
            continue

        # Continuation line: deeper indent than the date column, no date.
        indent = len(line) - len(line.lstrip())
        if (current is not None and indent > current_indent
                and not STOP_RE.search(stripped)):
            current['description'] += ' | ' + collapse_ws(stripped)
        else:
            current = None

    return transactions


def collapse_ws(text: str) -> str:
    return re.sub(r'\s+', ' ', text).strip()


def assign_references(transactions: List[Transaction]) -> None:
    """Give each row a stable content-derived reference (see module docstring)."""
    seen: dict = {}
    for txn in transactions:
        key = (txn['date'], txn['description'], f"{txn['amount']:.2f}")
        seen[key] = seen.get(key, 0) + 1
        digest = hashlib.sha1('|'.join(key + (str(seen[key]),)).encode('utf-8')).hexdigest()
        txn['reference'] = f"pdf-{digest[:12]}"


def format_pages(pages: List[int]) -> str:
    """Compress [1, 2, 3, 5] into '1-3, 5' for status messages."""
    if not pages:
        return ""
    runs = []
    start = prev = pages[0]
    for p in pages[1:]:
        if p == prev + 1:
            prev = p
            continue
        runs.append((start, prev))
        start = prev = p
    runs.append((start, prev))
    return ", ".join(f"{a}" if a == b else f"{a}-{b}" for a, b in runs)
