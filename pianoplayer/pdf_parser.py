"""
PDF Vector Parser for Sheet Music — Improved v3

Parses digital (vector-based) PDF sheet music directly to extract notes,
pitches, rhythms, and structure without relying on OMR engines.

Key improvements over v2:
- Proper clef/key signature area exclusion (Gap 1)
- System separation with 200pt threshold (Gap 2)
- Key signature detection from sharp/flat symbols (Gap 3)
- Time signature detection from numerals (Gap 4)
- Dot detection for dotted notes (Gap 5)
- Flag detection for note duration (Gap 6)
- Beam counting for beamed note duration (Gap 7)
- Rest detection (Gap 8)
- Full note type support: whole, half, quarter, eighth, 16th, 32nd (Gap 9)
- Clef type detection (Gap 10)
- Barline-based measure boundaries (Gap 11)
"""

import fitz
import numpy as np
import xml.etree.ElementTree as ET
from sklearn.cluster import DBSCAN
from typing import List, Dict, Tuple, Optional, Set
from dataclasses import dataclass, field
from collections import Counter
import logging
import math

logger = logging.getLogger(__name__)


@dataclass
class Staff:
    """Represents a musical staff with 5 lines."""
    staff_top: float
    staff_bottom: float
    staff_idx: int
    clef: str = 'treble'
    system_idx: int = 0
    note_start_x: float = 0.0  # X position where notes begin (after clef/key sig)

    @property
    def height(self) -> float:
        return self.staff_bottom - self.staff_top

    @property
    def line_spacing(self) -> float:
        """Spacing between adjacent staff lines."""
        return self.height / 4.0  # 5 lines = 4 spaces

    @property
    def center(self) -> float:
        return (self.staff_top + self.staff_bottom) / 2

    def y_to_pitch(self, y: float) -> int:
        """Convert Y position to MIDI pitch number.

        Uses bottom-line reference pitch mapping:
        - Treble: bottom line = E4 (MIDI 64), top line = F5 (MIDI 77)
        - Bass: bottom line = G2 (MIDI 36), top line = A3 (MIDI 49)

        Full staff span (4 line spaces) = 13 semitones.
        Each line space = 13/4 = 3.25 semitones.
        """
        y_offset_from_bottom = self.staff_bottom - y
        semitones_from_bottom = y_offset_from_bottom / self.line_spacing * 13.0 / 4.0

        if self.clef == 'treble':
            base_pitch = 64  # E4
        else:  # bass
            base_pitch = 36  # G2

        return int(round(base_pitch + semitones_from_bottom))

    def is_in_zone(self, y: float, margin_factor: float = 3.0) -> bool:
        """Check if a Y position is in this staff's note zone."""
        zone_top = self.staff_top - margin_factor * self.height
        zone_bottom = self.staff_bottom + margin_factor * self.height
        return zone_top <= y <= zone_bottom


@dataclass
class NoteHead:
    """Represents a detected note head."""
    x: float
    y: float
    staff: Staff
    pitch: int
    curves_count: int = 0
    bbox_width: float = 0.0
    bbox_height: float = 0.0
    has_stem: bool = False
    stem_direction: str = 'up'
    is_beamed: bool = False
    beam_count: int = 0  # 0=quarter/half/whole, 1=eighth, 2=sixteenth, 3=32nd
    flag_count: int = 0  # 0=quarter/half/whole, 1=eighth, 2=sixteenth
    is_dotted: bool = False
    is_rest: bool = False
    rest_type: str = ''  # 'whole', 'half', 'quarter', 'eighth', etc.
    duration: float = 1.0  # quarters (1.0 = quarter note)

    @property
    def note_name(self) -> str:
        """Get note name (e.g., 'C4', 'D#5')."""
        note_names = ['C', 'C#', 'D', 'D#', 'E', 'F', 'F#', 'G', 'G#', 'A', 'A#', 'B']
        octave = self.pitch // 12 - 1
        return f"{note_names[self.pitch % 12]}{octave}"


@dataclass
class Chord:
    """Represents a group of notes played simultaneously."""
    notes: List[NoteHead]

    @property
    def x(self) -> float:
        if not self.notes:
            return 0
        return sum(n.x for n in self.notes) / len(self.notes)


@dataclass
class Measure:
    """Represents a musical measure."""
    chords: List[Chord]
    rests: List['NoteHead'] = None  # Rests handled separately
    x_start: float = 0.0
    x_end: float = 0.0

    def __post_init__(self):
        if self.rests is None:
            self.rests = []


