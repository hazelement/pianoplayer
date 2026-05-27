"""PDF layout analysis for preserving source system/page layout.

Extracts system break positions from a PDF using PyMuPDF (fitz) geometry
analysis and maps them to MusicXML measure numbers.
"""

from __future__ import annotations

import logging
import statistics
from typing import TYPE_CHECKING

from pianoplayer.models import LayoutInfo, SystemBreak
from pianoplayer.musicxml_io import count_measures

if TYPE_CHECKING:
    import fitz

logger = logging.getLogger(__name__)


def analyze_pdf_layout(
    pdf_path: str,
    musicxml_path: str,
    max_pages: int = 200,
) -> LayoutInfo | None:
    """Analyze a source PDF to extract system break positions.

    Uses bimodal Y-gap distribution analysis to distinguish between
    intra-system gaps (between staves) and inter-system gaps (between
    systems/lines). Maps detected system boundaries to MusicXML measure
    numbers using uniform distribution.

    Args:
        pdf_path: Path to the source PDF file.
        musicxml_path: Path to the OMR-generated MusicXML file.
        max_pages: Maximum number of pages to analyze.

    Returns:
        LayoutInfo with system breaks mapped to measure numbers, or None
        if analysis fails.
    """
    try:
        import fitz  # PyMuPDF
    except ImportError:
        logger.warning("PyMuPDF (fitz) not installed; skipping PDF layout analysis")
        return None

    try:
        # Step 1: Count measures from MusicXML (OMR output).
        total_measures = count_measures(musicxml_path)
        if total_measures < 2:
            logger.warning(
                "MusicXML has fewer than 2 measures (%d); skipping layout analysis",
                total_measures,
            )
            return None

        # Step 2: Extract staff line Y coordinates from PDF pages.
        all_systems: list[list[float]] = []
        system_page_nums: list[int] = []  # actual PDF page for each system
        page_breaks: list[int] = []  # system indices that start on a new page
        doc = fitz.open(pdf_path)
        pages_to_analyze = min(len(doc), max(max_pages, 1))
        if pages_to_analyze < len(doc):
            logger.warning(
                "PDF layout analysis truncated: analyzing %d of %d pages "
                "(max_pages=%d). Increase max_pages for full coverage.",
                pages_to_analyze,
                len(doc),
                max_pages,
            )
        prev_page_with_systems: int | None = None

        for page_num in range(pages_to_analyze):
            page = doc[page_num]
            y_coords = _detect_staff_lines(page)
            if not y_coords:
                logger.debug("No staff lines detected on page %d", page_num)
                continue

            # Compute threshold for this page using bimodal split.
            threshold = _compute_system_gap_threshold(y_coords, page_height=page.rect.height)
            systems = _group_into_systems(y_coords, threshold)

            if len(systems) <= 1:
                logger.debug("Page %d has %d system(s), skipping", page_num, len(systems))
                continue

            system_count = len(systems)
            all_systems.extend(systems)
            system_page_nums.extend([page_num] * system_count)

            # Track page boundaries for new_page detection.
            if prev_page_with_systems is not None:
                page_breaks.append(len(all_systems) - system_count)
            prev_page_with_systems = page_num

        doc.close()

        if not all_systems:
            logger.warning("No staff systems detected in PDF: %s", pdf_path)
            return None

        # Step 3: Map system boundaries to measure numbers.
        total_systems = len(all_systems)
        if total_systems < 2:
            logger.warning(
                "Only %d system(s) detected in PDF; need at least 2 for layout preservation",
                total_systems,
            )
            return None

        system_breaks = _map_systems_to_measures(
            total_systems, total_measures, page_breaks, system_page_nums,
        )

        return LayoutInfo(
            system_breaks=system_breaks,
            total_measures_source=total_measures,
        )

    except Exception as exc:
        logger.warning("PDF layout analysis failed for %s: %s", pdf_path, exc)
        return None


def _detect_staff_lines(page: "fitz.Page") -> list[float]:
    """Detect staff line Y coordinates on a PDF page.

    Uses vector graphics extraction (drawings) for speed and accuracy.
    Returns sorted Y coordinates (top to bottom) scaled to page coordinates.
    """
    return _detect_staff_lines_vector(page)


