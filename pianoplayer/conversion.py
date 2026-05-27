"""External tool dependency checks and helpers for file conversion."""

import logging
import os
import platform
import shutil
import subprocess
import tempfile
from enum import Enum

logger = logging.getLogger(__name__)


class PdfType(Enum):
    """PDF classification for OMR engine selection."""
    VECTOR = "vector"
    RASTER = "raster"
    AMBIGUOUS = "ambiguous"


def detect_pdf_type(pdf_path: str) -> PdfType:
    """Detect whether a PDF is vector-based (digital) or raster (scanned).

    Classification strategy:
    1. Analyze first 3 pages (capped for performance)
    2. Extract all image objects and calculate image coverage
    3. Count drawing commands (lines, curves, fills)
    4. Image coverage > 30% of page area -> RASTER
    5. Drawing count >= 20 AND image coverage < 10% -> VECTOR
    6. Ambiguous cases -> RASTER (safe default per C-4)
    7. Multi-page PDFs (> 1 page) -> RASTER (route to Audiveris)

    Args:
        pdf_path: Path to the PDF file to analyze.

    Returns:
        PdfType enum value: VECTOR, RASTER, or AMBIGUOUS.
    """
    try:
        import fitz  # PyMuPDF
    except ImportError:
        logger.warning("PyMuPDF (fitz) not installed, defaulting to AMBIGUOUS")
        return PdfType.AMBIGUOUS

    try:
        doc = fitz.open(pdf_path)
        total_pages = len(doc)

        # N-2: Multi-page PDFs -> RASTER (route to Audiveris)
        if total_pages > 1:
            doc.close()
            return PdfType.RASTER

        # Analyze only the first page (or first 3 for multi-page, but we already
        # returned RASTER above for multi-page, so this is single-page only)
        pages_to_analyze = min(total_pages, 3)

        total_image_area = 0
        total_page_area = 0
        total_drawing_count = 0

        for page_idx in range(pages_to_analyze):
            page = doc[page_idx]
            page_rect = page.rect
            page_area = page_rect.width * page_rect.height
            total_page_area += page_area

            # Extract images and calculate coverage
            images = page.get_images(full=True)
            for img_info in images:
                # img_info format: (xref, smask, width, height, ...)
                img_width = img_info[2]
                img_height = img_info[3]

                # Get image position on page to calculate actual coverage
                try:
                    image_rects = page.get_image_rects(img_info[0])
                    for rect in image_rects:
                        total_image_area += rect.width * rect.height
                except Exception:
                    # Fallback: estimate from image dimensions
                    total_image_area += img_width * img_height

            # Count drawing commands
            drawings = page.get_drawings()
            total_drawing_count += len(drawings)

        doc.close()

        if total_page_area == 0:
            return PdfType.AMBIGUOUS

        image_coverage = total_image_area / total_page_area

        # Classification logic
        if image_coverage > 0.30:
            # High image coverage -> scanned PDF
            return PdfType.RASTER
        elif total_drawing_count >= 20 and image_coverage < 0.10:
            # Vector drawings present, minimal images -> digital PDF
            return PdfType.VECTOR
        else:
            # C-4: Ambiguous cases (20-30% coverage, 50-100 drawings) default to
            # RASTER for safer Audiveris routing
            return PdfType.RASTER

    except Exception as e:
        logger.warning("PDF type detection failed: %s", e)
        return PdfType.AMBIGUOUS