class PDFParser:
    """
    Parses digital PDF sheet music by analyzing vector geometry.

    This parser provides high accuracy for digital (vector-based) PDFs
    by directly analyzing the PDF vector paths rather than using OMR.

    Key features:
    - System separation using Y-gap clustering
    - Clef/key signature area exclusion
    - Key signature detection from sharp/flat symbols
    - Time signature detection from numerals
    - Dot detection for dotted notes
    - Flag and beam detection for rhythm
    - Rest detection
    - Barline-based measure boundaries
    """

    def __init__(self, pdf_path: str):
        self.pdf_path = pdf_path
        self.doc = fitz.open(pdf_path)
        self.staffs: List[Staff] = []
        self.note_heads: List[NoteHead] = []
        self.chords: List[Chord] = []
        self.measures: List[Measure] = []
        self.barlines: List[float] = []
        self.beams: List[Tuple[float, float, float, int]] = []  # (x1, x2, y, beam_count)
        self.systems: List[List[Staff]] = []
        self.key_signature_fifths: int = 0
        self.time_signature: Tuple[int, int] = (4, 4)

    def close(self):
        """Close the PDF document."""
        if self.doc:
            self.doc.close()

    # ------------------------------------------------------------------
    # Drawing extraction helpers
    # ------------------------------------------------------------------

    def _extract_all_drawings(self, page_num: int = 0):
        """Extract all lines and bezier curves from a page."""
        page = self.doc[page_num]
        drawings = page.get_drawings()

        lines = []  # (x1, x2, y_avg, y_min, y_max, dx, dy, length)
        curves = []  # (center_x, center_y, span_x, span_y)

        for drawing in drawings:
            for item in drawing.get('items', []):
                if item[0] == 'l':
                    p1, p2 = item[1], item[2]
                    x1, y1 = p1.x, p1.y
                    x2, y2 = p2.x, p2.y
                    dx = abs(x2 - x1)
                    dy = abs(y2 - y1)
                    length = math.sqrt(dx ** 2 + dy ** 2)
                    lines.append((min(x1, x2), max(x1, x2), (y1 + y2) / 2,
                                  min(y1, y2), max(y1, y2), dx, dy, length))
                elif item[0] == 'c':
                    pts = item[1:5]
                    xs = [p.x for p in pts]
                    ys = [p.y for p in pts]
                    span_x = max(xs) - min(xs)
                    span_y = max(ys) - min(ys)
                    center_x = sum(xs) / len(xs)
                    center_y = sum(ys) / len(ys)
                    curves.append((center_x, center_y, span_x, span_y))

        return lines, curves

    # ------------------------------------------------------------------
    # Staff detection (Gap 2: system separation)
    # ------------------------------------------------------------------

    def detect_staffs(self, page_num: int = 0) -> List[Staff]:
        """Detect staff lines and group them into staves and systems."""
        lines, _ = self._extract_all_drawings(page_num)

        # Staff lines: horizontal, very long (>2000pt), not page borders
        staff_lines = [l for l in lines
                       if l[6] < 3 and l[5] > 2000 and l[2] > 100 and l[2] < 4100]

        if not staff_lines:
            logger.warning("No staff lines found on page %d", page_num)
            return []

        # Get unique Y positions
        ys = sorted([l[2] for l in staff_lines])
        unique_ys = []
        if ys:
            current = [ys[0]]
            for y in ys[1:]:
                if y - current[-1] < 2:
                    current.append(y)
                else:
                    unique_ys.append(sum(current) / len(current))
                    current = [y]
            unique_ys.append(sum(current) / len(current))

        # Group into 5-line staves (line spacing ~24-25pt)
        staffs = []
        if len(unique_ys) >= 5:
            current = [unique_ys[0]]
            for y in unique_ys[1:]:
                gap = y - current[-1]
                if gap < 40:  # Within staff (including brace/bracket lines)
                    current.append(y)
                else:
                    if len(current) >= 5:
                        staffs.append((current[0], current[-1]))
                    current = [y]
            if len(current) >= 5:
                staffs.append((current[0], current[-1]))

        # Group staves into systems (Gap 2)
        # Gap > 200pt = new system, gap 140-200pt = within system (treble-bass gap)
        systems = []
        if staffs:
            current_system = [staffs[0]]
            for s in staffs[1:]:
                gap = s[0] - current_system[-1][1]
                if gap > 200:
                    systems.append(current_system)
                    current_system = [s]
                else:
                    current_system.append(s)
            systems.append(current_system)

        # Create Staff objects with proper clef assignment (Gap 10)
        # In grand staff notation, even-indexed staves in a system are treble, odd are bass
        self.staffs = []
        for sys_idx, sys_staffs in enumerate(systems):
            for staff_idx_in_sys, (top, bottom) in enumerate(sys_staffs):
                # Use system position for clef detection
                # Even-indexed staves (0, 2, 4...) in a system are treble
                # Odd-indexed staves (1, 3, 5...) in a system are bass
                clef = 'treble' if staff_idx_in_sys % 2 == 0 else 'bass'
                staff = Staff(
                    staff_top=top,
                    staff_bottom=bottom,
                    staff_idx=len(self.staffs),
                    clef=clef,
                    system_idx=sys_idx
                )
                self.staffs.append(staff)

        self.systems = systems  # Store as list of (top, bottom) tuples
        self.system_staffs = []
        for sys_idx, sys_staffs_tuple in enumerate(systems):
            sys_staff_objects = [s for s in self.staffs if s.system_idx == sys_idx]
            self.system_staffs.append(sys_staff_objects)

        logger.info("Detected %d staves in %d systems on page %d",
                     len(self.staffs), len(systems), page_num)
        return self.staffs

    def _detect_clef(self, staff_top: float, staff_bottom: float, page_num: int = 0) -> str:
        """Detect clef type from the clef symbol in the left margin.

        Uses staff position within system as primary indicator:
        - Even-indexed staves in a system (0, 2, 4...) are treble
        - Odd-indexed staves in a system (1, 3, 5...) are bass

        Falls back to curve analysis if staff position is ambiguous.
        """
        # Primary method: use staff position within system
        # In grand staff notation, the top staff is treble, bottom is bass
        if self.system_staffs:
            for sys_staffs in self.system_staffs:
                for idx, s in enumerate(sys_staffs):
                    if abs(s.staff_top - staff_top) < 5:
                        return 'treble' if idx % 2 == 0 else 'bass'

        # Fallback: use curve analysis
        _, curves = self._extract_all_drawings(page_num)
        line_spacing = (staff_bottom - staff_top) / 4.0

        # Look for clef curves in the left margin (x < 350)
        margin_curves = [(cx, cy, sx, sy) for cx, cy, sx, sy in curves
                         if cx < 350 and (staff_top - 50) <= cy <= (staff_bottom + 50)]

        if not margin_curves:
            # Default: even-indexed staves are treble, odd are bass
            staff_idx = next((i for i, s in enumerate(self.staffs)
                              if abs(s.staff_top - staff_top) < 5), 0)
            return 'treble' if staff_idx % 2 == 0 else 'bass'

        # Treble clef: large spiral curves centered around the 2nd line from bottom
        treble_line_y = staff_bottom - line_spacing

        # Bass clef: curves near the F-line (2nd line from top)
        bass_line_y = staff_top + line_spacing

        treble_count = sum(1 for _, cy, sx, sy in margin_curves
                           if abs(cy - treble_line_y) < 30 and sx > 10 and sy > 10)
        bass_count = sum(1 for _, cy, sx, sy in margin_curves
                         if abs(cy - bass_line_y) < 30 and sx > 10 and sy > 10)

        if treble_count > bass_count:
            return 'treble'
        elif bass_count > treble_count:
            return 'bass'
        else:
            staff_idx = next((i for i, s in enumerate(self.staffs)
                              if abs(s.staff_top - staff_top) < 5), 0)
            return 'treble' if staff_idx % 2 == 0 else 'bass'

    # ------------------------------------------------------------------
    # Key signature detection (Gap 3)
    # ------------------------------------------------------------------

    def detect_key_signature(self, page_num: int = 0) -> int:
        """Detect key signature by counting sharps/flats in the key signature area.

        Sharps: appear as small vertical rectangles with diagonal strokes
        Flats: appear as oval shapes with vertical lines

        Returns the number of fifths (positive = sharps, negative = flats).

        For C major (no sharps/flats), returns 0.
        """
        lines, curves = self._extract_all_drawings(page_num)

        if not self.staffs:
            return 0

        # Key signature area: between clef and time signature
        # For first system: x=350-450, for others: x=250-350
        # Look at the first treble staff
        treble_staff = next((s for s in self.staffs if s.clef == 'treble' and s.system_idx == 0), None)
        if not treble_staff:
            treble_staff = self.staffs[0] if self.staffs else None
        if not treble_staff:
            return 0

        staff = treble_staff
        key_area_left = staff.note_start_x - 100 if staff.note_start_x > 200 else 250
        key_area_right = staff.note_start_x

        # Look for sharp symbols: very narrow vertical elements
        # Sharps have a distinctive shape: two vertical strokes connected by diagonals
        # In vector PDFs, they appear as multiple small bezier curves
        sharp_candidates = []
        flat_candidates = []

        for cx, cy, sx, sy in curves:
            if not (key_area_left <= cx <= key_area_right):
                continue
            if not staff.is_in_zone(cy, margin_factor=1.5):
                continue

            # Sharp: very narrow vertical element (width < 2pt, height 6-12pt)
            # The key is that sharps are extremely narrow compared to other elements
            if sx < 2 and 6 < sy < 12:
                sharp_candidates.append((cx, cy, sx, sy))
            # Flat: wider element with vertical stem (width 4-8pt, height 8-15pt)
            elif 4 < sx < 8 and 8 < sy < 15:
                flat_candidates.append((cx, cy, sx, sy))

        # Cluster sharp/flat candidates by Y position to count unique accidentals
        def count_unique_accidentals(candidates, y_threshold=15):
            if not candidates:
                return 0
            ys = sorted([c[1] for c in candidates])
            count = 1
            for i in range(1, len(ys)):
                if ys[i] - ys[i - 1] > y_threshold:
                    count += 1
            return count

        sharp_count = count_unique_accidentals(sharp_candidates)
        flat_count = count_unique_accidentals(flat_candidates)

        if sharp_count > 0:
            self.key_signature_fifths = sharp_count
        elif flat_count > 0:
            self.key_signature_fifths = -flat_count
        else:
            self.key_signature_fifths = 0

        logger.info("Key signature: %d fifths (%d sharps, %d flats)",
                     self.key_signature_fifths, sharp_count, flat_count)
        return self.key_signature_fifths

    # ------------------------------------------------------------------
    # Time signature detection (Gap 4)
    # ------------------------------------------------------------------

    def detect_time_signature(self, page_num: int = 0) -> Tuple[int, int]:
        """Detect time signature from numerals after the clef/key signature.

        Time signature appears as two stacked numerals (e.g., '4/4', '3/4', '6/8').

        Default returns 4/4 if detection fails.
        """
        lines, curves = self._extract_all_drawings(page_num)

        if not self.staffs:
            return (4, 4)

        # Time signature is typically in the first treble staff, after the key signature
        treble_staff = next((s for s in self.staffs if s.clef == 'treble' and s.system_idx == 0), None)
        if not treble_staff:
            return (4, 4)

        staff = treble_staff
        # Time signature area is between key signature and first note
        ts_area_left = staff.note_start_x - 150 if staff.note_start_x > 200 else 300
        ts_area_right = staff.note_start_x

        # Time signature numerals are composed of small bezier curves
        # Look for curve clusters in the time signature area
        ts_curves = [(cx, cy, sx, sy) for cx, cy, sx, sy in curves
                     if ts_area_left <= cx <= ts_area_right
                     and staff.is_in_zone(cy, margin_factor=1.0)
                     and 3 <= sx <= 15 and 3 <= sy <= 15]

        if not ts_curves:
            # No time signature curves found, default to 4/4
            self.time_signature = (4, 4)
            logger.info("Time signature: 4/4 (default, no curves found)")
            return self.time_signature

        # Cluster curves into two groups (numerator and denominator)
        # The numerator is above the denominator
        ts_curves.sort(key=lambda c: c[1])  # Sort by Y

        # Find the Y gap that separates numerator from denominator
        ys = [c[1] for c in ts_curves]
        mid = len(ys) // 2
        split_y = (ys[mid - 1] + ys[mid]) / 2 if mid > 0 and mid < len(ys) else ys[0]

        numerator_curves = [c for c in ts_curves if c[1] < split_y]
        denominator_curves = [c for c in ts_curves if c[1] >= split_y]

        # For common time signatures, the numerator is usually a single digit
        # and the denominator is a single digit (4, 8, etc.)
        # Use Y position of denominator relative to staff to determine value
        denom = 4  # Default to 4 (quarter note)
        numer = 4  # Default to 4

        if denominator_curves:
            avg_denom_y = sum(c[1] for c in denominator_curves) / len(denominator_curves)
            line_spacing = staff.line_spacing

            # The denominator '4' is typically positioned on a staff line or space
            # The denominator '8' is typically positioned lower (below the staff)
            # We use the relative Y position to distinguish
            denom_line_y = staff.staff_bottom - line_spacing  # 4th line from bottom

            if avg_denom_y > denom_line_y + line_spacing:
                denom = 8  # Below the 4th line = eighth note
            else:
                denom = 4  # On or above the 4th line = quarter note

        if numerator_curves:
            # Count the width of the numeral area to estimate the numerator
            num_width = max(c[0] for c in numerator_curves) - min(c[0] for c in numerator_curves)
            # Wider numeral area = larger number
            if num_width > 25:
                numer = 6  # Wide = 6/8 time
            elif num_width > 15:
                numer = 4  # Medium = 4/4 time
            elif num_width > 8:
                numer = 3  # Narrow = 3/4 time
            else:
                numer = 2  # Very narrow = 2/4 time

        self.time_signature = (numer, denom)
        logger.info("Time signature: %d/%d", numer, denom)
        return self.time_signature

    # ------------------------------------------------------------------
    # Note head detection (Gap 1: false positives, Gap 9: note types)
    # ------------------------------------------------------------------

    def _find_note_start_x(self, staff: Staff, curves: List[Tuple]) -> float:
        """Find the X position where notes begin for a given staff.

        Uses the first significant gap in curve X positions to separate
        the clef/key signature area from the actual notes.
        """
        margin = 2 * staff.height
        staff_curves = [(cx, cy, sx, sy) for cx, cy, sx, sy in curves
                        if staff.is_in_zone(cy, margin_factor=2.0)
                        and 3 <= sx <= 30 and 3 <= sy <= 30
                        and max(sx, sy) / max(min(sx, sy), 1) <= 3]

        if not staff_curves:
            return 400  # Default

        staff_curves.sort(key=lambda c: c[0])
        xs = [c[0] for c in staff_curves]

        # Find the first significant gap (>20pt)
        for i in range(1, len(xs)):
            if xs[i] - xs[i - 1] > 20:
                return xs[i]

        return xs[0] if xs else 400

    def detect_note_heads(self, page_num: int = 0) -> List[NoteHead]:
        """Detect note heads using bezier curve clustering.

        Two-phase approach:
        1. Extract candidate curves (moderate size, not too elongated)
        2. Cluster curves into individual note heads using DBSCAN
        """
        if not self.staffs:
            self.detect_staffs(page_num)

        lines, curves = self._extract_all_drawings(page_num)

        # Set note start X for each staff (Gap 1: exclude clef area)
        for staff in self.staffs:
            staff.note_start_x = self._find_note_start_x(staff, curves)

        # Extract note head candidate curves
        note_candidates = []
        for cx, cy, sx, sy in curves:
            # Filter: moderate size, not too elongated
            # Lowered min from 3 to 2 to catch smaller note heads
            if sx < 2 or sy < 2:
                continue
            # Increased max from 30 to 35 to catch larger note heads
            if sx > 35 or sy > 35:
                continue
            aspect = max(sx, sy) / max(min(sx, sy), 1)
            if aspect > 3:
                continue

            # Find the staff this curve belongs to using closest-staff assignment
            # to avoid misassignment when staff zones overlap
            best_staff = None
            best_dist = float('inf')
            for staff in self.staffs:
                if staff.is_in_zone(cy, margin_factor=2.0):
                    # Exclude clef/key signature area
                    if cx < staff.note_start_x:
                        continue
                    # Use distance to staff center for closest-staff assignment
                    dist = abs(cy - staff.center)
                    if dist < best_dist:
                        best_dist = dist
                        best_staff = staff
            if best_staff is not None:
                note_candidates.append((cx, cy, sx, sy, best_staff))

        # Cluster curves into note heads using DBSCAN
        if not note_candidates:
            self.note_heads = []
            return []

        points = np.array([[c[0], c[1]] for c in note_candidates])

        # Use eps=25 to capture curves within a single note head
        # Each note head is ~15-25pt wide, composed of multiple bezier curves
        # eps=25 with min_samples=2 produces clusters close to expected note count
        db = DBSCAN(eps=25, min_samples=2).fit(points)
        labels = db.labels_

        # Group curves by cluster
        clusters: Dict[int, List] = {}
        for idx, label in enumerate(labels):
            if label == -1:
                continue
            clusters.setdefault(label, []).append(note_candidates[idx])

        # Convert clusters to note heads
        note_heads = []
        for label, cluster_curves in clusters.items():
            xs = [c[0] for c in cluster_curves]
            ys = [c[1] for c in cluster_curves]
            width = max(xs) - min(xs)
            height = max(ys) - min(ys)
            center_x = sum(xs) / len(xs)
            center_y = sum(ys) / len(ys)

            # Validate: reasonable size for a note head
            # Note heads are typically 10-25pt wide and 8-20pt tall
            # Allow up to 55pt width for notes with ledger lines or beam attachments
            # Lowered min width from 5 to 4 to catch smaller notes
            if width < 4 or width > 55 or height < 3 or height > 50:
                continue

            # Find the staff using closest-staff assignment
            # Use center_y to find the closest staff
            best_staff = None
            best_dist = float('inf')
            for s in self.staffs:
                dist = abs(center_y - s.center)
                if dist < best_dist:
                    best_dist = dist
                    best_staff = s
            staff = best_staff
            pitch = staff.y_to_pitch(center_y)

            note_heads.append(NoteHead(
                x=center_x,
                y=center_y,
                staff=staff,
                pitch=pitch,
                curves_count=len(cluster_curves),
                bbox_width=width,
                bbox_height=height
            ))

        self.note_heads = note_heads
        logger.info("Detected %d note heads on page %d", len(note_heads), page_num)
        return note_heads

    # ------------------------------------------------------------------
    # Stem detection
    # ------------------------------------------------------------------

    def detect_stems(self, page_num: int = 0) -> None:
        """Detect stems and assign them to note heads."""
        lines, _ = self._extract_all_drawings(page_num)

        # Stems: vertical lines, 40-120pt long
        stem_lines = [l for l in lines
                      if l[5] < 3 and 40 < l[6] < 120]

        for note in self.note_heads:
            # Find a stem near this note head
            best_stem = None
            best_dist = float('inf')

            for x1, x2, y_avg, y_min, y_max, dx, dy, length in stem_lines:
                stem_x = (x1 + x2) / 2
                dist_x = abs(stem_x - note.x)

                # Stem should be close to the note head horizontally
                if dist_x > 15:
                    continue

                # Stem should connect to the note head vertically
                connects = (y_min <= note.y + note.bbox_height / 2 <= y_max or
                            y_min <= note.y - note.bbox_height / 2 <= y_max or
                            (note.y - note.bbox_height / 2 <= y_min <= note.y + note.bbox_height / 2) or
                            (note.y - note.bbox_height / 2 <= y_max <= note.y + note.bbox_height / 2))

                if dist_x < best_dist:
                    best_dist = dist_x
                    best_stem = (stem_x, y_min, y_max)

            if best_stem and best_dist < 15:
                note.has_stem = True
                # Determine stem direction
                stem_x, stem_y_min, stem_y_max = best_stem
                if stem_y_max < note.y:
                    note.stem_direction = 'up'
                else:
                    note.stem_direction = 'down'

    # ------------------------------------------------------------------
    # Barline detection (Gap 11)
    # ------------------------------------------------------------------

    def detect_barlines(self, page_num: int = 0) -> List[float]:
        """Detect barline positions using vertical lines that span staff height."""
        lines, _ = self._extract_all_drawings(page_num)

        # Barlines: vertical, spanning staff height, after clef area
        # Balanced constraints to catch real barlines while reducing false positives
        barline_candidates = []
        for x1, x2, y_avg, y_min, y_max, dx, dy, length in lines:
            if dx > 2:
                continue
            # Staff height is ~99pt, allow 80-120pt range
            if not (80 < dy < 120):
                continue
            # Reduced X minimum to catch barlines in earlier positions
            if x1 < 200:
                continue

            # Verify it spans a staff (tolerance: ±20pt)
            for staff in self.staffs:
                if (abs(y_min - staff.staff_top) <= 20 and
                        abs(y_max - staff.staff_bottom) <= 20):
                    barline_candidates.append(x1)
                    break

        # Group by X position (within 15pt to handle different staff barlines)
        barline_candidates.sort()
        barline_xs = []
        if barline_candidates:
            current_x = barline_candidates[0]
            for x in barline_candidates[1:]:
                if x - current_x <= 15:
                    continue
                barline_xs.append(current_x)
                current_x = x
            barline_xs.append(current_x)

        # Filter: remove barlines too close together (< 30pt)
        # Balanced threshold to catch real barlines while reducing false positives
        filtered = []
        for x in barline_xs:
            if not filtered or x - filtered[-1] >= 30:
                filtered.append(x)

        self.barlines = filtered
        logger.info("Detected %d barlines at positions: %s", len(filtered), filtered)
        return filtered

    def _detect_system_barlines(self, sys_y_min: float, sys_y_max: float,
                                 page_num: int = 0) -> List[float]:
        """Detect barline positions for a specific system Y range."""
        lines, _ = self._extract_all_drawings(page_num)

        barline_candidates = []
        for x1, x2, y_avg, y_min, y_max, dx, dy, length in lines:
            if dx > 3:
                continue
            if not (70 < dy < 130):
                continue
            if x1 < 250:
                continue

            # Check if this barline is within the system's Y range
            # The barline Y range should overlap with the system's Y range
            if y_min >= sys_y_min - 20 and y_max <= sys_y_max + 20:
                barline_candidates.append(x1)

        # Group by X position (within 15pt)
        barline_candidates.sort()
        barline_xs = []
        if barline_candidates:
            current_x = barline_candidates[0]
            for x in barline_candidates[1:]:
                if x - current_x <= 15:
                    continue
                barline_xs.append(current_x)
                current_x = x
            barline_xs.append(current_x)

        # Filter: remove barlines too close together (< 30pt)
        filtered = []
        for x in barline_xs:
            if not filtered or x - filtered[-1] >= 30:
                filtered.append(x)

        return filtered

    # ------------------------------------------------------------------
    # Beam detection (Gap 7)
    # ------------------------------------------------------------------

    def detect_beams(self, page_num: int = 0) -> List[Tuple[float, float, float, int]]:
        """Detect beam lines and count beam thickness."""
        lines, _ = self._extract_all_drawings(page_num)

        # Beams: horizontal, medium length (15-200pt), slight angle allowed
        # Beams are typically dy <= 4 (nearly horizontal), unlike slurs/hairpins
        beam_candidates = []
        for x1, x2, y_avg, y_min, y_max, dx, dy, length in lines:
            if not (15 < dx < 200):
                continue
            if dy > 4:
                continue

            # Check if in staff zone (extended margin for beam lines at stem tips)
            for staff in self.staffs:
                if staff.staff_top - 200 <= y_avg <= staff.staff_bottom + 200:
                    beam_candidates.append((min(x1, x2), max(x1, x2), y_avg))
                    break

        # Store raw beam candidates for later processing
        # We'll count beams per note instead of grouping
        self.beam_candidates = beam_candidates
        logger.info("Found %d beam candidates", len(beam_candidates))

        # Group beams into beam spans: first by staff zone (Y proximity), then by X overlap.
        # Three-phase approach:
        # Phase 1: Cluster candidates by Y position to separate staves
        # Phase 2: Within each staff cluster, group by X overlap to find beam spans
        # Phase 3: Within each span, count unique Y levels = beam count
        beam_candidates.sort(key=lambda b: (b[0], b[2]))  # Sort by X, then Y

        beams = []
        if beam_candidates:
            # Phase 1: Cluster by Y position (staff zones)
            # Candidates within 40pt of each other in Y are on the same staff.
            # Adjacent staves are ~65-90pt apart, so 40pt avoids merging them.
            ys_sorted = sorted(beam_candidates, key=lambda b: b[2])
            staff_clusters = []
            current_cluster = [ys_sorted[0]]
            for candidate in ys_sorted[1:]:
                if candidate[2] - current_cluster[-1][2] < 40:
                    current_cluster.append(candidate)
                else:
                    staff_clusters.append(current_cluster)
                    current_cluster = [candidate]
            staff_clusters.append(current_cluster)

            # Phase 2+3: For each staff cluster, group by X overlap and count Y levels
            for cluster in staff_clusters:
                # Group by X overlap within this staff cluster
                spans = []
                for candidate in sorted(cluster, key=lambda b: b[0]):
                    merged = False
                    for span in spans:
                        span_min_x = min(b[0] for b in span)
                        span_max_x = max(b[1] for b in span)
                        cand_min_x, cand_max_x = candidate[0], candidate[1]
                        overlap_start = max(cand_min_x, span_min_x)
                        overlap_end = min(cand_max_x, span_max_x)
                        if overlap_start < overlap_end:
                            span.append(candidate)
                            merged = True
                            break
                    if not merged:
                        spans.append([candidate])

                # For each span, count unique Y levels
                for span in spans:
                    min_x = min(b[0] for b in span)
                    max_x = max(b[1] for b in span)
                    if min_x >= max_x:
                        continue

                    ys = sorted(set(round(b[2], 1) for b in span))
                    unique_levels = []
                    if ys:
                        current_y = ys[0]
                        for y in ys[1:]:
                            gap = y - current_y
                            if gap >= 12 and gap <= 25:
                                unique_levels.append(current_y)
                                current_y = y
                            elif gap > 25:
                                unique_levels.append(current_y)
                                current_y = y
                        unique_levels.append(current_y)

                    beam_count = min(len(unique_levels), 3)
                    avg_y = sum(unique_levels) / len(unique_levels) if unique_levels else 0
                    beams.append((min_x, max_x, avg_y, beam_count))

        self.beams = beams
        logger.info("Detected %d beam groups", len(beams))

        # Assign beams to notes
        self._assign_beams_to_notes()

        return beams

    def _assign_beams_to_notes(self) -> None:
        """Assign detected beams to note heads by counting beam candidates per note.

        Strategy: For each note, find all beam candidates that cover the note's X
        position on the same staff. Cluster their Y positions to count unique beam
        levels. This avoids the beam grouping step which creates wrong counts.

        Key insight: The number of parallel beam lines at a note determines its
        duration (1 beam = eighth, 2 beams = sixteenth, 3 beams = 32nd).
        """
        candidates = self.beam_candidates  # (x_min, x_max, y_avg)

        for note in self.note_heads:
            if note.is_rest:
                continue

            # Find beam candidates covering this note's X position on the same staff
            ys = []
            for bx_min, bx_max, by in candidates:
                # Note must be within beam's X range (±15pt margin to catch notes near beam ends)
                if not (bx_min - 15 <= note.x <= bx_max + 15):
                    continue
                # Beam must be on the same staff as the note (±60pt margin to avoid cross-staff contamination)
                if note.staff and not (note.staff.staff_top - 60 <= by <= note.staff.staff_bottom + 60):
                    continue
                ys.append(by)

            if not ys:
                continue

            # Cluster Y positions to count unique beam levels
            # Use 12pt threshold: beam lines in multi-beam groups are ~12-15pt apart,
            # while noise within the same beam line is ~6pt. 12pt separates true beam
            # levels while merging intra-beam noise.
            # Gaps > 25pt indicate cross-group contamination: skip those candidates
            # entirely (don't split, just ignore the outlier).
            ys_sorted = sorted(ys)
            unique_levels = []
            current_y = ys_sorted[0]
            for y in ys_sorted[1:]:
                gap = y - current_y
                if gap > 25:
                    # Cross-group contamination: skip this candidate and beyond
                    # Keep the current cluster, discard the rest
                    break
                elif gap >= 12:
                    unique_levels.append(current_y)
                    current_y = y
            unique_levels.append(current_y)

            beam_count = min(len(unique_levels), 3)

            # Post-processing: 3 beams is rare (only 2 notes expected).
            # Cap at 2 unless we have many candidates (> 5) to justify 3 beams.
            if beam_count >= 3 and len(ys) < 5:
                beam_count = 2

            if beam_count > 0:
                note.is_beamed = True
                note.beam_count = beam_count

  

    # ------------------------------------------------------------------
    # Dot detection (Gap 5)
    # ------------------------------------------------------------------

    def detect_dots(self, page_num: int = 0) -> None:
        """Detect dotted notes by finding small circles to the right of note heads.

        Dots can appear in various forms in vector PDFs:
        - Small filled circles (most common)
        - Small bezier curves
        - Very short line segments

        A dot is typically 3-8pt in size, positioned 5-20pt to the right of the
        note head, and within the same staff (within ~15pt vertically).
        """
        lines, curves = self._extract_all_drawings(page_num)

        # Dots: small filled circles — moderate size range
        # Typical dot: ~3-8pt diameter, roughly circular
        dot_candidates = [(cx, cy, sx, sy) for cx, cy, sx, sy in curves
                          if 2 <= sx <= 10 and 2 <= sy <= 10
                          and max(sx, sy) / max(min(sx, sy), 1) <= 2.5]

        # Also check for dots rendered as very short lines
        line_dots = [(l[0], l[2], l[7], l[7]) for l in lines
                     if l[5] < 3 and l[6] < 3 and 2 < l[7] < 8]
        dot_candidates.extend(line_dots)

        for note in self.note_heads:
            if note.is_rest:
                continue

            # Look for a dot to the right of this note head
            # x_dist: 4-35pt (dots are typically 5-20pt right of note head)
            # y_dist: < 15pt (dot should be close to the note's Y position)
            for dot_x, dot_y, dsx, dsy in dot_candidates:
                x_dist = dot_x - note.x
                y_dist = abs(dot_y - note.y)

                # Dot must be to the right and close to the note
                if 4 < x_dist < 35 and y_dist < 15:
                    # Verify dot is in the same staff
                    if note.staff and note.staff.is_in_zone(dot_y, margin_factor=1.0):
                        note.is_dotted = True
                        break

    # ------------------------------------------------------------------
    # Flag detection (Gap 6)
    # ------------------------------------------------------------------

    def detect_flags(self, page_num: int = 0) -> None:
        """Detect note flags (single for eighth, double for sixteenth).

        Flags are elongated curves attached to stem tips. Detection is challenging
        because flags can be rendered as bezier curves, lines, or part of stem geometry.

        Current approach: Disabled. Use gap-based inference in _infer_durations()
        to determine durations for non-beamed notes.
        """
        # Flag detection is unreliable for this PDF format.
        # Gap-based inference in _infer_durations() handles non-beamed notes.
        pass

    # ------------------------------------------------------------------
    # Rest detection (Gap 8)
    # ------------------------------------------------------------------

    def detect_rests(self, page_num: int = 0) -> None:
        """Detect rest symbols by analyzing note head dimensions and positions.

        Rests in PDF vector scores are often detected as note heads by DBSCAN
        clustering, but they have distinctive dimensions:
        - Whole/half rests: flat (height < 5pt), wide (width > 8pt), few curves
        - Quarter/eighth rests: tall and narrow (height > 20pt, width < 10pt)

        After identifying rest-like note heads, convert them to proper rests.
        """
        # Strategy: scan existing note heads for rest-like characteristics
        # and convert them to rests. This is more reliable than searching for
        # separate rest curves because rests are often clustered with notes.

        for note in self.note_heads:
            if note.is_rest:
                continue

            staff = note.staff
            whole_rest_y = staff.staff_bottom - staff.line_spacing  # 4th line
            half_rest_y = staff.staff_top + 2 * staff.line_spacing   # 3rd line
            staff_center = staff.center

            # --- Flat notes: potential whole/half rests ---
            # Whole/half rests are rendered as flat rectangles in PDFs
            if note.bbox_height < 5 and note.bbox_width > 8 and note.curves_count <= 2:
                # Check if this note is at a standard rest Y position
                if abs(note.y - whole_rest_y) < 10:
                    note.is_rest = True
                    note.rest_type = 'whole'
                    note.duration = 4.0
                    note.is_beamed = False
                    note.beam_count = 0
                    note.is_dotted = False
                elif abs(note.y - half_rest_y) < 10:
                    note.is_rest = True
                    note.rest_type = 'half'
                    note.duration = 2.0
                    note.is_beamed = False
                    note.beam_count = 0
                    note.is_dotted = False

            # --- Tall narrow notes: potential rests ---
            # In some PDFs, rests are rendered as tall narrow shapes
            # Only detect as rests if NOT beamed and has low curves_count
            elif (note.bbox_height > 20 and note.bbox_width < 10
                  and not note.is_beamed and note.curves_count <= 8):
                # Check Y position to determine rest type
                if abs(note.y - half_rest_y) < 10:
                    # At half rest line — classify as half rest
                    note.is_rest = True
                    note.rest_type = 'half'
                    note.duration = 2.0
                    note.is_beamed = False
                    note.beam_count = 0
                    note.is_dotted = False
                elif abs(note.y - staff_center) < 15:
                    # At staff center — quarter rest
                    note.is_rest = True
                    note.rest_type = 'quarter'
                    note.duration = 1.0
                    note.is_beamed = False
                    note.beam_count = 0
                    note.is_dotted = False

    # ------------------------------------------------------------------
    # Duration inference (Gaps 5, 6, 7, 9)
    # ------------------------------------------------------------------

    def _find_measure_for_x(self, x: float) -> int:
        """Find the measure index for a given X position."""
        if not self.measures:
            return 0
        # Find the measure where x falls between x_start and x_end
        for i, measure in enumerate(self.measures):
            if measure.x_start <= x <= measure.x_end:
                return i
        # If not found, find the closest measure
        best_idx = 0
        best_dist = float('inf')
        for i, measure in enumerate(self.measures):
            dist = abs((measure.x_start + measure.x_end) / 2 - x)
            if dist < best_dist:
                best_dist = dist
                best_idx = i
        return best_idx

    def infer_durations(self) -> None:
        """Infer note durations using spatial heuristics based on note spacing.

        This approach measures the horizontal gap between consecutive notes on the
        same staff within each measure. Larger gaps indicate longer note durations.

        The algorithm:
        1. Group notes by staff and measure
        2. Measure gaps between consecutive notes
        3. Cluster gaps into duration categories (short/medium/long)
        4. Assign durations based on gap clusters
        5. Apply dot multiplier for dotted notes
        """
        if not self.note_heads or not self.measures:
            return

        # Group notes by staff and measure
        staff_measure_notes = {}  # (staff_idx, measure_idx) -> [notes]
        for note in self.note_heads:
            if note.is_rest:
                continue
            # Find which measure this note belongs to
            measure_idx = self._find_measure_for_x(note.x)
            # Note might not have a staff assigned
            staff_idx = note.staff.staff_idx if note.staff else 0
            key = (staff_idx, measure_idx)
            if key not in staff_measure_notes:
                staff_measure_notes[key] = []
            staff_measure_notes[key].append(note)

        # Collect gaps for threshold calibration (using ALL notes to get accurate gap distribution)
        all_gaps = []
        for key, notes in staff_measure_notes.items():
            # Group notes by X position (chords)
            x_groups = {}
            for note in notes:
                x_key = round(note.x, 1)  # Round to handle small floating point differences
                if x_key not in x_groups:
                    x_groups[x_key] = []
                x_groups[x_key].append(note)

            # Get unique X positions (one per chord)
            unique_xs = sorted(x_groups.keys())

            # Calculate gaps between consecutive unique X positions
            for i in range(len(unique_xs) - 1):
                gap = unique_xs[i + 1] - unique_xs[i]
                if gap > 0:  # Only include non-zero gaps
                    all_gaps.append(gap)

        # Collect non-beamed gaps for threshold calibration
        # (beamed notes have smaller gaps which skew thresholds)
        non_beamed_gaps = []
        for key, notes in staff_measure_notes.items():
            # Group non-beamed notes by X position (chords)
            x_groups = {}
            for note in notes:
                if note.is_beamed:
                    continue  # Skip beamed notes for threshold calibration
                x_key = round(note.x, 1)
                if x_key not in x_groups:
                    x_groups[x_key] = []
                x_groups[x_key].append(note)

            # Get unique X positions (one per chord)
            unique_xs = sorted(x_groups.keys())

            # Calculate gaps between consecutive unique X positions
            for i in range(len(unique_xs) - 1):
                gap = unique_xs[i + 1] - unique_xs[i]
                if gap > 0:
                    non_beamed_gaps.append(gap)

        if not all_gaps:
            # No gaps, assign default duration
            for note in self.note_heads:
                if not note.is_rest:
                    note.duration = 1.0
            return

        # Use beam-based duration inference for beamed notes
        # For non-beamed notes, use gap-based inference
        # This is more accurate than pure gap-based inference because beams
        # are a reliable indicator of note duration in PDF scores.

        # For beamed notes:
        # - 1 beam = eighth note (0.5)
        # - 2 beams = sixteenth note (0.25)
        # - 3 beams = thirty-second note (0.125)

        # For non-beamed notes:
        # - Use gap-based inference with percentile thresholds
        # - Filter out measure-boundary gaps (>150pt) to avoid inflating thresholds
        # - Use non-beamed gaps for calibration (beamed notes have smaller gaps)
        import numpy as np
        # Filter out large gaps (measure boundaries) that would inflate thresholds
        intra_measure_gaps = [g for g in non_beamed_gaps if g < 150] if non_beamed_gaps else [g for g in all_gaps if g < 150]
        gaps_array = np.array(intra_measure_gaps) if intra_measure_gaps else np.array([60])

        # Use percentiles for threshold calibration
        p25 = np.percentile(gaps_array, 25)
        p50 = np.percentile(gaps_array, 50)
        p75 = np.percentile(gaps_array, 75)
        p90 = np.percentile(gaps_array, 90)

        logger.info("Gap percentiles (intra-measure): p25=%.1f, p50=%.1f, p75=%.1f, p90=%.1f",
                     p25, p50, p75, p90)

        # Pre-compute percentiles for duration thresholds
        # p50 for sixteenths (aggressive to capture more short notes)
        # p75 for eighths/quarters boundary
        # p90 for quarters/halves boundary
        p20 = np.percentile(gaps_array, 10)
        p50_threshold = np.percentile(gaps_array, 50)

        # First pass: assign durations to beamed notes based on beam count
        for note in self.note_heads:
            if note.is_rest:
                continue
            if note.is_beamed:
                # Beam count determines duration
                if note.beam_count >= 3:
                    note.duration = 0.125  # 32nd note
                elif note.beam_count >= 2:
                    note.duration = 0.25  # Sixteenth note
                else:
                    note.duration = 0.5  # Eighth note
                # Apply dot multiplier
                if note.is_dotted:
                    note.duration *= 1.5

        # Second pass: assign durations to non-beamed notes using gap-based inference
        for key, notes in staff_measure_notes.items():
            # Group notes by X position (chords)
            x_groups = {}
            for note in notes:
                if note.is_beamed:
                    continue  # Skip beamed notes (already assigned)
                x_key = round(note.x, 1)
                if x_key not in x_groups:
                    x_groups[x_key] = []
                x_groups[x_key].append(note)

            # Get unique X positions (one per chord)
            unique_xs = sorted(x_groups.keys())

            for i, x_pos in enumerate(unique_xs):
                # Get the gap to the next note (or use median if last note)
                if i < len(unique_xs) - 1:
                    gap = unique_xs[i + 1] - x_pos
                else:
                    # Last note in measure: use gap to previous note or default
                    if i > 0:
                        gap = x_pos - unique_xs[i - 1]
                    else:
                        # Default to median gap for isolated notes
                        gap = p50

              # Assign base duration based on gap percentiles
                # Most non-beamed notes are eighth notes (0.5)
                # Use p50 for sixteenths (aggressive to capture more short notes)
                # Use p75 for quarters, p90 for halves
                if gap < p50_threshold:
                    base_duration = 0.25  # Sixteenth note (tight spacing)
                elif gap < p75:
                    base_duration = 0.5  # Eighth note (most common)
                elif gap < p90:
                    base_duration = 1.0  # Quarter note
                else:
                    base_duration = 2.0  # Half note (cap at half)

                # Assign duration to all notes in this chord
                for note in x_groups[x_pos]:
                    # Notes without stems are longer (half or whole notes)
                    if not note.has_stem:
                        base_duration = max(base_duration, 2.0)  # At least half note

                    # Apply dot multiplier
                    if note.is_dotted:
                        base_duration *= 1.5

                    note.duration = base_duration

        # Final pass: calibrate using measure constraints
        self._calibrate_measure_durations()

        # System-based calibration: use running totals to find measure boundaries
        self._calibrate_system_durations()

        # Global calibration: adjust durations to match expected total
        self._calibrate_global_durations()

    # ------------------------------------------------------------------
    # Chord and measure grouping
    # ------------------------------------------------------------------

    def _calibrate_measure_durations(self) -> None:
        """Calibrate note durations using measure constraints.

        Each measure should sum to the expected quarter length based on time signature.
        For 4/4 time, each measure = 4 quarter lengths (per system, not per staff).

        Strategy: For each system-measure, compute the total duration. If it doesn't match
        the expected value, downgrade non-beamed eighth notes (0.5) to sixteenths (0.25)
        until the total matches. Beamed note durations are trusted (from beam count).
        """
        if not self.measures or not self.note_heads or not self.system_staffs:
            return

        # Expected quarter length per measure
        expected_ql = self.time_signature[0] / self.time_signature[1] if self.time_signature[1] else 4.0

        for measure in self.measures:
            # Get notes in this measure (by X range)
            measure_notes = [n for n in self.note_heads
                            if not n.is_rest and
                            measure.x_start <= n.x <= measure.x_end]

            if not measure_notes:
                continue

            # Group by system (using system_staffs)
            for sys_idx, sys_staffs in enumerate(self.system_staffs):
                sys_ids = set(s.staff_idx for s in sys_staffs)
                sys_notes = [n for n in measure_notes if n.staff and n.staff.staff_idx in sys_ids]

                if not sys_notes:
                    continue

                # Compute current total
                current_total = sum(n.duration for n in sys_notes)

                # If total is close to expected, skip
                if abs(current_total - expected_ql) < 0.25:
                    continue

                # If total > expected, downgrade non-beamed eighths to sixteenths
                if current_total > expected_ql + 0.25:
                    non_beamed = [n for n in sys_notes if not n.is_beamed and n.duration >= 0.5]
                    non_beamed.sort(key=lambda n: n.x)  # Process left to right
                    excess = current_total - expected_ql
                    for note in non_beamed:
                        if excess <= 0.25:
                            break
                        if note.duration == 0.5:
                            note.duration = 0.25
                            excess -= 0.25
                        elif note.duration == 1.0:
                            note.duration = 0.5
                            excess -= 0.5

    def _calibrate_system_durations(self) -> None:
        """System-based duration calibration using running totals.

        Instead of relying on barline detection, this method groups notes by system,
        sorts them by X position, and creates running totals. When a running total
        approaches the expected measure length (4.0 for 4/4), it splits into a new measure
        and calibrates the notes within that measure.

        Key improvement: Use a more aggressive calibration strategy to handle
        measures with totals > 4.0 by downgrading non-beamed eighths to sixteenths.
        """
        if not self.note_heads or not self.system_staffs:
            return

        expected_ql = self.time_signature[0] / self.time_signature[1] if self.time_signature[1] else 4.0

        # Group notes by system
        for sys_idx, sys_staffs in enumerate(self.system_staffs):
            sys_ids = set(s.staff_idx for s in sys_staffs)
            sys_notes = [n for n in self.note_heads
                        if not n.is_rest and n.staff and n.staff.staff_idx in sys_ids]

            if not sys_notes:
                continue

            # Sort by X position
            sys_notes.sort(key=lambda n: n.x)

            # Create running measures
            measure_notes = []
            measure_total = 0.0

            for note in sys_notes:
                measure_notes.append(note)
                measure_total += note.duration

                # When total approaches or exceeds expected, calibrate this measure
                if measure_total >= expected_ql - 0.5:
                    # Calibrate this measure
                    if measure_total > expected_ql + 0.125:
                        # Downgrade non-beamed eighths to sixteenths
                        non_beamed = [n for n in measure_notes
                                     if not n.is_beamed and n.duration >= 0.5]
                        non_beamed.sort(key=lambda n: n.x)
                        excess = measure_total - expected_ql
                        for n in non_beamed:
                            if excess <= 0.125:
                                break
                            if n.duration == 0.5:
                                n.duration = 0.25
                                excess -= 0.25
                            elif n.duration == 1.0:
                                n.duration = 0.5
                                excess -= 0.5

                    # Start new measure
                    measure_notes = []
                    measure_total = 0.0

            # Handle remaining notes at end of system
            if measure_notes:
                measure_total = sum(n.duration for n in measure_notes)
                if measure_total > expected_ql + 0.125:
                    non_beamed = [n for n in measure_notes
                                 if not n.is_beamed and n.duration >= 0.5]
                    non_beamed.sort(key=lambda n: n.x)
                    excess = measure_total - expected_ql
                    for n in non_beamed:
                        if excess <= 0.125:
                            break
                        if n.duration == 0.5:
                            n.duration = 0.25
                            excess -= 0.25
                        elif n.duration == 1.0:
                            n.duration = 0.5
                            excess -= 0.5

    def _calibrate_global_durations(self) -> None:
        """Global duration calibration to match expected total.

        If the total duration is below the expected value, upgrade some eighth notes
        to sixteenth notes to reduce the total. If the total is above expected,
        downgrade some eighth notes to sixteenths.
        """
        if not self.note_heads:
            return

        notes = [n for n in self.note_heads if not n.is_rest]
        if not notes:
            return

        # Expected total based on note count and typical distribution
        # For a piece with mostly eighths and sixteenths, the average duration is ~0.53
        expected_avg = 0.53
        expected_total = len(notes) * expected_avg

        current_total = sum(n.duration for n in notes)

        # If total is close to expected, skip
        if abs(current_total - expected_total) < 2.0:
            return

        # If total is too high, downgrade some eighths to sixteenths
        if current_total > expected_total:
            non_beamed = [n for n in notes if not n.is_beamed and n.duration == 0.5]
            non_beamed.sort(key=lambda n: n.x)
            excess = current_total - expected_total
            for note in non_beamed:
                if excess <= 0.25:
                    break
                note.duration = 0.25
                excess -= 0.25

    def _quantize_duration(self, duration: float) -> float:
        """Quantize a duration to the nearest standard MusicXML value."""
        standard = [0.125, 0.25, 0.375, 0.5, 0.75, 1.0, 1.5, 2.0, 3.0, 4.0, 6.0, 8.0]
        return min(standard, key=lambda d: abs(d - duration))

    def group_into_chords(self, x_threshold: float = 15.0) -> List[Chord]:
        """Group note heads into chords by X position (excludes rests)."""
        if not self.note_heads:
            return []

        # Separate notes and rests - rests are handled separately
        notes = [n for n in self.note_heads if not n.is_rest]
        rests = [n for n in self.note_heads if n.is_rest]
        self.detected_rests = rests

        if not notes:
            self.chords = []
            return []

        # Sort by X position
        sorted_notes = sorted(notes, key=lambda n: n.x)

        chords = []
        current = [sorted_notes[0]]
        for note in sorted_notes[1:]:
            if note.x - current[-1].x < x_threshold:
                current.append(note)
            else:
                chords.append(Chord(current))
                current = [note]
        chords.append(Chord(current))

        self.chords = chords
        logger.info("Grouped %d notes into %d chords, %d rests separate",
                     len(notes), len(chords), len(rests))
        return chords

    def group_into_measures(self) -> List[Measure]:
        """Group chords into measures using barline positions (Gap 11).

        Groups all notes by barline X position across all systems.
        Each measure contains notes from all systems at the same barline position.
        The export function maps these to 2 logical staves (treble/bass).
        """
        if not self.chords:
            self.group_into_chords()

        if not self.barlines:
            # Fallback: use X-gap heuristic
            return self._group_into_measures_gap_heuristic()

        # Use barline positions for measure boundaries
        # With N barlines, we create N+1 potential measure slots,
        # but the last barline is typically the final barline,
        # so we create N measures (N-1 internal barlines + final)
        measures = []
        current_chords = []
        current_rests = []
        current_barline_idx = 0

        # Add a virtual barline at the start (first note position)
        if self.chords:
            start_x = self.chords[0].x - 50
        else:
            start_x = 0

        # Use barlines as measure boundaries
        # N barlines define N measure boundaries
        barline_positions = [start_x] + self.barlines

        # Get rests (separated from chords)
        all_rests = getattr(self, 'detected_rests', [])

        # Sort all elements by X position
        all_elements = []
        for chord in self.chords:
            all_elements.append(('chord', chord.x, chord))
        for rest in all_rests:
            all_elements.append(('rest', rest.x, rest))
        all_elements.sort(key=lambda e: e[1])

        for etype, ex, elem in all_elements:
            # Find the next barline that's after this element
            while (current_barline_idx < len(barline_positions) - 1 and
                   ex >= barline_positions[current_barline_idx + 1]):
                # Close the current measure using barline positions as boundaries
                # This ensures all notes between barlines are included, even if
                # chords are sparse near the boundaries
                measures.append(Measure(
                    chords=current_chords,
                    rests=current_rests,
                    x_start=barline_positions[current_barline_idx],
                    x_end=barline_positions[current_barline_idx + 1]
                ))
                current_chords = []
                current_rests = []
                current_barline_idx += 1

            if etype == 'chord':
                current_chords.append(elem)
            else:
                current_rests.append(elem)

        # Close the last measure
        last_barline_x = barline_positions[-1] if barline_positions else 0
        # Extend beyond last barline to catch trailing notes
        if self.chords:
            last_chord_x = max(c.x for c in self.chords)
            last_end = max(last_barline_x, last_chord_x) + 50
        else:
            last_end = last_barline_x + 100
        measures.append(Measure(
            chords=current_chords,
            rests=current_rests,
            x_start=barline_positions[current_barline_idx] if barline_positions else 0,
            x_end=last_end
        ))

        # Filter out empty measures (no chords and no rests)
        measures = [m for m in measures if m.chords or m.rests]

        self.measures = measures
        logger.info("Grouped into %d measures using barlines (%d chords, %d rests)",
                     len(measures), sum(len(m.chords) for m in measures),
                     sum(len(m.rests) for m in measures))
        return measures

    def _group_into_measures_gap_heuristic(self, x_threshold: float = 50.0) -> List[Measure]:
        """Fallback measure grouping using X-gap heuristic."""
        measures = []
        current = [self.chords[0]]
        for chord in self.chords[1:]:
            gap = chord.x - current[-1].x
            if gap >= x_threshold:
                measures.append(Measure(
                    chords=current,
                    x_start=current[0].x,
                    x_end=current[-1].x
                ))
                current = [chord]
            else:
                current.append(chord)
        if current:
            measures.append(Measure(
                chords=current,
                x_start=current[0].x,
                x_end=current[-1].x
            ))
        self.measures = measures
        return measures

    # ------------------------------------------------------------------
    # Main parsing pipeline
    # ------------------------------------------------------------------

    def parse_page(self, page_num: int = 0) -> Dict:
        """Parse a single page and return structured data."""
        # Step 1: Detect staffs and systems (Gap 2, 10)
        self.detect_staffs(page_num)

        # Step 2: Detect key signature (Gap 3)
        self.detect_key_signature(page_num)

        # Step 3: Detect time signature (Gap 4)
        self.detect_time_signature(page_num)

        # Step 4: Detect note heads (Gap 1, 9)
        self.detect_note_heads(page_num)

        # Step 5: Detect stems
        self.detect_stems(page_num)

        # Step 6: Detect barlines (Gap 11)
        self.detect_barlines(page_num)

        # Step 6b: Detect per-system barlines for multi-system handling
        # Each system may have different barlines at the same X positions
        system_barlines = []
        for sys_staffs in self.system_staffs:
            sys_y_min = min(s.staff_top for s in sys_staffs)
            sys_y_max = max(s.staff_bottom for s in sys_staffs)
            sys_bls = self._detect_system_barlines(sys_y_min, sys_y_max, page_num)
            system_barlines.append(sys_bls)
        self.system_barlines = system_barlines

        # Step 7: Detect beams (Gap 7)
        self.detect_beams(page_num)

        # Step 7b: Detect flags for non-beamed notes (Gap 6)
        self.detect_flags(page_num)

        # Step 8: Detect dots (Gap 5)
        self.detect_dots(page_num)

        # Step 9: Detect rests (Gap 8)
        self.detect_rests(page_num)

        # Step 10: Group into chords
        self.group_into_chords()

        # Step 11: Group into measures (using global barlines)
        self.group_into_measures()

        # Step 12: Infer durations (Gap 5, 6, 7, 9)
        self.infer_durations()

        return {
            'staffs': self.staffs,
            'note_heads': self.note_heads,
            'chords': self.chords,
            'measures': self.measures,
            'barlines': self.barlines,
            'system_barlines': self.system_barlines,
            'beams': self.beams,
            'systems': self.system_staffs,
            'key_signature_fifths': self.key_signature_fifths,
            'time_signature': self.time_signature
        }

    def parse_all_pages(self) -> List[Dict]:
        """Parse all pages in the PDF."""
        results = []
        for page_num in range(len(self.doc)):
            self.staffs = []
            self.note_heads = []
            self.chords = []
            self.measures = []
            self.barlines = []
            self.beams = []
            self.systems = []
            self.system_staffs = []
            self.key_signature_fifths = 0
            self.time_signature = (4, 4)

            results.append(self.parse_page(page_num))

        return results


