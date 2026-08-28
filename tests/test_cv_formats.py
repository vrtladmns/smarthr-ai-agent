"""CV files the agent could not read, from the production event log.

23 cv_text_extract_failed events. kanika1662@gmail.com sent Kanika_Sr.BDM.pdf,
537 KB, two pages, each one a single 1153x1612 JPEG and no text layer at all -
readable by eye, invisible to a parser. One candidate sent a legacy .doc, an
extension the agent accepted as a CV and then had no branch for.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import recruiter_agent as ra

RTF = rb"{\rtf1\ansi\deff0 {\fonttbl{\f0 Arial;}}\f0\fs24 Kanika Sharma \par Senior BDM \par}"
OLE2 = b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1" + b"\x00" * 40


# --- what the file actually is ------------------------------------------------

def test_sniffing_beats_the_extension():
    assert ra.sniff_cv_format(b"%PDF-1.7 ...") == "pdf"
    assert ra.sniff_cv_format(b"PK\x03\x04...") == "docx"
    assert ra.sniff_cv_format(RTF) == "rtf"
    assert ra.sniff_cv_format(OLE2) == "doc"
    assert ra.sniff_cv_format(b"Kanika Sharma, BDM") is None


def test_a_doc_extension_no_longer_swallows_the_cv():
    """.doc was in CV_EXTENSIONS with no branch in extract_cv_text, so every one
    read as empty and the candidate was told their CV was unreadable."""
    assert ".doc" in ra.CV_EXTENSIONS
    assert "Kanika Sharma" in ra.extract_cv_text("resume.doc", RTF)
    assert "Kanika Sharma" in ra.extract_cv_text("resume.doc", b"Kanika Sharma\nSenior BDM")


def test_a_misnamed_file_is_read_by_content():
    assert "Kanika" in ra.extract_cv_text("resume.txt", RTF), "RTF named .txt"
    assert "Kanika" in ra.extract_cv_text("cv.docx", b"Kanika Sharma plain text"), "text named .docx"


def test_a_real_legacy_doc_is_attempted_not_ignored():
    """Returns empty without a converter installed, but must not raise and must
    say so in the log rather than failing silently."""
    assert ra.extract_cv_text("resume.doc", OLE2) == ""


# --- OCR resolution -----------------------------------------------------------

def test_ocr_never_renders_below_the_scan_it_is_reading():
    class Page:
        rect = type("R", (), {"width": 288.0, "height": 432.0})()
        def get_images(self, full=False):
            return [(1, 0, 1153, 1612)]        # xref, smask, width, height
    dpi = ra.ocr_dpi_for_page(Page())
    assert dpi > ra.OCR_DPI, "200 dpi downsampled a 1153px-wide scan to 800px"
    assert dpi <= ra.OCR_MAX_DPI


def test_ocr_dpi_falls_back_safely():
    class Bare:
        rect = type("R", (), {"width": 0.0, "height": 0.0})()
        def get_images(self, full=False): return []
    assert ra.ocr_dpi_for_page(Bare()) == ra.OCR_DPI

    class Broken:
        @property
        def rect(self): raise RuntimeError("no rect")
        def get_images(self, full=False): return []
    assert ra.ocr_dpi_for_page(Broken()) == ra.OCR_DPI


def test_an_image_only_pdf_still_reports_nothing_without_ocr():
    """The honest outcome when Tesseract is absent: empty, not a crash."""
    assert ra.extract_cv_text("scan.pdf", b"%PDF-1.4\nnot really a pdf") == ""


if __name__ == "__main__":
    import pytest
    sys.exit(pytest.main([__file__, "-q"]))