def check_audiveris() -> tuple[bool, str]:
    """Check if Audiveris is available.

    Supports two installation modes:
    1. AUDIVERIS_HOME — path to Audiveris 5.x installation directory
       (contains bin/, lib/runtime/, lib/app/)
    2. AUDIVERIS_JAR_PATH — path to standalone audiveris.jar (legacy 4.x)
    """
    # Mode 1: Audiveris 5.x installation directory
    home = os.environ.get("AUDIVERIS_HOME")
    if home is not None:
        runtime_java = os.path.join(home, "lib", "runtime", "bin", "java")
        app_dir = os.path.join(home, "lib", "app")
        if os.path.isfile(runtime_java) and os.path.isdir(app_dir):
            # Also check system java is available as fallback
            return True, f"Audiveris installation found at: {home}"
        return False, f"Audiveris installation incomplete at: {home}"

    # Mode 2: Legacy standalone JAR
    jar_path = os.environ.get("AUDIVERIS_JAR_PATH")
    if jar_path is None:
        return False, (
            "Audiveris not configured. "
            "Set AUDIVERIS_HOME (Audiveris 5.x) or "
            "AUDIVERIS_JAR_PATH (legacy 4.x) environment variable."
        )
    if not os.path.isfile(jar_path):
        return False, f"Audiveris JAR not found at: {jar_path}"
    return True, f"Audiveris JAR found at: {jar_path}"


def _get_musescore_binary() -> str | None:
    """Return the path to the MuseScore binary, or None if not found.

    Checks common binary names: musescore4, musescore3, musescore (in that order).
    """
    candidates = ["musescore4", "musescore3", "musescore"]
    for name in candidates:
        path = shutil.which(name)
        if path is not None:
            return path
    return None


def _build_audiveris_java_cmd(
    java_heap: str,
    audiveris_home: str | None,
    audiveris_jar: str | None,
) -> list[str] | None:
    """Build the Java command for running Audiveris.

    Supports two modes:
    1. Audiveris 5.x: Uses bundled JRE with full classpath from lib/app/
    2. Legacy 4.x: Uses system java with -jar audiveris.jar

    Returns the java command list, or None if configuration is invalid.
    """
    if audiveris_home is not None:
        # Audiveris 5.x: bundled JRE + classpath
        runtime_java = os.path.join(audiveris_home, "lib", "runtime", "bin", "java")
        app_dir = os.path.join(audiveris_home, "lib", "app")
        if not os.path.isfile(runtime_java) or not os.path.isdir(app_dir):
            return None

        # Build classpath from all JARs in lib/app/
        classpath_jars = []
        for f in sorted(os.listdir(app_dir)):
            if f.endswith(".jar"):
                classpath_jars.append(os.path.join(app_dir, f))
        classpath = os.pathsep.join(classpath_jars)

        return [
            runtime_java,
            f"-Xmx{java_heap}",
            "-Djava.awt.headless=true",
            "-Dfile.encoding=UTF-8",
            "--enable-native-access=ALL-UNNAMED",
            "-cp",
            classpath,
            "Audiveris",
        ]

    elif audiveris_jar is not None:
        # Legacy 4.x: system java + -jar
        java_path = shutil.which("java")
        if java_path is None:
            return None
        return [
            java_path,
            f"-Xmx{java_heap}",
            "-jar",
            audiveris_jar,
        ]

    return None


def _run_audiveris_5x(
    java_cmd: list[str],
    pdf_path: str,
    output_dir: str,
    timeout: int,
) -> tuple[bool, str, str]:
    """Run Audiveris 5.x single-command OMR workflow.

    Audiveris 5.x uses a unified CLI:
      Audiveris -batch -transcribe -export -output <dir> <input.pdf>

    This single command handles: workspace creation, PDF import, OMR
    recognition, and MusicXML export.
    """
    logger.info("Running Audiveris 5.x OMR: %s -> %s", pdf_path, output_dir)

    cmd = java_cmd + [
        "-batch",
        "-transcribe",
        "-export",
        "-output",
        output_dir,
        pdf_path,
    ]

    logger.info("Command: %s", " ".join(cmd[:8]) + " ...")

    result = subprocess.run(
        cmd,
        capture_output=True,
        text=True,
        timeout=timeout,
    )

    combined_output = (result.stdout + result.stderr)[-2000:]
    logger.debug("Audiveris output:\n%s", combined_output)

    if result.returncode != 0:
        return False, "", (
            f"Audiveris OMR failed (exit {result.returncode}): "
            f"{combined_output[:500]}"
        )

    # Find the exported MusicXML file in the output directory
    exported_xml = _find_musicxml_in_dir(output_dir)
    if exported_xml is None:
        # Check for .omr workspace file — transcription may have failed
        # silently but produced a workspace
        return False, "", (
            f"No MusicXML file found in output directory: {output_dir}. "
            f"Audiveris may have failed OMR recognition."
        )

    return True, exported_xml, ""