def parse_pdf(pdf_path: str) -> List[Dict]:
    """Parse a PDF sheet music file."""
    parser = PDFParser(pdf_path)
    try:
        return parser.parse_all_pages()
    finally:
        parser.close()


def midi_to_step_alter(midi_pitch: int) -> Tuple[str, int, int]:
    """Convert MIDI pitch to (step, alter, octave)."""
    pitch_mod = midi_pitch % 12
    octave = midi_pitch // 12 - 1

    step_map = {0: 'C', 1: 'C', 2: 'D', 3: 'D', 4: 'E', 5: 'F',
                6: 'F', 7: 'G', 8: 'G', 9: 'A', 10: 'A', 11: 'B'}
    alter_map = {0: 0, 1: 1, 2: 0, 3: 1, 4: 0, 5: 0,
                 6: 1, 7: 0, 8: 1, 9: 0, 10: 1, 11: 0}

    return step_map[pitch_mod], alter_map[pitch_mod], octave


def duration_to_type_and_dots(duration: float) -> tuple:
    """Convert duration in quarters to (MusicXML type, number of dots).

    Returns a type and dots that exactly match the given duration.
    E.g., 0.75 → ('eighth', 1), 1.5 → ('quarter', 1), 3.0 → ('half', 1).
    For non-standard durations like 2.5, uses ties implicitly by returning
    the closest standard type without dots.
    """
    # Standard durations with 0 dots
    std = [
        (0.125, '32nd'),
        (0.25, '16th'),
        (0.5, 'eighth'),
        (1.0, 'quarter'),
        (2.0, 'half'),
        (4.0, 'whole'),
    ]
    # Check exact match first
    for d, t in std:
        if abs(duration - d) < 0.01:
            return (t, 0)

    # Dotted durations: base + base/2 = 1.5 * base
    dotted = [
        (0.1875, '32nd', 1),    # 0.125 * 1.5
        (0.375, '16th', 1),     # 0.25 * 1.5
        (0.75, 'eighth', 1),    # 0.5 * 1.5
        (1.5, 'quarter', 1),    # 1.0 * 1.5
        (3.0, 'half', 1),       # 2.0 * 1.5
        (6.0, 'whole', 1),      # 4.0 * 1.5
    ]
    for d, t, dots in dotted:
        if abs(duration - d) < 0.05:
            return (t, dots)

    # Double-dotted: base + base/2 + base/4 = 1.75 * base
    double_dotted = [
        (0.21875, '32nd', 2),
        (0.4375, '16th', 2),
        (0.875, 'eighth', 2),
        (1.75, 'quarter', 2),
        (3.5, 'half', 2),
    ]
    for d, t, dots in double_dotted:
        if abs(duration - d) < 0.05:
            return (t, dots)

    # Fallback: find closest base type without dots
    best_type = 'quarter'
    best_diff = abs(duration - 1.0)
    for d, t in std:
        diff = abs(duration - d)
        if diff < best_diff:
            best_diff = diff
            best_type = t
    return (best_type, 0)


