"""Image correction, against the values measured on real scans."""
from PIL import Image, ImageStat
from renfield_mcp_scanner.pipeline import correct, paper_peaks


def _page(r, g, b, ink=0.06):
    """A synthetic page: mostly paper at (r,g,b), a minority of dark ink."""
    im = Image.new("RGB", (400, 500), (r, g, b))
    px = im.load()
    for y in range(int(500 * ink)):
        for x in range(400):
            px[x, y] = (20, 20, 20)
    return im


def test_paper_peak_finds_the_paper_not_the_ink():
    assert paper_peaks(_page(226, 236, 253)) == [226, 236, 253]


def test_blue_cast_is_neutralised():
    before = _page(226, 236, 253)
    r0, g0, b0 = ImageStat.Stat(before).mean
    r1, g1, b1 = ImageStat.Stat(correct(before)).mean
    assert (b0 - r0) > 20            # the real scanner measured +26
    assert abs(b1 - r1) < 3          # neutral afterwards


def test_dark_page_is_left_alone():
    # A photo or inverted print has no paper peak to anchor on; rescaling it
    # would blow it out.
    dark = Image.new("RGB", (50, 50), (40, 40, 45))
    assert correct(dark).tobytes() == dark.convert("RGB").tobytes()


def test_correction_preserves_dpi_on_save(tmp_path):
    # PIL drops pHYs on re-save and img2pdf sizes the PDF page from it: losing
    # this turned an A4 page into a 26-inch page with perfect pixels.
    from renfield_mcp_scanner.pipeline import correct_file
    p = tmp_path / "p0001.png"
    _page(226, 236, 253).save(p, dpi=(300, 300))
    correct_file(p, dpi=300.0)
    with Image.open(p) as im:
        assert round(im.info["dpi"][0]) == 300