def _detect_staff_lines_vector(page: "fitz.Page") -> list[float]:
    """Detect staff lines from PDF vector drawing commands.

    Staff lines are typically horizontal lines of similar length,
    evenly spaced in groups of 5. Filters out page borders.
    """
    drawings = page.get_drawings()
    if not drawings:
        return []

    page_w = page.rect.width
    page_h = page.rect.height
    margin = 10.0  # Ignore lines within 10pt of page edges.

    # Collect horizontal line segments.
    h_lines: list[tuple[float, float, float]] = []  # (y, x1, x2)

    for drawing in drawings:
        for item in drawing.get("items", []):
            if item[0] != "l":  # "l" = line segment.
                continue
            p1, p2 = item[1], item[2]
            x1, y1 = p1.x, p1.y
            x2, y2 = p2.x, p2.y

            # Must be mostly horizontal.
            if abs(y2 - y1) > 3:
                continue
            # Must be reasonably long (staff lines span most of the page width).
            length = abs(x2 - x1)
            if length < page_w * 0.3:
                continue

            avg_y = (y1 + y2) / 2
            # Filter out page borders.
            if avg_y < margin or avg_y > page_h - margin:
                continue
            # Filter out lines at page edges (horizontal borders).
            avg_x = (x1 + x2) / 2
            if avg_x < margin or avg_x > page_w - margin:
                continue

            h_lines.append((avg_y, min(x1, x2), max(x1, x2)))

    if not h_lines:
        return []

    # Group lines by Y coordinate (within 3pt tolerance).
    h_lines.sort(key=lambda l: l[0])
    unique_ys: list[float] = []
    current_group: list[float] = [h_lines[0][0]]

    for i in range(1, len(h_lines)):
        if h_lines[i][0] - h_lines[i - 1][0] <= 3:
            current_group.append(h_lines[i][0])
        else:
            unique_ys.append(statistics.mean(current_group))
            current_group = [h_lines[i][0]]
    if current_group:
        unique_ys.append(statistics.mean(current_group))

    return sorted(unique_ys)


def _compute_system_gap_threshold(
    y_coords: list[float], page_height: float = 1000.0
) -> float:
    """Compute the Y-gap threshold separating intra-system from inter-system gaps.

    Uses bimodal gap analysis: finds the largest jump between consecutive
    sorted Y-gaps to distinguish small gaps (within systems) from large
    gaps (between systems). Falls back to 5x median gap when no clear
    bimodal split is detected.
    """
    if len(y_coords) < 4:
        return page_height * 0.05

    gaps = [y_coords[i + 1] - y_coords[i] for i in range(len(y_coords) - 1)]
    if not gaps:
        return page_height * 0.05

    sorted_gaps = sorted(gaps)

    # Find the largest absolute gap between consecutive sorted gaps.
    # This identifies the boundary between intra-system and inter-system gaps.
    best_split = None
    best_jump = 0.0

    for i in range(1, len(sorted_gaps)):
        jump = sorted_gaps[i] - sorted_gaps[i - 1]
        if jump > best_jump:
            best_jump = jump
            best_split = (sorted_gaps[i - 1] + sorted_gaps[i]) / 2

    if best_split is not None and best_jump > 50:
        system_gap_threshold = best_split
    else:
        # Fallback: use a multiplier of the median gap.
        median_gap = statistics.median(sorted_gaps)
        system_gap_threshold = median_gap * 5.0

    # Clamp to reasonable page-relative values.
    min_threshold = page_height * 0.03
    max_threshold = page_height * 0.25
    system_gap_threshold = max(min_threshold, min(max_threshold, system_gap_threshold))

    return system_gap_threshold


def _group_into_systems(
    y_coords: list[float], threshold: float
) -> list[list[float]]:
    """Group staff line Y coordinates into systems.

    Lines separated by more than `threshold` points start a new system.
    Returns a list of systems, each containing sorted Y coordinates.
    """
    if not y_coords:
        return []

    systems: list[list[float]] = [[y_coords[0]]]

    for i in range(1, len(y_coords)):
        gap = y_coords[i] - y_coords[i - 1]
        if gap > threshold:
            systems.append([y_coords[i]])
        else:
            systems[-1].append(y_coords[i])

    return systems


def _map_systems_to_measures(
    total_systems: int,
    total_measures: int,
    page_breaks: list[int],
    system_page_nums: list[int],
) -> list[SystemBreak]:
    """Map system boundaries to measure numbers using uniform distribution.

    Distributes measures evenly across systems, handling remainders
    by spreading extra measures across systems. The first system starts
    at measure 1 (no break needed). Each subsequent system gets a break
    at the measure where that system starts.

    Args:
        total_systems: Total number of systems detected across all pages.
        total_measures: Total number of measures in the MusicXML score.
        page_breaks: List of system indices that start on a new page.
        system_page_nums: Actual PDF page number for each system.

    Returns:
        List of SystemBreak objects (one per system boundary, excluding
        the first system which starts at measure 1).
    """
    if total_systems < 2:
        return []

    # Calculate base measures per system and remainder.
    base_per_system = total_measures // total_systems
    remainder = total_measures % total_systems

    breaks: list[SystemBreak] = []
    cumulative_measures = 0

    for system_idx in range(1, total_systems):
        # Distribute remainder: first `remainder` systems get one extra measure.
        measures_in_this_system = base_per_system + (1 if system_idx <= remainder else 0)
        cumulative_measures += measures_in_this_system
        measure_number = min(cumulative_measures, total_measures)

        # Ensure measure numbers are strictly increasing.
        if breaks and measure_number <= breaks[-1].measure_number:
            measure_number = breaks[-1].measure_number + 1

        # Determine if this system starts on a new page.
        new_page = system_idx in page_breaks

        # Use actual PDF page number from tracking.
        page_number = system_page_nums[system_idx]

        breaks.append(SystemBreak(
            measure_number=measure_number,
            page_number=page_number,
            new_page=new_page,
        ))

    return breaks