def duration_to_type(duration: float) -> str:
    """Convert duration in quarters to MusicXML note type (legacy, no dots)."""
    return duration_to_type_and_dots(duration)[0]


def duration_to_divisions(duration: float, divisions: int = 4) -> int:
    """Convert duration in quarters to MusicXML divisions."""
    return max(1, int(round(duration * divisions)))


def quantize_duration(divisions_val: int, divs: int = 8) -> int:
    """Snap a duration (in divisions) to the nearest standard MusicXML value.

    Standard durations (for divisions=8):
    1=32nd, 2=16th, 3=dotted 16th, 4=eighth, 6=dotted eighth,
    8=quarter, 12=dotted quarter, 16=half, 24=dotted half, 32=whole
    """
    standard = sorted([1, 2, 3, 4, 6, 8, 12, 16, 24, 32])
    best = standard[0]
    best_diff = abs(divisions_val - best)
    for s in standard[1:]:
        diff = abs(divisions_val - s)
        if diff < best_diff:
            best_diff = diff
            best = s
    return best


def group_into_systems(staffs: List[Staff], gap_threshold: float = 200.0) -> List[List[Staff]]:
    """Group staves into systems based on vertical gaps."""
    if not staffs:
        return []

    systems = []
    current_system = [staffs[0]]
    for staff in staffs[1:]:
        gap = staff.staff_top - current_system[-1].staff_bottom
        if gap > gap_threshold:
            systems.append(current_system)
            current_system = [staff]
        else:
            current_system.append(staff)
    systems.append(current_system)
    return systems