def _reduce_pdf_resolution(
    pdf_path: str, max_pixels: int = 20_000_000
) -> tuple[str, bool]:
    """Reduce PDF resolution if it exceeds Audiveris's pixel limit.

    Audiveris has a hardcoded ~20M pixel limit per page. High-resolution
    PDFs (e.g., scanned sheet music at 300+ DPI) can exceed this.

    Args:
        pdf_path: Path to input PDF.
        max_pixels: Maximum estimated pixels per page (default 20M for
            Audiveris safety margin).

    Returns:
        (path, needs_cleanup) — path to use for OMR, and whether it's a
        temp file that should be deleted after processing.
    """
    try:
        import fitz  # PyMuPDF
    except ImportError:
        logger.warning("PyMuPDF (fitz) not installed, skipping resolution check")
        return pdf_path, False

    doc = fitz.open(pdf_path)
    max_estimated_pixels = 0

    for page in doc:
        width = page.rect.width
        height = page.rect.height
        # Audiveris renders PDFs at ~4x native resolution.
        # Account for that multiplier when estimating pixel count.
        estimated_pixels = (width * 4) * (height * 4)
        max_estimated_pixels = max(max_estimated_pixels, estimated_pixels)

    if max_estimated_pixels <= max_pixels:
        doc.close()
        return pdf_path, False

    # Need to reduce resolution.
    # Target ~15M pixels to stay safely under the 20M limit.
    target_pixels = 15_000_000
    scale = (target_pixels / max_estimated_pixels) ** 0.5
    # Don't scale below 10% of original (would be unreadable for OMR).
    scale = max(scale, 0.1)

    logger.info(
        "PDF exceeds Audiveris pixel limit (%d pixels). "
        "Scaling to %.0f%% (~%d pixels).",
        max_estimated_pixels,
        scale * 100,
        max_estimated_pixels * scale * scale,
    )

    # Use vector scaling to preserve quality (don't rasterize).
    # Scale page dimensions and embed original page as vector graphic.
    new_doc = fitz.open()
    for idx in range(len(doc)):
        page = doc[idx]
        new_width = page.rect.width * scale
        new_height = page.rect.height * scale
        new_page = new_doc.new_page(width=new_width, height=new_height)
        new_page.show_pdf_page(
            fitz.Rect(0, 0, new_width, new_height),
            doc,
            idx
        )

    doc.close()

    # Save to temp file.
    fd, tmp_path = tempfile.mkstemp(suffix='_resized.pdf')
    os.close(fd)
    new_doc.save(tmp_path)
    new_doc.close()

    return tmp_path, True


