import re
import sys
import sqlite3
import hashlib
import argparse
from decimal import Decimal
from datetime import datetime
from pathlib import Path
import pyperclip
from openpyxl import Workbook
from openpyxl.styles import Font, PatternFill, Alignment
from openpyxl.worksheet.datavalidation import DataValidation


class StatementManager:
    def __init__(self, db_path, config_path=None):
        self.db_path = db_path
        self.conn = None
        self.categories = [
            "software",
            "professional membership",
            "technical library",
            "magazines and journals",
            "training",
            "not work related",
            "other"
        ]
        self.api_key = None
        self.client = None

        if config_path:
            self._load_config(config_path)

        self._init_database()

    def _load_config(self, config_path):
        """Load configuration from JSON file"""
        try:
            import json
            with open(config_path, 'r') as f:
                config = json.load(f)
                self.api_key = config.get('anthropic_api_key')
                if self.api_key:
                    import anthropic
                    self.client = anthropic.Anthropic(api_key=self.api_key)
                    print("✓ Claude API configured")
        except Exception as e:
            print(f"Warning: Could not load config file: {e}")

    def _init_database(self):
        """Initialize database and create table if it doesn't exist"""
        self.conn = sqlite3.connect(self.db_path)
        cursor = self.conn.cursor()
        cursor.execute("""
                       CREATE TABLE IF NOT EXISTS transactions
                       (
                           id               INTEGER PRIMARY KEY AUTOINCREMENT,
                           trans_date       TEXT NOT NULL,
                           description      TEXT NOT NULL,
                           reference_number TEXT NOT NULL,
                           amount           REAL NOT NULL,
                           category         TEXT,
                           ai_classified    INTEGER   DEFAULT 0,
                           created_at       TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                           UNIQUE (trans_date, reference_number, amount)
                       )
                       """)
        self.conn.commit()

    def parse_clipboard(self):
        """Parse clipboard data and extract transactions - cross-platform"""
        try:
            data = pyperclip.paste()
        except Exception as e:
            print(f"Error accessing clipboard: {e}")
            print("Make sure you have clipboard access enabled for this application.")
            return []

        if not data:
            print("Clipboard is empty")
            return []

        transactions = []
        lines = data.splitlines()

        # Pattern to match date at start of line
        date_pattern = re.compile(r'^(\d{6})\s+(.+)')
        # Pattern to match reference number (long digit sequence)
        ref_pattern = re.compile(r'\d{15,}')

        i = 0
        while i < len(lines):
            line = lines[i].strip()
            if not line:
                i += 1
                continue

            match = date_pattern.match(line)
            if match:
                date_str = match.group(1)
                rest = match.group(2).strip()

                # Check if amount is on this line or next
                amount_match = re.search(r'([\d.]+)$', rest)

                if amount_match:
                    # Amount is on same line
                    amount = Decimal(amount_match.group(1))
                    desc_and_ref = rest[:amount_match.start()].strip()

                    # Extract reference number from next line if available
                    ref_num = ""
                    if i + 1 < len(lines):
                        next_line = lines[i + 1].strip()
                        ref_match = ref_pattern.search(next_line)
                        if ref_match:
                            ref_num = ref_match.group(0)
                            i += 1  # Skip the reference line

                    # Format date as YYYY-MM-DD
                    formatted_date = f"20{date_str[4:6]}-{date_str[2:4]}-{date_str[0:2]}"

                    transactions.append({
                        'date': formatted_date,
                        'description': desc_and_ref,
                        'reference': ref_num,
                        'amount': float(amount)
                    })

            i += 1

        return transactions

    # ── PDF statements ──────────────────────────────────────────────────────
    # A transaction page has a table header like:
    #   Date of  Description   Debits  Credits (-)
    #   Transaction
    # Rows start with a "DD Mon YY" date and end with an amount; credits carry a
    # trailing "-". Any deeper-indented line that follows a row (foreign fee
    # breakdowns, merchant details on refunds) is part of that row's description.
    _PDF_HEADER_RE = re.compile(r'Date of\s+Description\s+Debits\s+Credits', re.I)
    _PDF_ROW_RE = re.compile(r'^(?P<indent>\s*)(?P<date>\d{2} [A-Za-z]{3} \d{2})\s+(?P<rest>.*?)\s*$')
    _PDF_AMOUNT_RE = re.compile(
        r'^(?P<desc>.*?)\s+(?P<amount>\d{1,3}(?:,\d{3})*\.\d{2}|\d+\.\d{2})\s*(?P<credit>-)?$')
    # Lines that end a transaction block even without a blank line in between.
    _PDF_STOP_RE = re.compile(r'^Page \d+ of \d+$|balance|total|continued', re.I)

    @staticmethod
    def collect_pdf_files(paths):
        """Expand a mix of PDF files and folders into a sorted list of PDF paths."""
        files = []
        for raw in paths:
            path = Path(raw).expanduser()
            if path.is_dir():
                found = sorted(f for f in path.iterdir()
                               if f.is_file() and f.suffix.lower() == '.pdf')
                if not found:
                    print(f"Warning: no PDF files in folder {path}")
                files.extend(found)
            elif path.is_file():
                files.append(path)
            else:
                print(f"Warning: {path} is not a file or folder, skipping")
        return files

    def parse_pdf(self, pdf_path):
        """Parse a bank statement PDF.

        Returns (transactions, pages_used). Pages without the transaction table
        header (cover letters, points summaries, marketing) are ignored.
        """
        try:
            import pdfplumber
        except ImportError:
            print("Error: pdfplumber is not installed. Run: pip install pdfplumber")
            return [], []

        transactions = []
        pages_used = []
        with pdfplumber.open(str(pdf_path)) as pdf:
            for page_no, page in enumerate(pdf.pages, 1):
                text = page.extract_text(layout=True) or ""
                if not self._PDF_HEADER_RE.search(text):
                    continue
                found = self._parse_pdf_page(text.splitlines())
                if found:
                    pages_used.append(page_no)
                    transactions.extend(found)

        self._assign_pdf_references(transactions)
        return transactions, pages_used

    def _parse_pdf_page(self, lines):
        """Parse the transaction table on one page of layout-preserved text."""
        # Skip everything up to and including the two-line table header.
        start = 0
        for i, line in enumerate(lines):
            if self._PDF_HEADER_RE.search(line):
                start = i + 1
                break
        if lines[start:start + 1] and lines[start].strip().lower() == 'transaction':
            start += 1

        transactions = []
        current = None
        current_indent = 0

        for line in lines[start:]:
            stripped = line.strip()
            if not stripped:
                current = None
                continue

            row = self._PDF_ROW_RE.match(line)
            if row:
                current = None
                amount_match = self._PDF_AMOUNT_RE.match(row.group('rest'))
                if not amount_match:
                    print(f"  Warning: no amount on line, skipped: {stripped[:60]}")
                    continue
                try:
                    date_obj = datetime.strptime(row.group('date'), '%d %b %y')
                except ValueError:
                    print(f"  Warning: bad date on line, skipped: {stripped[:60]}")
                    continue

                amount = float(amount_match.group('amount').replace(',', ''))
                if amount_match.group('credit'):
                    amount = -amount

                current = {
                    'date': date_obj.strftime('%Y-%m-%d'),
                    'description': self._collapse_ws(amount_match.group('desc')),
                    'reference': '',
                    'amount': amount,
                }
                current_indent = len(row.group('indent'))
                transactions.append(current)
                continue

            # Continuation line: deeper indent than the date column, no date.
            indent = len(line) - len(line.lstrip())
            if (current is not None and indent > current_indent
                    and not self._PDF_STOP_RE.search(stripped)):
                current['description'] += ' | ' + self._collapse_ws(stripped)
            else:
                current = None

        return transactions

    @staticmethod
    def _collapse_ws(text):
        return re.sub(r'\s+', ' ', text).strip()

    @staticmethod
    def _assign_pdf_references(transactions):
        """Statements carry no card reference number, but the table's unique
        key needs one. Derive a stable hash from the row content plus its
        occurrence count, so re-importing the same statement is detected as a
        duplicate while two identical purchases on one day both survive."""
        seen = {}
        for txn in transactions:
            key = (txn['date'], txn['description'], f"{txn['amount']:.2f}")
            seen[key] = seen.get(key, 0) + 1
            digest = hashlib.sha1('|'.join(key + (str(seen[key]),)).encode('utf-8')).hexdigest()
            txn['reference'] = f"pdf-{digest[:12]}"

    def classify_transaction(self, description: str, amount: float):
        """Use Claude API to classify a transaction"""
        if not self.client:
            return None

        try:
            prompt = f"""Given this credit card transaction, classify it into one of these categories for tax deduction purposes:

Categories:
- "software": Software purchases, SaaS subscriptions, development tools
- "professional membership": Professional organization memberships, certifications
- "technical library": Books, technical publications, O'Reilly, Manning, etc.
- "magazines and journals": Technical magazines, IEEE, ACM subscriptions
- "training": Online courses, conferences, workshops, training materials
- "not work related": Personal purchases, food, entertainment, general shopping
- "other": Work-related but doesn't fit other categories

Transaction:
Description: {description}
Amount: ${amount}

Context: This is for an OT cybersecurity professional in industrial control systems.

Respond with ONLY the category name in lowercase, nothing else."""

            message = self.client.messages.create(
                model="claude-sonnet-5-5",
                max_tokens=256,
                messages=[{"role": "user", "content": prompt}],
                # Thinking is on by default on this model; keep it minimal for a
                # one-word answer. extra_body because the pinned SDK predates output_config.
                extra_body={"output_config": {"effort": "low"}},
            )

            # Read by block type, not position: the first block may be a thinking block.
            text = next((b.text for b in message.content if b.type == "text"), "")
            category = text.strip().strip('"').lower()

            # Validate category
            if category in self.categories:
                return category
            else:
                return "other"

        except Exception as e:
            print(f"  Warning: Classification failed for '{description[:40]}...': {e}")
            return None

    def add_transactions(self, transactions, use_ai=False):
        """Add transactions to database, checking for duplicates"""
        cursor = self.conn.cursor()
        added = 0
        duplicates = []

        for i, trans in enumerate(transactions, 1):
            category = None
            ai_classified = 0

            # Try AI classification if enabled
            if use_ai and self.client:
                print(f"  Classifying {i}/{len(transactions)}: {trans['description'][:50]}...")
                category = self.classify_transaction(trans['description'], trans['amount'])
                if category:
                    ai_classified = 1
                    print(f"    → {category}")

            try:
                cursor.execute("""
                               INSERT INTO transactions (trans_date, description, reference_number, amount, category,
                                                         ai_classified)
                               VALUES (?, ?, ?, ?, ?, ?)
                               """, (trans['date'], trans['description'], trans['reference'], trans['amount'], category,
                                     ai_classified))
                added += 1
            except sqlite3.IntegrityError:
                duplicates.append(trans)

        self.conn.commit()

        return added, duplicates

    def export_to_excel(self, output_path):
        """Export transactions to Excel with monthly worksheets"""
        cursor = self.conn.cursor()
        cursor.execute("""
                       SELECT id, trans_date, description, reference_number, amount, category, ai_classified
                       FROM transactions
                       ORDER BY trans_date
                       """)

        transactions = cursor.fetchall()

        if not transactions:
            print("No transactions to export")
            return

        # Group by month
        monthly_data = {}
        for trans in transactions:
            trans_id = trans[0]
            date_obj = datetime.strptime(trans[1], '%Y-%m-%d')
            month_key = date_obj.strftime('%Y-%m')
            month_name = date_obj.strftime('%b %Y')

            if month_name not in monthly_data:
                monthly_data[month_name] = []

            monthly_data[month_name].append(trans)

        # Create workbook
        wb = Workbook()
        wb.remove(wb.active)  # Remove default sheet

        # Create data validation for categories
        dv = DataValidation(type="list", formula1=f'"{",".join(self.categories)}"', allow_blank=True)

        for month_name, trans_list in sorted(monthly_data.items()):
            # Create worksheet (sanitize name for Excel)
            ws_name = month_name[:31]  # Excel limit
            ws = wb.create_sheet(title=ws_name)

            # Headers - Added ID column
            headers = ['ID', 'Date', 'Description', 'Reference', 'Amount', 'Category']
            ws.append(headers)

            # Style headers
            header_fill = PatternFill(start_color='366092', end_color='366092', fill_type='solid')
            header_font = Font(bold=True, color='FFFFFF')

            for cell in ws[1]:
                cell.fill = header_fill
                cell.font = header_font
                cell.alignment = Alignment(horizontal='center')

            # Add data
            for trans in trans_list:
                trans_id = trans[0]
                date_obj = datetime.strptime(trans[1], '%Y-%m-%d')
                formatted_date = date_obj.strftime('%d/%m/%Y')
                category = trans[5] or ''

                # Add indicator if AI classified
                if trans[6] == 1 and category:  # ai_classified flag
                    category = f"{category} 🤖"

                ws.append([trans_id, formatted_date, trans[2], trans[3], trans[4], category])

            # Add data validation to category column (now column F)
            last_row = ws.max_row
            dv.add(f'F2:F{last_row}')
            ws.add_data_validation(dv)

            # Adjust column widths
            ws.column_dimensions['A'].width = 8  # ID
            ws.column_dimensions['B'].width = 12  # Date
            ws.column_dimensions['C'].width = 45  # Description
            ws.column_dimensions['D'].width = 20  # Reference
            ws.column_dimensions['E'].width = 12  # Amount
            ws.column_dimensions['F'].width = 28  # Category

            # Format amount column as currency
            for row in range(2, last_row + 1):
                ws[f'E{row}'].number_format = '$#,##0.00'

        # Save workbook
        wb.save(output_path)
        print(f"\n✓ Excel file created: {output_path}")
        print(f"  Total worksheets: {len(monthly_data)}")

        # Count AI classifications
        ai_count = sum(1 for trans in transactions if trans[6] == 1)
        if ai_count > 0:
            print(f"  AI-classified entries: {ai_count} (marked with 🤖)")

    def close(self):
        """Close database connection"""
        if self.conn:
            self.conn.close()