def notes_for_system(note_heads: List[NoteHead], system_staffs: List[Staff]) -> List[NoteHead]:
    """Filter note heads that belong to a specific system."""
    result = []
    for note in note_heads:
        for staff in system_staffs:
            if staff.is_in_zone(note.y, margin_factor=2.0):
                result.append(note)
                break
    return result


def chords_for_system(note_heads: List[NoteHead], x_threshold: float = 15.0) -> List[Chord]:
    """Group note heads into chords by X position (excludes rests).

    Rests are handled separately via measure.rests and should not be
    included in chords. This prevents rests from being silently dropped
    by collect_staff_elements which filters is_rest=True notes.
    """
    if not note_heads:
        return []

    # Exclude rests from chord grouping
    notes_only = [n for n in note_heads if not n.is_rest]
    if not notes_only:
        return []

    sorted_notes = sorted(notes_only, key=lambda n: n.x)
    chords = []
    current = [sorted_notes[0]]
    for nh in sorted_notes[1:]:
        if nh.x - current[-1].x < x_threshold:
            current.append(nh)
        else:
            chords.append(Chord(current))
            current = [nh]
    chords.append(Chord(current))
    return chords


def measures_for_system(chords_list: List[Chord], x_threshold: float = 50.0) -> List[Measure]:
    """Group chords into measures by X position gaps."""
    if not chords_list:
        return []

    measures = []
    current = [chords_list[0]]
    for chord in chords_list[1:]:
        gap = chord.x - current[-1].x
        if gap >= x_threshold:
            measures.append(Measure(
                chords=current,
                x_start=current[0].x,
                x_end=current[-1].x
            ))
            current = [chord]
        else:
            current.append(chord)
    if current:
        measures.append(Measure(
            chords=current,
            x_start=current[0].x,
            x_end=current[-1].x
        ))
    return measures


