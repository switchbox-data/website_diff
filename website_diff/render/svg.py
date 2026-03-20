"""Pre-render inline <svg> elements to PNG for diffing.

Inline SVGs (e.g. from plotnine/matplotlib charts inlined into HTML) cannot be
meaningfully text-diffed — inserting <ins>/<del> tags inside SVG markup produces
invalid SVG that browsers won't render.  This module converts each inline <svg>
to a rasterized PNG <img> in the prerendered copies so the existing image-diff
pipeline can handle them.

Note: BeautifulSoup's html.parser lowercases SVG attributes (viewBox → viewbox),
which breaks cairosvg.  We extract raw SVG markup from the HTML file on disk for
rasterization, while using BS4 only for locating and replacing elements.
"""

from __future__ import annotations

import os
import re

import cairosvg
from bs4 import BeautifulSoup, Tag
from loguru import logger

_MIN_VIEWBOX_WIDTH = 100

_SVG_BLOCK_RE = re.compile(r"<svg\b[^>]*>.*?</svg>", re.DOTALL)


def _viewbox_width_from_raw(svg_text: str) -> float | None:
    """Extract viewBox width from raw SVG markup (case-preserving)."""
    m = re.search(r'viewBox="([^"]*)"', svg_text)
    if not m:
        return None
    parts = m.group(1).split()
    if len(parts) >= 4:
        try:
            return float(parts[2])
        except ValueError:
            return None
    return None


def _fig_id_from_raw(svg_text: str, html_text: str) -> str | None:
    """Find the ancestor div id='fig-*' for an SVG in the raw HTML."""
    pos = html_text.find(svg_text)
    if pos == -1:
        return None
    preceding = html_text[:pos]
    matches = list(re.finditer(r'<div\s[^>]*id="(fig-[^"]*)"', preceding))
    if matches:
        return matches[-1].group(1)
    return None


def render(rootdir: str, relpath: str, soup: BeautifulSoup, selector: str) -> None:
    """Replace inline <svg> elements with rasterized <img> tags.

    Parameters match the signature used by altair.render / plotly.render:
      rootdir  — absolute path to the HTML page being processed
      relpath  — subdirectory name for prerendered images (e.g. "prerendered")
      soup     — parsed BeautifulSoup of the page (mutated in place)
      selector — CSS selector for the content root
    """
    root_elem = soup.select_one(selector)
    if root_elem is None:
        return

    bs4_svgs = root_elem.find_all("svg")
    if not bs4_svgs:
        return

    with open(rootdir, "r", encoding="utf-8") as f:
        raw_html = f.read()
    raw_svg_blocks = _SVG_BLOCK_RE.findall(raw_html)

    if len(raw_svg_blocks) != len(bs4_svgs):
        logger.warning(
            f"SVG count mismatch: {len(raw_svg_blocks)} in raw HTML vs "
            f"{len(bs4_svgs)} in BS4. Falling back to index-based matching."
        )

    page_dir = os.path.dirname(rootdir)
    page_stem = os.path.splitext(os.path.basename(rootdir))[0]
    out_dir = os.path.join(page_dir, relpath)
    os.makedirs(out_dir, exist_ok=True)

    replaced = 0
    for i, bs4_svg in enumerate(bs4_svgs):
        if i >= len(raw_svg_blocks):
            break

        raw_svg = raw_svg_blocks[i]
        vb_width = _viewbox_width_from_raw(raw_svg)
        if vb_width is not None and vb_width < _MIN_VIEWBOX_WIDTH:
            continue

        fig_id = _fig_id_from_raw(raw_svg, raw_html)
        if fig_id:
            png_name = f"{fig_id}_{page_stem}.png"
        else:
            png_name = f"inline_svg_{page_stem}_{i}.png"

        png_path = os.path.join(out_dir, png_name)

        try:
            cairosvg.svg2png(
                bytestring=raw_svg.encode("utf-8"),
                write_to=png_path,
                output_width=int(vb_width * 2) if vb_width else None,
            )
        except Exception as exc:
            logger.warning(f"Failed to rasterize SVG #{i} ({png_name}): {exc}")
            continue

        width_attr = bs4_svg.get("width")
        img_attrs: dict[str, str] = {"src": os.path.join(relpath, png_name)}
        if width_attr:
            img_attrs["width"] = str(width_attr)
        css_class = bs4_svg.get("class")
        if css_class:
            img_attrs["class"] = (
                " ".join(css_class) if isinstance(css_class, list) else str(css_class)
            )

        new_img = soup.new_tag("img", **img_attrs)
        bs4_svg.replace_with(new_img)
        replaced += 1

    if replaced:
        logger.info(f"Replaced {replaced} inline SVG(s) with PNGs in {rootdir}")