def main():
    parser = argparse.ArgumentParser(
        description='Credit Card Statement Manager with AI Classification (Cross-platform)',
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Sources:
  Clipboard (-a): in your bank's web portal, select and copy the credit card
    transaction rows. Each row should start with a DDMMYY date and end with
    the amount; the card reference number may sit on the following line.
  PDF (-p): e-statement PDFs. Pass files, folders (all *.pdf inside), or a
    mix. Only pages with the "Date of / Description / Debits / Credits"
    table are read; cover pages, points summaries and trailing pages are
    skipped. Multi-line rows (foreign fee breakdowns, refund details) are
    joined into one description. Credits are stored as negative amounts.
  Duplicates (same date, reference and amount) are skipped automatically,
  so re-importing a statement is safe.

Examples (run inside the project environment: `uv run ...` or
`.venv/bin/python ...`; a bare `python3` is the system interpreter and
will not have the dependencies installed):
  # Add transactions from clipboard (no AI)
  uv run statement_manager.py -d statements.db -a

  # Import one PDF statement
  uv run statement_manager.py -d statements.db -p estatement.pdf

  # Import every PDF in a folder, plus one more file
  uv run statement_manager.py -d statements.db -p ~/Statements/2026 extra.pdf

  # Import PDFs and classify with Claude for tax purposes
  uv run statement_manager.py -d statements.db -c config.json -p ~/Statements --classify

  # Export everything to an Excel workbook (one worksheet per month)
  uv run statement_manager.py -d statements.db -e expenses.xlsx

  # Full round: import, classify and export in one go
  uv run statement_manager.py -d statements.db -c config.json -p ~/Statements --classify -e expenses.xlsx

  # Keep a database per financial year
  uv run statement_manager.py -d fy2026.db -p ~/Statements/FY2026
  uv run statement_manager.py -d fy2026.db -e fy2026_expenses.xlsx

config.json format (keep this file out of version control):
  {"anthropic_api_key": "sk-ant-..."}

Categories used by --classify:
  software, professional membership, technical library,
  magazines and journals, training, not work related, other

Excel export notes:
  - Column A holds the database ID so edits can be synced back
    (see excel_to_db_sync.py).
  - AI-classified rows are marked with a robot emoji in the Category column.
  - The Category column has a dropdown so manual fixes stay valid.

Running with no arguments prints this help."""
    )

    parser.add_argument('-d', '--database', required=True,
                        help='Path to SQLite database file')
    parser.add_argument('-c', '--config',
                        help='Path to config file (JSON with anthropic_api_key)')
    parser.add_argument('-a', '--add', action='store_true',
                        help='Add transactions from clipboard to database')
    parser.add_argument('-p', '--pdf', nargs='+', metavar='PATH',
                        help='Add transactions from PDF statement file(s) and/or folder(s)')
    parser.add_argument('--classify', action='store_true',
                        help='Use AI to classify transactions (requires -c and -a/-p)')
    parser.add_argument('-e', '--export',
                        help='Export database to Excel file')

    # No arguments: show the full help with examples instead of a terse error.
    if len(sys.argv) == 1:
        parser.print_help()
        sys.exit(0)

    args = parser.parse_args()

    # Validate arguments
    if args.classify and not (args.add or args.pdf):
        print("Error: --classify requires -a (clipboard) or -p (PDF)")
        sys.exit(1)

    if args.classify and not args.config:
        print("Error: --classify requires -c (config) flag with API key")
        sys.exit(1)

    if not (args.add or args.pdf or args.export):
        parser.print_usage()
        print("Error: nothing to do. Use -a, -p and/or -e.")
        sys.exit(1)

    # Initialize manager
    manager = StatementManager(args.database, args.config)

    try:
        transactions = []

        if args.add:
            print("Parsing clipboard data...")
            found = manager.parse_clipboard()
            if not found:
                print("No valid transactions found in clipboard")
            else:
                print(f"Found {len(found)} transactions in clipboard")
                transactions.extend(found)

        if args.pdf:
            pdf_files = StatementManager.collect_pdf_files(args.pdf)
            if not pdf_files:
                print("No PDF files to process")
            for pdf_file in pdf_files:
                print(f"Parsing {pdf_file}...")
                found, pages = manager.parse_pdf(pdf_file)
                if found:
                    page_list = ", ".join(str(p) for p in pages)
                    print(f"  Found {len(found)} transactions on page(s) {page_list}")
                    transactions.extend(found)
                else:
                    print("  No transaction pages found")

        if transactions:
            print(f"\nAdding {len(transactions)} transactions to database")
            if args.classify:
                print("AI Classification enabled...")

            added, duplicates = manager.add_transactions(transactions, use_ai=args.classify)

            print(f"\n✓ Added {added} new transactions")

            if duplicates:
                print(f"\n⚠️  Found {len(duplicates)} duplicate(s):")
                for dup in duplicates:
                    print(f"  {dup['date']} - {dup['description'][:50]} - ${dup['amount']}")
                print("\nDuplicate transactions were not added to the database.")
                if not args.export:
                    sys.exit(1)
        elif args.add or args.pdf:
            print("\nNothing to add")

        # Export to Excel if requested
        if args.export:
            print(f"\nExporting to Excel...")
            manager.export_to_excel(args.export)

    finally:
        manager.close()


if __name__ == "__main__":
    main()
