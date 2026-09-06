"""Turns plain text into a simple, mailable PDF for LetterStream.

Deliberately minimal: one font, one size, page breaks handled by fpdf2's own layout.
LetterStream's own coversheet (send_mail's default coversheet=True) handles address
placement for the windowed envelope, so this file's only job is readable body text —
it does not need to lay out a return address or worry about window positioning itself.
"""
from fpdf import FPDF


def text_to_pdf(body: str) -> tuple[bytes, int]:
    """Returns (pdf_bytes, page_count). page_count is required by LetterStream's own
    API (the `pages` field) — computed here rather than left for the caller to guess,
    since a wrong page count is a common real integration mistake with this API."""
    pdf = FPDF(format="Letter")
    pdf.set_auto_page_break(auto=True, margin=25)
    pdf.set_margins(25, 25, 25)
    pdf.add_page()
    pdf.set_font("Helvetica", size=12)
    for paragraph in body.split("\n\n"):
        pdf.multi_cell(0, 7, paragraph.strip())
        pdf.ln(4)
    return bytes(pdf.output()), pdf.page_no()