def convert_pdf_to_musicxml(
    pdf_path: str,
    output_xml_path: str,
    engine: str = "auto",
) -> tuple[bool, str, str]:
    """Convert PDF to MusicXML using specified OMR engine.

    Supports dual-engine conversion with automatic PDF type detection and
    fallback. Engine A → Engine B → hard fail (N-1: no bounce back).

    Audiveris-specific settings (java_heap, timeout, jar path) are resolved
    internally from environment variables: AUDIVERIS_JAVA_HEAP, OMR_TIMEOUT,
    AUDIVERIS_JAR_PATH.

    Args:
        pdf_path: Path to input PDF file.
        output_xml_path: Desired path for output MusicXML file.
        engine: Engine selection: "auto" (default), "audiveris", or "builtin".
            "auto" uses PDF type detection to route: VECTOR → builtin,
            RASTER/AMBIGUOUS → audiveris.

    Returns:
        (success, xml_path, error_message)
        On success: (True, path_to_musicxml, "")
        On failure: (False, "", error_description)
    """
    # Validate engine parameter
    if engine not in ("auto", "audiveris", "builtin"):
        return False, "", f"Invalid engine: '{engine}'. Must be 'auto', 'audiveris', or 'builtin'."

    # Auto mode: use PDF type detection to route
    if engine == "auto":
        pdf_type = detect_pdf_type(pdf_path)
        if pdf_type == PdfType.VECTOR:
            primary_engine = "builtin"
        else:  # RASTER or AMBIGUOUS
            primary_engine = "audiveris"
    else:
        primary_engine = engine

    logger.info("PDF conversion: primary engine = %s", primary_engine)

    # Try primary engine
    success, xml_path, error = _try_engine(
        primary_engine, pdf_path, output_xml_path,
    )

    # Fallback: if primary fails, try other engine ONCE (N-1: no bounce back)
    if not success:
        fallback_engine = "audiveris" if primary_engine == "builtin" else "builtin"
        logger.info(
            "Primary engine (%s) failed: %s. Trying fallback: %s",
            primary_engine, error, fallback_engine,
        )
        success2, xml_path2, error2 = _try_engine(
            fallback_engine, pdf_path, output_xml_path,
        )
        if success2:
            return True, xml_path2, (
                f"Primary engine ({primary_engine}) failed: {error}. "
                f"Fallback to {fallback_engine} succeeded."
            )
        else:
            return False, "", (
                f"Both engines failed. "
                f"{primary_engine}: {error}. "
                f"{fallback_engine}: {error2}."
            )

    return success, xml_path, error


def _try_audiveris(
    pdf_path: str,
    output_xml_path: str,
    java_heap: str | None = None,
    timeout: int | None = None,
    audiveris_jar: str | None = None,
) -> tuple[bool, str, str]:
    """Run Audiveris OMR conversion.

    Handles all Audiveris-specific logic: config resolution, Java command
    building, PDF resolution reduction, execution, and cleanup.

    Returns (success, xml_path, error_msg).
    """
    # Resolve configuration from env vars with defaults
    if java_heap is None:
        java_heap = os.environ.get("AUDIVERIS_JAVA_HEAP", "1G")
    if timeout is None:
        timeout = int(os.environ.get("OMR_TIMEOUT", "600"))
    if audiveris_jar is None:
        audiveris_jar = os.environ.get("AUDIVERIS_JAR_PATH")

    audiveris_home = os.environ.get("AUDIVERIS_HOME")

    # Pre-flight checks
    if not os.path.isfile(pdf_path):
        return False, "", f"Input PDF not found: {pdf_path}"

    if audiveris_home is None and audiveris_jar is None:
        return False, "", (
            "Audiveris OMR engine is not configured. "
            "Please use the built-in parser or upload MusicXML/MIDI instead."
        )

    # Build Java command
    java_cmd = _build_audiveris_java_cmd(java_heap, audiveris_home, audiveris_jar)
    if java_cmd is None:
        return False, "", (
            "Audiveris Java command failed to build. "
            "Check your Audiveris installation and environment variables."
        )

    # Reduce PDF resolution if needed (Audiveris ~20M pixel limit)
    pdf_to_use, needs_cleanup = _reduce_pdf_resolution(pdf_path)

    # Create temporary output directory
    output_dir = tempfile.mkdtemp(prefix="audiveris_export_")

    try:
        logger.info(
            "Starting Audiveris OMR: %s -> %s", pdf_to_use, output_xml_path
        )
        logger.info("Java heap: %s", java_heap)

        if audiveris_home is not None:
            # Audiveris 5.x: single unified command
            success, exported_xml, error = _run_audiveris_5x(
                java_cmd, pdf_to_use, output_dir, timeout
            )
            if not success:
                return False, "", error
        else:
            # Legacy 4.x: multi-step workflow
            return _run_audiveris_legacy(
                java_cmd, pdf_to_use, output_dir, timeout
            )

        # Ensure uncompressed XML (music21 can't parse .mxl)
        if exported_xml.endswith(".mxl"):
            exported_xml = _ensure_uncompressed_xml(exported_xml)

        # Copy to desired output path
        shutil.copy2(exported_xml, output_xml_path)
        logger.info(
            "OMR complete: %s -> %s", exported_xml, output_xml_path
        )

        return True, output_xml_path, ""

    except subprocess.TimeoutExpired:
        return False, "", (
            f"Audiveris OMR timed out after {timeout}s. "
            f"Try the built-in parser for faster processing."
        )
    except Exception as e:
        return False, "", f"Audiveris OMR failed: {str(e)}"
    finally:
        # Cleanup temporary directories and resized PDF
        shutil.rmtree(output_dir, ignore_errors=True)
        if needs_cleanup and os.path.exists(pdf_to_use):
            os.remove(pdf_to_use)


