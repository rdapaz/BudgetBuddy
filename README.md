# BudgetBuddy

A polished PySide6 desktop application for Australian household budgeting. Import bank transactions, classify them with AI, track spending against budgets, and visualise your finances.

![Python 3.10+](https://img.shields.io/badge/python-3.10%2B-blue)
![PySide6](https://img.shields.io/badge/GUI-PySide6-green)
![License: MIT](https://img.shields.io/badge/license-MIT-brightgreen)

## Features

### Desktop GUI
- **Dashboard** — Monthly spending summary with stat cards and category breakdown charts
- **Transactions** — Searchable, sortable transaction list with inline category editing
- **Reports** — Visual spending reports with matplotlib charts (category, trend, monthly comparison)
- **Budgets** — Set monthly budgets per category with progress tracking and alerts
- **Settings** — Classification engine configuration, data import, model training, and data coverage overview

### AI Classification (Dual Engine)
- **Anthropic Claude API** — Cloud-based classification using `claude-sonnet-5-5` with batch processing
- **Go CLI classifier** — Same categories and prompt as the GUI engine, with concurrent workers for bulk runs (`classifier.go`)
- **Local DistilBERT Model** — Train your own model from corrected data for free, offline classification
- **Smart Fallback** — If the selected engine is unavailable, automatically falls back to the other
- Toggle between engines from Settings with a single click

### Security
- **Keyring Integration** — API keys stored in Windows Credential Manager (or macOS Keychain / Linux Secret Service), never in config files
- Safe to share the app without exposing API credits

### 19 Budget Categories (9 Groups)

| Group | Categories |
|-------|-----------|
| Housing | Mortgage/Rent, Utilities, Home Maintenance |
| Transport | Fuel, Car Maintenance, Public Transport, Parking & Tolls |
| Food | Groceries, Dining Out |
| Personal | Clothing, Health & Medical, Personal Care |
| Financial | Insurance, Bank Fees & Interest |
| Lifestyle | Entertainment, Subscriptions |
| Family | Childcare & Education, Pets |
| Other | Other |
| — | Uncategorised |

### Quality of Life
- **Transaction Search** — Seamlessly search by description, date (`2026-02` for all Feb transactions), or category
- **Excel Export** — Export currently displayed transactions to styled `.xlsx` with one click or `Ctrl+E`
- **Keyboard Shortcuts** — `Ctrl+1` through `Ctrl+5` for instant tab navigation
- **Help System** — `(?)` buttons on every view and card, rendering a built-in markdown help file
- **Persistent Layout** — Window size and position remembered between launches

## Installation

### Prerequisites
- Python 3.10+ (tested on 3.14)
- [uv](https://docs.astral.sh/uv/) or pip
- Go 1.21+ and a C compiler (only for the legacy Go classifier — `go-sqlite3` uses cgo; on macOS run `xcode-select --install`)

### Setup (Python)

```bash
git clone https://github.com/rdapaz/BudgetBuddy.git
cd BudgetBuddy

# Option A: uv (recommended)
uv venv .venv
uv pip install -r requirements.txt

# Option B: pip
python3 -m venv .venv
source .venv/bin/activate        # Windows: .venv\Scripts\activate
pip install -r requirements.txt
```

`pip install -e .` also works and additionally installs a `budgetbuddy` console command.

> The first install downloads PyTorch (several hundred MB). If `uv` times out, retry with `UV_HTTP_TIMEOUT=600`.

### Setup (Go classifier)

```bash
go build -o classifier .
./classifier -h
```

`go run classifier.go ...` also works without a build step.

### Key Dependencies
- `PySide6` — Qt GUI framework
- `anthropic` — Claude API client
- `torch` + `transformers` — Local DistilBERT model
- `matplotlib` — Charts and reports
- `openpyxl` — Excel export
- `keyring` — Secure API key storage
- `scikit-learn` — Model training utilities

## Usage

### Launch the App

```bash
.venv/bin/python budgetbuddy.py      # or: source .venv/bin/activate && python budgetbuddy.py
```

The app expects `expenses.db` in the project root. Create it first with `statement_manager.py` (see below) if it doesn't exist.

### Recommended Workflow

1. **Import** — Paste bank transactions from clipboard via Settings → Import, or load e-statement PDFs with `statement_manager.py -p` (below)
2. **Classify with Claude API** — Use the Anthropic engine to bulk-classify transactions
3. **Review & Correct** — Manually verify and fix any misclassifications in the Transactions tab
4. **Train Local Model** — Once you have enough corrected data, train DistilBERT from Settings
5. **Use Either Engine** — For future imports, use whichever engine you prefer

### Legacy CLI Tools

The original command-line tools are still available:

```bash
# Import from clipboard
python statement_manager.py -d expenses.db -a

# Import e-statement PDFs: single files, whole folders (every *.pdf inside), or a mix.
# Only pages carrying the "Date of / Description / Debits / Credits" table are read,
# so cover pages and trailing pages are ignored. Multi-line rows (foreign fee
# breakdowns, refund details) are joined into one description with " | ", credits
# are stored as negative amounts, and re-importing a statement is skipped as duplicates.
python statement_manager.py -d expenses.db -p estatement.pdf
python statement_manager.py -d expenses.db -p ~/Statements/FY2026
python statement_manager.py -d expenses.db -c config.json -p ~/Statements --classify

# Run with no arguments for the full list of examples
python statement_manager.py

# Classify with Go (concurrent) into the same 19 budget categories as the GUI.
# Key comes from config.json {"anthropic_api_key": "..."} or, if that file is
# absent, from $ANTHROPIC_API_KEY. Writes budget_category, same column the GUI uses.
go run classifier.go -d expenses.db -w 10
# or, after `go build -o classifier .`:
./classifier -d expenses.db -w 10 --dry-run     # preview without writing
./classifier -list                              # print the category list
./classifier -mode tax -d expenses.db           # legacy tax-deduction categories

# Sync corrections from an edited Excel export back into the database
python excel_to_db_sync.py -i expenses.xlsx -d expenses.db --dry-run

# Export to Excel
python statement_manager.py -d expenses.db -e expenses.xlsx

# Train local model
python expense_classifier_trainer.py -i expenses.xlsx

# Predict with local model
python expense_classifier_predictor.py -d expenses.db -m models
```

## Project Structure

```
BudgetBuddy/
├── budgetbuddy.py              # App entry point
├── gui/
│   ├── main_window.py          # Main window with sidebar navigation
│   ├── categories.py           # 19 budget categories definition
│   ├── theme.py                # Colours, fonts, shared styles
│   ├── help.md                 # Built-in help documentation
│   ├── models/
│   │   ├── database.py         # SQLite data access layer
│   │   └── api_key.py          # Keyring-based API key management
│   ├── views/
│   │   ├── dashboard.py        # Monthly spending overview
│   │   ├── transactions.py     # Transaction list with search/export
│   │   ├── reports.py          # Charts and visual reports
│   │   ├── budgets.py          # Budget tracking per category
│   │   └── settings.py         # Engine config, import, training
│   ├── widgets/
│   │   ├── chart_canvas.py     # Matplotlib canvas widget
│   │   ├── stat_card.py        # Dashboard stat card widget
│   │   └── help_window.py      # Help dialog with HTML rendering
│   └── workers/
│       ├── classify_worker.py  # API + Local classification threads
│       └── train_worker.py     # DistilBERT training thread
├── statement_manager.py        # Legacy CLI import/export
├── classifier.go               # Go concurrent classifier, budget + legacy tax modes (go build -o classifier .)
├── excel_to_db_sync.py         # Push Excel edits back into SQLite
├── excel_to_processed_table.py # Load Excel into the processed table
├── expense_classifier_trainer.py   # Legacy CLI trainer
├── expense_classifier_predictor.py # Legacy CLI predictor
├── pyproject.toml              # Package metadata (pip install -e .)
└── requirements.txt            # Pinned-minimum runtime deps
```

## Configuration

- **API Key**: Stored securely via `keyring` — configure in Settings → Classification Engine
- **Engine Preference**: Saved in SQLite `app_settings` table
- **Window Geometry**: Saved via Qt's `QSettings` (Windows Registry)
- **No config files needed** — all settings managed through the GUI

## License

MIT
