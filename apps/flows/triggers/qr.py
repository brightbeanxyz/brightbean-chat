"""QR codes for ref-URL triggers, rendered locally.

SPEC §21 phase 2 asks for the ref URL and its QR. The issue is explicit that the
code comes from a local library and not an external service, and the reason is
worth stating: handing a workspace's deep link to an image API publishes which
accounts a self-hosted deployment runs, to a third party, from the server.

:mod:`segno` is pure Python with no runtime dependencies on 3.12 and renders both
formats without Pillow.
"""

import io

import segno

__all__ = ["ERROR_CORRECTION", "PNG_SCALE", "SVG_SCALE", "render_png", "render_svg"]

#: Fifteen percent recovery. The default for a code that ends up on a flyer or a
#: shop window, where a scuff is likelier than a clean scan.
ERROR_CORRECTION = "m"

SVG_SCALE = 8
PNG_SCALE = 8
QUIET_ZONE = 2


def render_svg(payload: str) -> bytes:
    """An SVG QR for ``payload``, with no XML declaration or DOCTYPE.

    Both are omitted so the markup can be embedded as well as served, and so
    there is no external-entity surface at all in a document a browser is about
    to parse.
    """
    buffer = io.BytesIO()
    segno.make(payload, error=ERROR_CORRECTION).save(
        buffer,
        kind="svg",
        scale=SVG_SCALE,
        border=QUIET_ZONE,
        xmldecl=False,
        svgns=True,
        omitsize=False,
    )
    return buffer.getvalue()


def render_png(payload: str) -> bytes:
    """A PNG QR for ``payload``."""
    buffer = io.BytesIO()
    segno.make(payload, error=ERROR_CORRECTION).save(buffer, kind="png", scale=PNG_SCALE, border=QUIET_ZONE)
    return buffer.getvalue()