def _try_builtin_parser(
    pdf_path: str,
    output_xml_path: str,
) -> tuple[bool, str, str]:
    """Run the built-in vector PDF parser.

    Parses digital (vector-based) PDFs directly without OMR engines.
    Validates output has minimum note count (> 10) and valid XML.

    Returns (success, xml_path, error_msg).
    """
    try:
        from pianoplayer import pdf_parser

        parser = pdf_parser.PDFParser(pdf_path)
        try:
            results = parser.parse_all_pages()
            if not results:
                return False, "", "Built-in parser: no pages could be parsed"

            page_data = results[0]  # First page only
            note_count = len(page_data.get("note_heads", []))

            if note_count < 10:
                return False, "", (
                    f"Built-in parser: only {note_count} notes detected "
                    f"(minimum 10 required). This PDF may be scanned/raster-based."
                )

            pdf_parser.export_to_musicxml(page_data, output_xml_path)

            # Validate XML output is parseable
            import xml.etree.ElementTree as ET
            ET.parse(output_xml_path)

            return True, output_xml_path, ""
        finally:
            parser.close()
    except Exception as e:
        return False, "", f"Built-in parser error: {str(e)}"


def _try_engine(
    engine: str,
    pdf_path: str,
    output_xml_path: str,
) -> tuple[bool, str, str]:
    """Route to the appropriate engine and execute conversion.

    Args:
        engine: "audiveris" or "builtin".
        pdf_path: Path to input PDF.
        output_xml_path: Path for output MusicXML.

    Returns:
        (success, xml_path, error_msg)
    """
    if engine == "audiveris":
        return _try_audiveris(pdf_path, output_xml_path)
    elif engine == "builtin":
        return _try_builtin_parser(pdf_path, output_xml_path)
    else:
        return False, "", f"Unknown engine: {engine}"


def _run_audiveris_legacy(
    java_cmd: list[str],
    pdf_path: str,
    output_dir: str,
    timeout: int,
) -> tuple[bool, str, str]:
    """Run legacy Audiveris 4.x multi-step OMR workflow.

    Steps: create workspace → import PDF → recognize → export MusicXML.
    """
    workspace_dir = tempfile.mkdtemp(prefix="audiveris_")

    try:
        logger.info("Running Audiveris legacy 4.x OMR workflow")
        logger.info("Workspace: %s", workspace_dir)

        headless_cmd = java_cmd + ["--headless"]

        # Step 1: Create workspace
        logger.info("Step 1/4: Creating Audiveris workspace...")
        result = subprocess.run(
            headless_cmd + ["--create-workspace", workspace_dir],
            capture_output=True, text=True, timeout=timeout,
        )
        if result.returncode != 0:
            return False, "", f"Audiveris workspace creation failed: {result.stderr[:500]}"

        # Step 2: Import PDF
        logger.info("Step 2/4: Importing PDF...")
        result = subprocess.run(
            headless_cmd + ["--import", workspace_dir, pdf_path],
            capture_output=True, text=True, timeout=timeout,
        )
        if result.returncode != 0:
            return False, "", f"Audiveris PDF import failed: {result.stderr[:500]}"

        # Step 3: Recognize
        logger.info("Step 3/4: Running OMR recognition...")
        result = subprocess.run(
            headless_cmd + ["--recognize-all", workspace_dir],
            capture_output=True, text=True, timeout=timeout,
        )
        if result.returncode != 0:
            return False, "", f"Audiveris OMR recognition failed: {result.stderr[:500]}"

        # Step 4: Export MusicXML
        logger.info("Step 4/4: Exporting MusicXML...")
        result = subprocess.run(
            headless_cmd + ["--export", workspace_dir, output_dir],
            capture_output=True, text=True, timeout=timeout,
        )
        if result.returncode != 0:
            return False, "", f"Audiveris MusicXML export failed: {result.stderr[:500]}"

        exported_xml = _find_musicxml_in_dir(output_dir)
        if exported_xml is None:
            return False, "", f"No MusicXML file found in export directory: {output_dir}"

        return True, exported_xml, ""

    finally:
        shutil.rmtree(workspace_dir, ignore_errors=True)


