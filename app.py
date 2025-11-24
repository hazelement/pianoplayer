from flask import Flask, render_template, request, send_file, jsonify
import os
import tempfile
from werkzeug.utils import secure_filename
from pianoplayer.core import annotate_with_args

app = Flask(__name__)
app.config['MAX_CONTENT_LENGTH'] = 16 * 1024 * 1024  # 16MB max file size
app.config['UPLOAD_FOLDER'] = tempfile.gettempdir()

ALLOWED_EXTENSIONS = {'mid', 'midi', 'xml', 'musicxml', 'mscz', 'mscx'}

def allowed_file(filename):
    return '.' in filename and filename.rsplit('.', 1)[1].lower() in ALLOWED_EXTENSIONS

@app.route('/')
def index():
    return render_template('index.html')

@app.route('/upload', methods=['POST'])
def upload_file():
    if 'file' not in request.files:
        return jsonify({'error': 'No file provided'}), 400
    
    file = request.files['file']
    if file.filename == '':
        return jsonify({'error': 'No file selected'}), 400
    
    if not allowed_file(file.filename):
        return jsonify({'error': 'Invalid file type. Please upload MIDI or MusicXML files.'}), 400
    
    try:
        # Save uploaded file
        filename = secure_filename(file.filename)
        input_path = os.path.join(app.config['UPLOAD_FOLDER'], filename)
        file.save(input_path)
        
        # Get hand size from request
        hand_size = request.form.get('handSize', 'M')
        
        # Generate output filename
        output_filename = 'annotated_' + os.path.splitext(filename)[0] + '.xml'
        output_path = os.path.join(app.config['UPLOAD_FOLDER'], output_filename)
        
        # Process the file
        annotate_with_args(
            filename=input_path,
            outputfile=output_path,
            n_measures=100,
            start_measure=1,
            depth=0,
            rbeam=0,
            lbeam=1,
            quiet=True,
            musescore=False,
            below_beam=False,
            with_vedo=False,
            vedo_speed=1.5,
            sound_off=True,
            left_only=False,
            right_only=False,
            hand_size_XXS=(hand_size == 'XXS'),
            hand_size_XS=(hand_size == 'XS'),
            hand_size_S=(hand_size == 'S'),
            hand_size_M=(hand_size == 'M'),
            hand_size_L=(hand_size == 'L'),
            hand_size_XL=(hand_size == 'XL'),
            hand_size_XXL=(hand_size == 'XXL')
        )
        
        # Clean up input file
        os.remove(input_path)
        
        return jsonify({
            'success': True,
            'filename': output_filename
        })
    
    except Exception as e:
        # Clean up on error
        if os.path.exists(input_path):
            os.remove(input_path)
        return jsonify({'error': str(e)}), 500

@app.route('/download/<filename>')
def download_file(filename):
    try:
        file_path = os.path.join(app.config['UPLOAD_FOLDER'], filename)
        response = send_file(file_path, as_attachment=True, download_name=filename)
        
        # Clean up after sending
        @response.call_on_close
        def cleanup():
            if os.path.exists(file_path):
                os.remove(file_path)
        
        return response
    except Exception as e:
        return jsonify({'error': 'File not found'}), 404

if __name__ == '__main__':
    app.run(debug=True, host='0.0.0.0', port=5000)
