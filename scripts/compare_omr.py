#!/usr/bin/env python3
"""Compare OMR engine outputs on the same PDF input.

Runs both the built-in parser and Audiveris (if available) on a PDF and
reports note count, measure count, and pitch-level comparison.

Usage:
    python scripts/compare_omr.py [pdf_path]
    python scripts/compare_omr.py test/source.pdf
"""

import os
import sys
import tempfile
import xml.etree.ElementTree as ET

NS = "{http://www.w3.org/2001/XMLSchema-instance}"  # not used, placeholder

def extract_musicxml_stats(xml_path: str) -> dict:
    """Extract note count, measure count, and pitch list from MusicXML."""
    tree = ET.parse(xml_path)
    root = tree.getroot()

    # Detect namespace
    ns = ""
    if root.tag.startswith("{"):
        ns = root.tag.split("}")[0] + "}"

    measures = root.findall(f".//{ns}measure") or root.findall(".//measure")
    notes = root.findall(f".//{ns}note") or root.findall(".//note")

    # Extract pitches (step + octave) for comparison
    pitches = []
    pitches_with_octave = []
    for note in notes:
        pitch_el = note.find(f"{ns}pitch") if ns else note.find("pitch")
        if pitch_el is not None:
            step = pitch_el.find(f"{ns}step") if ns else pitch_el.find("step")
            octave = pitch_el.find(f"{ns}octave") if ns else pitch_el.find("octave")
            if step is not None and octave is not None:
                pitches.append(step.text)
                pitches_with_octave.append(f"{step.text}{octave.text}")

    # Pitch class distribution
    from collections import Counter
    pitch_dist = Counter(pitches)

    return {
        "note_count": len(notes),
        "measure_count": len(measures),
        "pitches": pitches,
        "pitches_with_octave": pitches_with_octave,
        "pitch_distribution": dict(pitch_dist.most_common()),
    }


def compare_pitch_lists(pitches_a: list, pitches_b: list) -> dict:
    """Compare two pitch lists and report similarity metrics."""
    from collections import Counter

    counter_a = Counter(pitches_a)
    counter_b = Counter(pitches_b)

    all_pitches = set(counter_a.keys()) | set(counter_b.keys())

    exact_matches = 0
    only_a = 0
    only_b = 0
    mismatched = 0

    for p in all_pitches:
        ca = counter_a.get(p, 0)
        cb = counter_b.get(p, 0)
        if ca > 0 and cb > 0:
            exact_matches += min(ca, cb)
            mismatched += abs(ca - cb)
        elif ca > 0:
            only_a += ca
        else:
            only_b += cb

    total_a = len(pitches_a)
    total_b = len(pitches_b)

    return {
        "exact_matches": exact_matches,
        "only_in_a": only_a,
        "only_in_b": only_b,
        "mismatched_counts": mismatched,
        "overlap_pct": (exact_matches / max(total_a, total_b) * 100) if max(total_a, total_b) > 0 else 0,
    }


def run_builtin(pdf_path: str) -> tuple:
    """Run built-in parser and return (success, xml_path, stats)."""
    from pianoplayer.conversion import convert_pdf_to_musicxml

    fd, tmp_xml = tempfile.mkstemp(suffix="_builtin.xml")
    os.close(fd)

    success, xml_path, error = convert_pdf_to_musicxml(
        pdf_path, tmp_xml, engine="builtin"
    )

    if not success:
        if os.path.exists(tmp_xml):
            os.remove(tmp_xml)
        return False, "", error, None

    stats = extract_musicxml_stats(tmp_xml)
    return True, tmp_xml, "", stats


