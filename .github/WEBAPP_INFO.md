## PianoPlayer Web Application

A web application for automatic piano fingering generation.

### Project Structure
```
pianoplayer/
├── app.py                  # Flask web application
├── templates/
│   └── index.html          # Web interface
├── pianoplayer/            # Core fingering algorithm
│   ├── __init__.py
│   ├── core.py            # Main annotation logic
│   ├── hand.py            # Fingering generation
│   ├── scorereader.py     # Music score parsing
│   ├── utils.py           # Utility functions
│   ├── vkeyboard.py       # Virtual keyboard
│   └── wavegenerator.py   # Audio generation
├── scores/                 # Example scores
├── requirements.txt        # Python dependencies
├── README.md              # Documentation
└── LICENSE                # MIT License
```

### Technology Stack
- **Backend**: Flask (Python web framework)
- **Frontend**: HTML5, CSS3, JavaScript
- **Music Processing**: music21 library
- **File Handling**: werkzeug

### Key Features
1. Drag-and-drop file upload
2. Real-time processing feedback
3. Automatic file cleanup
4. Hand size customization
5. Work title inference from filename

### Development Notes
- The fingering algorithm uses combinatorial search with velocity optimization
- Direct XML manipulation avoids music21's round-trip issues with complex tuplets
- All fingerings are annotated by default (can be modified in core.py)
- Medium hand size (M) is the default, representing average adult hands
