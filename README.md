# PianoPlayer

Automatic piano fingering generator web application. Upload MIDI or MusicXML files and download them with optimal piano fingering annotations.

## Features

- 🎹 Upload MIDI (.mid, .midi) or MusicXML (.xml, .musicxml, .mscz, .mscx) files
- ✋ Select hand size (XXS to XXL, default: Medium)
- 🎼 Automatic fingering annotation for both hands
- ⬇️ Download annotated MusicXML files
- 🎨 Modern, responsive web interface with drag-and-drop
- 📝 Automatic work title and movement title in output

## Installation

1. Clone the repository:
```bash
git clone https://github.com/hazelement/pianoplayer.git
cd pianoplayer
```

2. Install dependencies:
```bash
pip install -r requirements.txt
```

3. Ensure MuseScore is installed (for MIDI to MusicXML conversion):
```bash
# Ubuntu/Debian
sudo apt install musescore

# macOS
brew install musescore

# Windows - download from https://musescore.org
```

## Running the Web Application

Start the Flask server:
```bash
python app.py
```

The application will be available at `http://localhost:5000`

## Usage

1. Open your browser and go to `http://localhost:5000`
2. Upload a MIDI or MusicXML file (drag-and-drop or click to browse)
3. Select your hand size (default is Medium)
4. Click "Process File"
5. Wait for processing to complete
6. Click "Download Annotated File" to get your fingered score

## Hand Size Options

- **XXS** - Extra Extra Small (child hands)
- **XS** - Extra Small
- **S** - Small
- **M** - Medium (default, average adult)
- **L** - Large
- **XL** - Extra Large
- **XXL** - Extra Extra Large (very large hands)

## API Usage

You can also use the fingering annotation programmatically:

```python
from pianoplayer.core import annotate_with_args

annotate_with_args(
    filename='input.mid',
    outputfile='output.xml',
    hand_size_M=True  # Medium hand size
)
```

## Command Line Usage (Legacy)

You can still use the command line interface:

```bash
python -m pianoplayer.core input.mid -o output.xml
# Or with options:
python -m pianoplayer.core input.mid -o output.xml -M --quiet
#
# Optional arguments:
#   -h, --help            show this help message and exit
#   -o , --outputfile     Annotated output xml file name
#   -n , --n-measures     [100] Number of score measures to scan
#   -s , --start-measure  Start from measure number [1]
#   -d , --depth          [auto] Depth of combinatorial search, [2-9]
#   -rbeam                [0] Specify Right Hand beam number
#   -lbeam                [1] Specify Left Hand beam number
#   --quiet               Switch off verbosity
#   -m, --musescore       Open output in musescore after processing
#   -b, --below-beam      Show fingering numbers below beam line
#   -v, --with-vedo       Play 3D scene after processing
#   -z, --sound-off       Disable sound
#   -l, --left-only       Fingering for left hand only
#   -r, --right-only      Fingering for right hand only
#   -XXS, --hand-size-XXS Set hand size to XXS
#   -XS, --hand-size-XS   Set hand size to XS
#   -S, --hand-size-S     Set hand size to S
#   -M, --hand-size-M     Set hand size to M
#   -L, --hand-size-L     Set hand size to L
#   -XL, --hand-size-XL   Set hand size to XL
#   -XXL, --hand-size-XXL Set hand size to XXL
```

## Production Deployment

For production use, use a WSGI server like Gunicorn:

```bash
pip install gunicorn
gunicorn -w 4 -b 0.0.0.0:5000 app:app
```

## File Size Limits

- Maximum upload size: 16 MB
- Supported formats: `.mid`, `.midi`, `.xml`, `.musicxml`, `.mscz`, `.mscx`

## Security Features

- Files are automatically deleted after download
- Temporary files are stored securely
- File uploads are sanitized
- File type validation enforced


## How the algorithm works:
The algorithm minimizes the fingers speed needed to play a sequence of notes or chords by searching
through feasible combinations of fingerings.

One possible advantage of this algorithm over similar ones is that it is completely *dynamic*,
which means that it
takes into account the physical position and speed of fingers while moving on the keyboard
and the duration of each played note.
It is *not* based on a static look-up table of likely or unlikely combinations of fingerings.

Fingering a piano score can vary a lot from individual to individual, therefore there is not such
a thing as a "best" choice for fingering.
This algorithm is meant to suggest a fingering combination which is "optimal" in the sense that it
minimizes the effort of the hand avoiding unnecessary movements.

## Parameters you can change:
- Your hand size (from 'XXS' to 'XXL') which sets the relaxed distance between thumb and pinkie.
- The beam number associated to the right hand is by default nr.0 (nr.1 for left hand).
You can change it with `-rbeam` and `-lbeam` command line options.
- Depth of combinatorial search, from 3 up to 9 notes ahead of the currently playing note. By
default the algorithm selects this number automatically based on the duration of the notes to be played.

## Limitations
- Some specific fingering combinations, considered too unlikely in the first place, are excluded from the search (e.g. the 3rd finger crossing the 4th).
- Hands are always assumed independent from each other.
- In the 3D representation with sounds enabled, notes are played one after the other (no chords),
so the tempo within the measure is not always respected.
- Small notes/ornaments are ignored.