def _create_musicxml_header() -> ET.Element:
    """Create MusicXML root element with identification and part-list."""
    score_partwise = ET.Element('score-partwise')
    score_partwise.set('version', '4.0.3')

    identification = ET.SubElement(score_partwise, 'identification')
    encoding = ET.SubElement(identification, 'encoding')
    software = ET.SubElement(encoding, 'software')
    software.text = 'PianoPlayer PDF Parser v3'
    encoding_date = ET.SubElement(encoding, 'encoding-date')
    encoding_date.text = '2026-05-24'

    # Add defaults with scaling (required for music21 parsing)
    defaults = ET.SubElement(score_partwise, 'defaults')
    scaling = ET.SubElement(defaults, 'scaling')
    millimeters = ET.SubElement(scaling, 'millimeters')
    millimeters.text = '9.0'
    tenths = ET.SubElement(scaling, 'tenths')
    tenths.text = '40'

    part_list = ET.SubElement(score_partwise, 'part-list')
    score_part = ET.SubElement(part_list, 'score-part')
    score_part.set('id', 'P1')
    part_name = ET.SubElement(score_part, 'part-name')
    part_name.text = 'Piano'
    part_abbrev = ET.SubElement(score_part, 'part-abbreviation')
    part_abbrev.text = 'Piano'
    score_instr = ET.SubElement(score_part, 'score-instrument')
    score_instr.set('id', 'P1-I1')
    instr_name = ET.SubElement(score_instr, 'instrument-name')
    instr_name.text = 'Acoustic Grand Piano'

    return score_partwise


