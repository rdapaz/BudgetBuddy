"""
QThread worker that reads e-statement PDFs off the UI thread.

pdfplumber takes a few hundred milliseconds per page, so a folder of
statements would freeze the window if parsed inline. Parsing happens here;
the Settings view inserts the results on the main thread, exactly as it does
for a clipboard import.
"""

from PySide6.QtCore import QThread, Signal


class PdfImportWorker(QThread):
    """Parse a list of PDF files and/or folders in the background."""
    progress = Signal(int, int, str)   # current, total, file name
    finished = Signal(list, list)      # results: [(file name, transactions, pages)], messages: [str]

    def __init__(self, paths: list, parent=None):
        super().__init__(parent)
        self.paths = list(paths)
        self._stop = False

    def stop(self):
        self._stop = True

    def run(self):
        results = []
        messages = []
        try:
            from gui.models.pdf_statement import collect_pdf_files, parse_pdf
        except ImportError as e:
            messages.append(f"PDF import unavailable: {e}")
            self.finished.emit(results, messages)
            return

        files = collect_pdf_files(self.paths, warn=messages.append)
        total = len(files)

        for i, path in enumerate(files, 1):
            if self._stop:
                messages.append("Stopped by user.")
                break
            self.progress.emit(i, total, path.name)
            try:
                transactions, pages = parse_pdf(path, warn=lambda m, n=path.name: messages.append(f"{n}: {m}"))
            except ImportError:
                messages.append("pdfplumber is not installed. Run: uv sync  (or pip install pdfplumber)")
                break
            except Exception as e:
                messages.append(f"{path.name}: could not read ({e})")
                continue
            results.append((path.name, transactions, pages))

        self.finished.emit(results, messages)