def run_audiveris(pdf_path: str) -> tuple:
    """Run Audiveris parser and return (success, xml_path, error, stats)."""
    from pianoplayer.conversion import check_audiveris, convert_pdf_to_musicxml

    available, msg = check_audiveris()
    if not available:
        return False, "", msg, None

    fd, tmp_xml = tempfile.mkstemp(suffix="_audiveris.xml")
    os.close(fd)

    success, xml_path, error = convert_pdf_to_musicxml(
        pdf_path, tmp_xml, engine="audiveris"
    )

    if not success:
        if os.path.exists(tmp_xml):
            os.remove(tmp_xml)
        return False, "", error, None

    stats = extract_musicxml_stats(tmp_xml)
    return True, tmp_xml, "", stats


def print_report(pdf_path: str, builtin_result: tuple, audiveris_result: tuple) -> None:
    """Print a structured comparison report."""
    print("=" * 65)
    print("  OMR Engine Comparison Report")
    print("=" * 65)
    print(f"  Input PDF: {pdf_path}")
    print(f"  PDF size:  {os.path.getsize(pdf_path) / 1024:.1f} KB")
    print()

    builtin_ok, builtin_xml, builtin_err, builtin_stats = builtin_result
    audiveris_ok, audiveris_xml, audiveris_err, audiveris_stats = audiveris_result

    # Built-in results
    print("--- Built-in Parser ---")
    if builtin_ok:
        print(f"  Status:        OK")
        print(f"  Notes:         {builtin_stats['note_count']}")
        print(f"  Measures:      {builtin_stats['measure_count']}")
        print(f"  Pitch dist:    {builtin_stats['pitch_distribution']}")
    else:
        print(f"  Status:        FAILED")
        print(f"  Error:         {builtin_err}")

    print()

    # Audiveris results
    print("--- Audiveris ---")
    if audiveris_ok:
        print(f"  Status:        OK")
        print(f"  Notes:         {audiveris_stats['note_count']}")
        print(f"  Measures:      {audiveris_stats['measure_count']}")
        print(f"  Pitch dist:    {audiveris_stats['pitch_distribution']}")
    else:
        print(f"  Status:        FAILED")
        print(f"  Error:         {audiveris_err}")

    print()

    # Comparison
    if builtin_ok and audiveris_ok:
        print("--- Pitch Comparison ---")
        comparison = compare_pitch_lists(
            builtin_stats["pitches"],
            audiveris_stats["pitches"],
        )
        print(f"  Exact matches:     {comparison['exact_matches']}")
        print(f"  Only in builtin:   {comparison['only_in_a']}")
        print(f"  Only in audiveris: {comparison['only_in_b']}")
        print(f"  Mismatched counts: {comparison['mismatched_counts']}")
        print(f"  Overlap:           {comparison['overlap_pct']:.1f}%")

        # Note count delta
        note_delta = abs(builtin_stats["note_count"] - audiveris_stats["note_count"])
        note_pct = (note_delta / max(builtin_stats["note_count"], audiveris_stats["note_count"]) * 100)
        print()
        print("--- Summary ---")
        print(f"  Note count delta:  {note_delta} ({note_pct:.1f}%)")
        meas_delta = abs(builtin_stats["measure_count"] - audiveris_stats["measure_count"])
        print(f"  Measure delta:     {meas_delta}")
    elif builtin_ok:
        print("--- Summary ---")
        print(f"  Built-in parser produced {builtin_stats['note_count']} notes.")
        print(f"  Audiveris was not available for comparison.")
    else:
        print("--- Summary ---")
        print("  Neither engine produced usable output.")

    print()
    print("=" * 65)


def main():
    pdf_path = sys.argv[1] if len(sys.argv) > 1 else "test/source.pdf"

    if not os.path.isfile(pdf_path):
        print(f"Error: PDF not found: {pdf_path}")
        sys.exit(1)

    print(f"Analyzing: {pdf_path}")
    print()

    builtin_result = run_builtin(pdf_path)
    audiveris_result = run_audiveris(pdf_path)

    print_report(pdf_path, builtin_result, audiveris_result)

    # Cleanup temp files
    for _, xml_path, _, _ in (builtin_result, audiveris_result):
        if xml_path and os.path.exists(xml_path):
            os.remove(xml_path)


if __name__ == "__main__":
    main()
