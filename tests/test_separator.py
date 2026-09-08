"""Separator sheets: the only routing layer that carries its destination on the
paper, and therefore the only one that works for an unattended stack."""
import pytest

from renfield_mcp_scanner.separator import (
    PAYLOAD_PREFIX, Segment, payload_for, segment, target_from_payload,
)


def test_payload_is_namespaced_and_versioned():
    assert payload_for("household") == "RFSEP1:household"
    assert payload_for("household").startswith(PAYLOAD_PREFIX)


def test_payload_roundtrips():
    assert target_from_payload(payload_for("xidra")) == "xidra"


@pytest.mark.parametrize("payload", [
    "HAUSHALT",                       # a bare label — the naive design
    "",
    "RFSEP1:",
    "rfsep1:household",               # case matters
    "RFSEP2:household",               # a future version is not this one
    "RFSEP1:household extra",
    "RFSEP1:Household",               # uppercase ids are not minted
    "https://example.com/invoice/42",  # a real document's own QR code
    "BCD\n002\n1\nSCT\nGIBAATWW",     # a GiroCode on an invoice
])
def test_foreign_barcodes_are_not_separators(payload):
    # Real documents carry barcodes: GiroCodes, tracking codes, archive stamps.
    # Any of them matching would silently split a document or misroute it.
    assert target_from_payload(payload) is None


class _Page:
    """Stands in for a scanned page; decode_page is patched per test."""
    def __init__(self, name): self.name = name
    def __repr__(self): return self.name


def _seg(monkeypatch, pages, marks):
    """marks: {page_name: target_id} for pages that ARE separator sheets."""
    monkeypatch.setattr("renfield_mcp_scanner.separator.decode_page",
                        lambda p: marks.get(p.name))
    return segment(pages)


def test_stack_without_sheets_is_one_document(monkeypatch):
    pages = [_Page("p1"), _Page("p2"), _Page("p3")]
    out = _seg(monkeypatch, pages, {})
    assert len(out) == 1
    assert out[0].target_id is None and len(out[0].pages) == 3


def test_separator_splits_and_names_the_target(monkeypatch):
    pages = [_Page("sep"), _Page("p1"), _Page("p2")]
    out = _seg(monkeypatch, pages, {"sep": "household"})
    assert len(out) == 1
    assert out[0].target_id == "household"
    # The sheet itself is CONSUMED — it is a routing mark, not content.
    assert [p.name for p in out[0].pages] == ["p1", "p2"]


def test_mixed_stack_splits_into_several_documents(monkeypatch):
    pages = [_Page("sepA"), _Page("a1"), _Page("a2"),
             _Page("sepB"), _Page("b1")]
    out = _seg(monkeypatch, pages, {"sepA": "household", "sepB": "office"})
    assert [(s.target_id, len(s.pages)) for s in out] == [("household", 2), ("office", 1)]


def test_pages_before_the_first_sheet_keep_no_target(monkeypatch):
    # They are NOT attributed to the sheet that follows them — that would file
    # one document's pages under the next document's destination.
    pages = [_Page("x1"), _Page("sep"), _Page("a1")]
    out = _seg(monkeypatch, pages, {"sep": "household"})
    assert out[0].target_id is None and [p.name for p in out[0].pages] == ["x1"]
    assert out[1].target_id == "household"


def test_two_sheets_in_a_row_produce_no_empty_document(monkeypatch):
    pages = [_Page("sepA"), _Page("sepB"), _Page("b1")]
    out = _seg(monkeypatch, pages, {"sepA": "household", "sepB": "office"})
    assert len(out) == 1 and out[0].target_id == "office"


def test_trailing_sheet_produces_no_empty_document(monkeypatch):
    pages = [_Page("sep"), _Page("a1"), _Page("sepEnd")]
    out = _seg(monkeypatch, pages, {"sep": "household", "sepEnd": "office"})
    assert len(out) == 1 and out[0].target_id == "household"


def test_a_page_that_fails_to_decode_is_content_not_a_boundary(monkeypatch):
    pages = [_Page("p1"), _Page("broken"), _Page("p2")]
    out = _seg(monkeypatch, pages, {})
    assert len(out) == 1 and len(out[0].pages) == 3