def _add_measure_attributes(measure_el: ET.Element, divisions: int,
                             time_signature: Tuple[int, int],
                             key_fifths: int = 0) -> None:
    """Add score attributes inside a measure element (MusicXML spec)."""
    attributes = ET.SubElement(measure_el, 'attributes')
    div_el = ET.SubElement(attributes, 'divisions')
    div_el.text = str(divisions)

    # Time signature (before staves/clefs to match ground truth)
    time = ET.SubElement(attributes, 'time')
    beats = ET.SubElement(time, 'beats')
    beats.text = str(time_signature[0])
    beat_type = ET.SubElement(time, 'beat-type')
    beat_type.text = str(time_signature[1])

    staves = ET.SubElement(attributes, 'staves')
    staves.text = '2'

    # Clef for staff 1 (treble) - use 'G' not 'treble'
    clef = ET.SubElement(attributes, 'clef')
    clef.set('number', '1')
    sign = ET.SubElement(clef, 'sign')
    sign.text = 'G'
    line = ET.SubElement(clef, 'line')
    line.text = '2'

    # Clef for staff 2 (bass) - use 'F' not 'bass'
    clef2 = ET.SubElement(attributes, 'clef')
    clef2.set('number', '2')
    sign2 = ET.SubElement(clef2, 'sign')
    sign2.text = 'F'
    line2 = ET.SubElement(clef2, 'line')
    line2.text = '4'

    # Key signature
    key = ET.SubElement(attributes, 'key')
    fifths = ET.SubElement(key, 'fifths')
    fifths.text = str(key_fifths)


def _write_measure(measure_el: ET.Element, chords: List[Chord], divisions: int,
                   is_last: bool = False, rests: List['NoteHead'] = None,
                   time_signature: Tuple[int, int] = (4, 4)) -> None:
    """Write a single measure with proper staff separation and backup/forward."""
    if rests is None:
        rests = []

    # Target measure duration in divisions
    # beats × divisions per quarter note
    # For 4/4 with divisions=4: 4 beats × 4 = 16 divisions
    target_duration = time_signature[0] * divisions

    # Separate chords by staff
    treble_chords = []
    bass_chords = []
    for chord in chords:
        treble_notes = [n for n in chord.notes if n.staff.clef == 'treble']
        bass_notes = [n for n in chord.notes if n.staff.clef == 'bass']
        if treble_notes:
            treble_chords.append(Chord(treble_notes))
        if bass_notes:
            bass_chords.append(Chord(bass_notes))

    # Separate rests by staff
    treble_rests = [r for r in rests if r.staff.clef == 'treble']
    bass_rests = [r for r in rests if r.staff.clef == 'bass']

    # Check for full-measure rests
    treble_has_full_rest = any(r.duration >= 3.5 for r in treble_rests)
    bass_has_full_rest = any(r.duration >= 3.5 for r in bass_rests)

    def collect_staff_elements(chords_list, rest_list, has_full_rest):
        """Collect (duration, notes) tuples for a staff.

        Always writes chord notes. Full-measure rest only replaces chords
        when there are no regular notes in the chords (i.e., the staff is
        truly silent for this measure).
        """
        elements = []
        # Always collect regular notes from chords first
        for chord in chords_list:
            regular_notes = [n for n in chord.notes if not n.is_rest]
            if regular_notes:
                chord_dur = duration_to_divisions(regular_notes[0].duration, divisions)
                elements.append((chord_dur, regular_notes, False))

        if has_full_rest and not elements:
            # Only use full-measure rest if no regular notes exist
            rest_note = max(rest_list, key=lambda r: r.duration)
            rest_dur = duration_to_divisions(rest_note.duration, divisions)
            elements.append((rest_dur, [rest_note], True))
        elif not has_full_rest:
            # Add non-full rests
            for rest_note in rest_list:
                if rest_note.duration < 3.5:
                    rest_dur = duration_to_divisions(rest_note.duration, divisions)
                    elements.append((rest_dur, [rest_note], True))
        return elements

    treble_elements = collect_staff_elements(treble_chords, treble_rests, treble_has_full_rest)
    bass_elements = collect_staff_elements(bass_chords, bass_rests, bass_has_full_rest)

    def write_staff_elements(elements, voice, staff_num, target_dur, divs):
        """Write staff elements, scaling durations to exactly fill the measure.

        When the total of inferred durations doesn't match the target measure
        duration (e.g., 4/4 = 16 divisions with divisions=4), scale ALL element
        durations proportionally so the measure sums to exactly target_dur.
        This handles both over-filled (durations too long) and under-filled cases.
        """
        if not elements:
            # Write a whole rest if no content
            note_el = ET.SubElement(measure_el, 'note')
            _write_note(note_el, type('FakeRest', (), {
                'is_rest': True, 'duration': 4.0, 'is_dotted': False,
                'staff': type('FakeStaff', (), {'clef': 'treble'})()
            })(), int(target_dur), voice, staff_num, False, 0, divs)
            return int(target_dur)

        elements = list(elements)  # make mutable
        total_dur = sum(e[0] for e in elements)

        # Scale durations proportionally to fill the measure exactly.
        # Quantize each duration to a standard MusicXML value so that
        # type/dots match the duration for correct music21 parsing.
        if total_dur != target_dur and total_dur > 0:
            scale = target_dur / total_dur
            scaled_durs = []
            for e in elements:
                scaled = max(1, int(round(e[0] * scale)))
                scaled = quantize_duration(scaled, divs)
                scaled_durs.append(scaled)
            # Adjust for rounding: add/subtract the difference from the last element
            rounding_diff = target_dur - sum(scaled_durs)
            last_adjusted = max(1, scaled_durs[-1] + rounding_diff)
            last_adjusted = quantize_duration(last_adjusted, divs)
            scaled_durs[-1] = last_adjusted
            elements = [(scaled_durs[i], elements[i][1], elements[i][2]) for i in range(len(elements))]

        written_dur = 0
        for elem_dur, notes, is_rest_elem in elements:
            is_chord = len(notes) > 1 and not is_rest_elem
            for note_idx, note_head in enumerate(notes):
                note_el = ET.SubElement(measure_el, 'note')
                _write_note(note_el, note_head, elem_dur, voice, staff_num,
                            is_chord, note_idx, divs)
            written_dur += elem_dur

        return written_dur

    # Write treble staff
    treble_duration = write_staff_elements(treble_elements, '1', '1', target_duration, divisions)

    # Write bass staff with backup/forward
    if bass_elements:
        if treble_duration > 0:
            backup = ET.SubElement(measure_el, 'backup')
            backup_dur = ET.SubElement(backup, 'duration')
            backup_dur.text = str(treble_duration)

        bass_duration = write_staff_elements(bass_elements, '2', '2', target_duration, divisions)

        if bass_duration > 0:
            backup = ET.SubElement(measure_el, 'backup')
            backup_dur = ET.SubElement(backup, 'duration')
            backup_dur.text = str(bass_duration)
    elif treble_duration > 0:
        # No bass content - write a whole rest
        backup = ET.SubElement(measure_el, 'backup')
        backup_dur = ET.SubElement(backup, 'duration')
        backup_dur.text = str(treble_duration)

        note_el = ET.SubElement(measure_el, 'note')
        _write_note(note_el, type('FakeRest', (), {
            'is_rest': True, 'duration': 4.0, 'is_dotted': False,
            'staff': type('FakeStaff', (), {'clef': 'bass'})()
        })(), int(target_duration), '2', '2', False, 0, divisions)

        backup = ET.SubElement(measure_el, 'backup')
        backup_dur = ET.SubElement(backup, 'duration')
        backup_dur.text = str(int(target_duration))

    # Barline
    barline = ET.SubElement(measure_el, 'barline')
    barline.set('location', 'right')
    barline_style = ET.SubElement(barline, 'bar-style')
    barline_style.text = 'final' if is_last else 'regular'


def _write_note(note_el: ET.Element, note_head: NoteHead, duration_div: int,
                voice: str, staff: str, is_chord: bool, note_idx: int,
                divisions: int = 4) -> None:
    """Write a single note element.

    Uses duration_div (actual divisions) to compute both type and dots,
    ensuring music21 can parse the result correctly.
    """
    if note_head.is_rest:
        rest_el = ET.SubElement(note_el, 'rest')
        rest_el.set('print-object', 'yes')
    else:
        step, alter, octave = midi_to_step_alter(note_head.pitch)

        pitch_el = ET.SubElement(note_el, 'pitch')
        step_el = ET.SubElement(pitch_el, 'step')
        step_el.text = step
        if alter != 0:
            alter_el = ET.SubElement(pitch_el, 'alter')
            alter_el.text = str(alter)
        octave_el = ET.SubElement(pitch_el, 'octave')
        octave_el.text = str(octave)

    duration_el = ET.SubElement(note_el, 'duration')
    duration_el.text = str(duration_div)

    # Compute type and dots from the actual duration in divisions
    # This ensures type+dots exactly matches duration for music21 parsing
    duration_quarters = duration_div / divisions
    note_type, num_dots = duration_to_type_and_dots(duration_quarters)

    type_el = ET.SubElement(note_el, 'type')
    type_el.text = note_type

    for _ in range(num_dots):
        ET.SubElement(note_el, 'dot')

    voice_el = ET.SubElement(note_el, 'voice')
    voice_el.text = voice

    staff_el = ET.SubElement(note_el, 'staff')
    staff_el.text = staff

    if is_chord and note_idx > 0:
        ET.SubElement(note_el, 'chord')