def _find_musicxml_in_dir(directory: str) -> str | None:
    """Find a MusicXML file in the given directory (recursive).

    Returns the path to the first .xml file found, or None.
    """
    for root, _dirs, files in os.walk(directory):
        for f in files:
            if f.endswith(".xml") or f.endswith(".musicxml") or f.endswith(".mxl"):
                return os.path.join(root, f)
    return None


def _ensure_uncompressed_xml(xml_path: str) -> str:
    """If xml_path is a .mxl (ZIP-compressed MusicXML), decompress it.
    
    Returns the path to an uncompressed .xml file that music21 can parse.
    If already uncompressed, returns the original path unchanged.
    """
    if not xml_path.endswith(".mxl"):
        return xml_path
    
    import zipfile
    
    out_path = xml_path.replace(".mxl", ".xml")
    try:
        with zipfile.ZipFile(xml_path, "r") as zf:
            # .mxl contains a single score.xml or similar
            for name in zf.namelist():
                if name.endswith(".xml"):
                    zf.extract(name, os.path.dirname(out_path))
                    extracted = os.path.join(os.path.dirname(out_path), name)
                    if extracted != out_path:
                        os.rename(extracted, out_path)
                    return out_path
    except Exception as e:
        logger.warning(f"Failed to decompress .mxl: {e}")
    
    return xml_path


def export_musicxml_to_pdf(
    xml_path: str,
    output_pdf_path: str,
    timeout: int | None = None,
) -> tuple[bool, str, str]:
    """Export annotated MusicXML to PDF using MuseScore CLI.

    Args:
        xml_path: Path to input annotated MusicXML file.
        output_pdf_path: Desired path for output PDF file.
        timeout: Timeout in seconds. Defaults to PDF_EXPORT_TIMEOUT env var or 120.

    Returns:
        (success, pdf_path, error_message)
    """
    if timeout is None:
        timeout = int(os.environ.get("PDF_EXPORT_TIMEOUT", "120"))

    if not os.path.isfile(xml_path):
        return False, "", f"Input MusicXML not found: {xml_path}"

    ms_binary = _get_musescore_binary()
    if ms_binary is None:
        return False, "", "MuseScore binary not found. Install MuseScore 3.x or 4.x."

    try:
        logger.info("MuseScore PDF export: %s -> %s", xml_path, output_pdf_path)

        result = subprocess.run(
            [ms_binary, "-o", output_pdf_path, xml_path],
            capture_output=True, text=True, timeout=timeout
        )

        if result.returncode != 0:
            return False, "", (
                f"MuseScore PDF export failed (exit {result.returncode}): "
                f"{result.stderr[:500]}"
            )

        if not os.path.isfile(output_pdf_path):
            return False, "", f"MuseScore did not produce PDF: {output_pdf_path}"

        logger.info("PDF export complete: %s", output_pdf_path)
        return True, output_pdf_path, ""

    except subprocess.TimeoutExpired:
        return False, "", f"MuseScore PDF export timed out after {timeout} seconds"
    except Exception as e:
        return False, "", f"MuseScore PDF export failed: {str(e)}"