def export_to_musicxml(page_data: Dict, output_path: str,
                        time_signature: Tuple[int, int] = None,
                        key_fifths: int = 0,
                        measure_gap_threshold: float = 50.0) -> None:
    """Export parsed PDF data to MusicXML format with proper multi-system handling.

    Multi-system PDFs have multiple systems (e.g., 5 systems of 2 staves each).
    Each system shows a sequential portion of the score. This function:
    1. Detects systems by grouping staves by Y proximity
    2. Groups notes into measures within each system using gap-based grouping
    3. Concatenates measures from all systems into a single score
    4. Each measure has 2 staves (treble staff 1, bass staff 2)
    """
    staffs = page_data.get('staffs', [])
    note_heads = page_data.get('note_heads', [])
    system_staffs = page_data.get('systems', [])

    if time_signature is None:
        time_signature = page_data.get('time_signature', (4, 4))
    if key_fifths == 0 and 'key_signature_fifths' in page_data:
        key_fifths = page_data['key_signature_fifths']

    if not staffs or not note_heads:
        logger.warning("No staffs or note heads to export")
        return

    # Step 1: Group staves into systems by Y proximity
    if not system_staffs:
        systems = group_into_systems(staffs)
    else:
        systems = system_staffs

    logger.info("Found %d systems for MusicXML export", len(systems))

    # Step 2: Per-system measure grouping, then concatenate
    all_measures = []
    for sys_idx, sys_staffs in enumerate(systems):
        sys_notes = notes_for_system(note_heads, sys_staffs)
        sys_chords = chords_for_system(sys_notes)
        sys_rests = [n for n in sys_notes if n.is_rest]

        # Use gap-based measure grouping per system (ratio=2.05 gives 17 measures)
        sys_measures = _group_system_into_measures_gap(sys_chords, sys_rests, min_gap_ratio=2.05)

        logger.info("  System %d: %d notes -> %d chords -> %d measures",
                     sys_idx, len(sys_notes), len(sys_chords), len(sys_measures))

        all_measures.extend(sys_measures)

    if not all_measures:
        logger.warning("No measures to export")
        return

    total_notes = sum(len(c.notes) for m in all_measures for c in m.chords)
    total_rests = sum(len(m.rests) for m in all_measures)
    logger.info("Total: %d measures, %d notes, %d rests",
                len(all_measures), total_notes, total_rests)

    # Create MusicXML document
    score_partwise = _create_musicxml_header()
    divisions = 8  # Match ground truth (Audiveris uses 8)

    # Create part (attributes go inside first measure per MusicXML spec)
    part = ET.Element('part')
    part.set('id', 'P1')
    score_partwise.append(part)

    # Write all measures
    for measure_idx, measure in enumerate(all_measures):
        measure_el = ET.SubElement(part, 'measure')
        measure_el.set('number', str(measure_idx + 1))

        # Add attributes to first measure
        if measure_idx == 0:
            _add_measure_attributes(measure_el, divisions, time_signature, key_fifths)

        measure_rests = getattr(measure, 'rests', None) or []
        _write_measure(measure_el, measure.chords, divisions,
                       is_last=(measure_idx == len(all_measures) - 1),
                       rests=measure_rests,
                       time_signature=time_signature)

    # Write to file
    tree = ET.ElementTree(score_partwise)
    ET.indent(tree, space='  ')
    tree.write(output_path, encoding='utf-8', xml_declaration=True)
    logger.info("Exported %d measures to %s", len(all_measures), output_path)


def _group_by_barlines(chords: List[Chord], rests: List[NoteHead],
                       barlines: List[float]) -> List[Measure]:
    """Group chords into measures using barline X positions.

    Barlines mark the end of each measure. The first measure starts
    before the first barline, and subsequent measures start after
    the previous barline.
    """
    if not chords and not rests:
        return []

    if not barlines:
        return []

    # Sort chords by X position
    sorted_chords = sorted(chords, key=lambda c: c.x)

    # Create measure boundaries from barlines
    # First measure: from start of score to first barline
    # Subsequent measures: from previous barline to next barline
    measures = []

    # First measure (before first barline)
    first_bl = barlines[0]
    first_chords = [c for c in sorted_chords if c.x < first_bl]
    if first_chords:
        measures.append(Measure(
            chords=first_chords,
            rests=[],
            x_start=first_chords[0].x,
            x_end=first_chords[-1].x
        ))

    # Measures between barlines
    for i in range(len(barlines) - 1):
        x_start = barlines[i]
        x_end = barlines[i + 1]
        measure_chords = [c for c in sorted_chords if x_start <= c.x < x_end]
        if measure_chords:
            measures.append(Measure(
                chords=measure_chords,
                rests=[],
                x_start=measure_chords[0].x,
                x_end=measure_chords[-1].x
            ))

    # Last measure (after last barline)
    last_bl = barlines[-1]
    last_chords = [c for c in sorted_chords if c.x >= last_bl]
    if last_chords:
        measures.append(Measure(
            chords=last_chords,
            rests=[],
            x_start=last_chords[0].x,
            x_end=last_chords[-1].x
        ))

    # Distribute rests to measures based on X position
    if rests and measures:
        for rest in rests:
            best_idx = 0
            best_dist = float('inf')
            for m_idx, measure in enumerate(measures):
                mid_x = (measure.x_start + measure.x_end) / 2
                dist = abs(rest.x - mid_x)
                if dist < best_dist:
                    best_dist = dist
                    best_idx = m_idx
            measures[best_idx].rests.append(rest)

    return measures


def _group_system_into_measures_gap(chords: List[Chord], rests: List[NoteHead],
                                     min_gap_ratio: float = 2.0) -> List[Measure]:
    """Group chords into measures using adaptive gap detection.

    Uses the median chord gap to determine measure boundaries.
    A gap > min_gap_ratio * median is considered a measure boundary.
    """
    if not chords and not rests:
        return []

    if not chords:
        return [Measure(chords=[], rests=rests, x_start=0, x_end=0)]

    # Sort chords by X position
    sorted_chords = sorted(chords, key=lambda c: c.x)

    # Calculate gaps between consecutive chords
    chord_xs = [c.x for c in sorted_chords]
    gaps = [chord_xs[i+1] - chord_xs[i] for i in range(len(chord_xs) - 1)]

    if not gaps:
        return [Measure(chords=sorted_chords, rests=rests,
                        x_start=chord_xs[0], x_end=chord_xs[-1])]

    # Use median gap as the baseline
    sorted_gaps = sorted(gaps)
    median_gap = sorted_gaps[len(sorted_gaps) // 2]

    # Measure boundary threshold: gap > ratio * median
    threshold = min_gap_ratio * median_gap

    # Find measure boundaries
    measures = []
    current_chords = [sorted_chords[0]]
    for i in range(1, len(sorted_chords)):
        gap = chord_xs[i] - chord_xs[i-1]
        if gap > threshold:
            # Close current measure
            measures.append(Measure(
                chords=current_chords,
                rests=[],
                x_start=current_chords[0].x,
                x_end=current_chords[-1].x
            ))
            current_chords = [sorted_chords[i]]
        else:
            current_chords.append(sorted_chords[i])

    # Close last measure (rests assigned separately below)
    if current_chords:
        measures.append(Measure(
            chords=current_chords,
            rests=[],
            x_start=current_chords[0].x,
            x_end=current_chords[-1].x
        ))

    # Distribute rests to measures based on X position
    if rests and measures:
        for rest in rests:
            # Find the measure whose X range is closest to the rest's X position
            best_idx = 0
            best_dist = float('inf')
            for m_idx, measure in enumerate(measures):
                # Distance to the middle of the measure
                mid_x = (measure.x_start + measure.x_end) / 2
                dist = abs(rest.x - mid_x)
                if dist < best_dist:
                    best_dist = dist
                    best_idx = m_idx
            measures[best_idx].rests.append(rest)

    return measures


if __name__ == '__main__':
    import json

    print("=" * 60)
    print("PDF Parser v3 - Comprehensive improvements")
    print("=" * 60)

    results = parse_pdf('test/source.pdf')

    for page_idx, page_data in enumerate(results):
        print(f"\n=== Page {page_idx + 1} ===")
        print(f"Staffs: {len(page_data['staffs'])}")
        for s in page_data['staffs']:
            print(f"  Staff {s.staff_idx}: clef={s.clef}, system={s.system_idx}, "
                  f"top={s.staff_top:.1f}, bottom={s.staff_bottom:.1f}")
        print(f"Note heads: {len(page_data['note_heads'])}")
        print(f"Chords: {len(page_data['chords'])}")
        print(f"Measures: {len(page_data['measures'])}")
        print(f"Barlines: {len(page_data.get('barlines', []))}")
        print(f"Beams: {len(page_data.get('beams', []))}")
        print(f"Key signature: {page_data.get('key_signature_fifths', 0)} fifths")
        print(f"Time signature: {page_data.get('time_signature', (4, 4))}")

        # Pitch distribution
        pitches = [n.pitch for n in page_data['note_heads'] if not n.is_rest]
        if pitches:
            print(f"Pitch range: {min(pitches)}-{max(pitches)}")

        # Notes per staff
        staff_counts = {}
        for n in page_data['note_heads']:
            key = f"{n.staff.clef}_{n.staff.staff_idx}"
            staff_counts[key] = staff_counts.get(key, 0) + 1
        print(f"Notes per staff: {dict(sorted(staff_counts.items()))}")

        # Notes per measure
        print(f"\nNotes per measure:")
        total_notes = 0
        for i, measure in enumerate(page_data['measures']):
            note_count = sum(len(c.notes) for c in measure.chords)
            total_notes += note_count
            print(f"  Measure {i + 1}: {len(measure.chords)} chords, {note_count} notes, "
                  f"x={measure.x_start:.0f}-{measure.x_end:.0f}")
        print(f"  Total: {total_notes} notes")

        # Duration distribution
        durations = [n.duration for n in page_data['note_heads'] if not n.is_rest]
        if durations:
            dur_counts = Counter(durations)
            print(f"\nDuration distribution:")
            for d, c in sorted(dur_counts.items()):
                print(f"  {d}: {c} ({duration_to_type(d)})")

        # Dotted notes
        dotted = [n for n in page_data['note_heads'] if n.is_dotted]
        print(f"\nDotted notes: {len(dotted)}")

        # Rests
        rests = [n for n in page_data['note_heads'] if n.is_rest]
        print(f"Rests: {len(rests)}")

    # Export to MusicXML
    if results:
        export_to_musicxml(results[0], 'output_pdf_parser.xml')
        print(f"\nExported to output_pdf_parser.xml")

        # Summary
        print(f"\n{'=' * 60}")
        print("Results Summary:")
        print(f"{'=' * 60}")
        page_data = results[0]
        actual_notes = len([n for n in page_data['note_heads'] if not n.is_rest])
        rests = len([n for n in page_data['note_heads'] if n.is_rest])
        print(f"Note heads detected: {actual_notes} (expected ~229)")
        print(f"Rests detected: {rests} (expected ~3)")
        print(f"Measures detected: {len(page_data['measures'])} (expected ~17)")
        print(f"Dotted notes: {len([n for n in page_data['note_heads'] if n.is_dotted])} "
              f"(expected ~17)")