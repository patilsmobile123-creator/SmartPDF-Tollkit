from flask import Flask, request, send_file, jsonify
from flask_cors import CORS
import os
import io
import zipfile
import tempfile
import time
from pypdf import PdfReader, PdfWriter
from PIL import Image
from reportlab.lib.pagesizes import A4, letter
from reportlab.pdfgen import canvas
from reportlab.lib.utils import ImageReader
import fitz  # PyMuPDF

app = Flask(__name__, static_folder='.', static_url_path='')
CORS(app)

# Use temp directory for uploads (Render compatible)
UPLOAD_FOLDER = tempfile.mkdtemp()
os.makedirs(UPLOAD_FOLDER, exist_ok=True)

def cleanup_files():
    """Remove old files to prevent disk fill"""
    try:
        for f in os.listdir(UPLOAD_FOLDER):
            fp = os.path.join(UPLOAD_FOLDER, f)
            if os.path.isfile(fp) and time.time() - os.path.getmtime(fp) > 3600:
                os.remove(fp)
    except:
        pass

@app.route('/')
def home():
    return app.send_static_file('index.html')

# 1. IMAGES TO PDF
@app.route('/api/images-to-pdf', methods=['POST'])
def images_to_pdf():
    try:
        files = request.files.getlist('images')
        if not files:
            return jsonify({'error': 'No images uploaded'}), 400
        
        page_size = request.form.get('page_size', 'A4')
        orientation = request.form.get('orientation', 'portrait')
        fit_mode = request.form.get('fit_mode', 'fit')
        
        # Set page size
        if page_size == 'A4':
            size = A4
        else:
            size = letter
            
        if orientation == 'landscape':
            size = (size[1], size[0])
        
        output_path = os.path.join(UPLOAD_FOLDER, f"images_{int(time.time())}.pdf")
        c = canvas.Canvas(output_path, pagesize=size)
        width, height = size
        
        for file in files:
            try:
                img = Image.open(file.stream)
                img_width, img_height = img.size
                aspect = img_height / float(img_width)
                
                if fit_mode == 'fit':
                    # Fit image to page maintaining aspect ratio
                    if (height/width) > aspect:
                        new_width = width
                        new_height = width * aspect
                    else:
                        new_height = height
                        new_width = height / aspect
                    x = (width - new_width) / 2
                    y = (height - new_height) / 2
                    c.drawImage(ImageReader(img), x, y, width=new_width, height=new_height)
                elif fit_mode == 'fill':
                    # Stretch to fill page
                    c.drawImage(ImageReader(img), 0, 0, width=width, height=height)
                else:  # original
                    # Original size, bottom-left aligned
                    c.drawImage(ImageReader(img), 0, 0)
                
                c.showPage()
            except Exception as e:
                return jsonify({'error': f'Error processing image: {str(e)}'}), 400
        
        c.save()
        cleanup_files()
        return send_file(output_path, as_attachment=True, download_name='converted_images.pdf')
        
    except Exception as e:
        return jsonify({'error': str(e)}), 500

# 2. MERGE PDFs
@app.route('/api/merge-pdf', methods=['POST'])
def merge_pdfs():
    try:
        files = request.files.getlist('pdfs')
        if len(files) < 2:
            return jsonify({'error': 'Please upload at least 2 PDFs'}), 400
        
        writer = PdfWriter()
        
        for file in files:
            try:
                reader = PdfReader(file.stream)
                for page in reader.pages:
                    writer.add_page(page)
            except Exception as e:
                return jsonify({'error': f'Error reading PDF {file.filename}: {str(e)}'}), 400
        
        output_path = os.path.join(UPLOAD_FOLDER, f"merged_{int(time.time())}.pdf")
        with open(output_path, 'wb') as f:
            writer.write(f)
        
        cleanup_files()
        return send_file(output_path, as_attachment=True, download_name='merged.pdf')
        
    except Exception as e:
        return jsonify({'error': str(e)}), 500

# 3. SPLIT PDF
@app.route('/api/split-pdf', methods=['POST'])
def split_pdf():
    try:
        file = request.files.get('pdf')
        if not file:
            return jsonify({'error': 'No PDF uploaded'}), 400
        
        split_type = request.form.get('split_type', 'all')
        pages_str = request.form.get('pages', '')
        
        reader = PdfReader(file.stream)
        total_pages = len(reader.pages)
        
        memory_file = io.BytesIO()
        
        with zipfile.ZipFile(memory_file, 'w', zipfile.ZIP_DEFLATED) as zf:
            if split_type == 'all':
                # Split all pages individually
                for i, page in enumerate(reader.pages):
                    writer = PdfWriter()
                    writer.add_page(page)
                    page_io = io.BytesIO()
                    writer.write(page_io)
                    page_io.seek(0)
                    zf.writestr(f'page_{i+1}.pdf', page_io.getvalue())
            else:
                # Parse page range (e.g., "1-3,5,7-9")
                pages_to_extract = []
                try:
                    for part in pages_str.split(','):
                        part = part.strip()
                        if '-' in part:
                            start, end = map(int, part.split('-'))
                            pages_to_extract.extend(range(start-1, end))
                        else:
                            pages_to_extract.append(int(part)-1)
                except:
                    return jsonify({'error': 'Invalid page range format. Use: 1-3,5,7'}), 400
                
                # Extract specified pages
                for i in pages_to_extract:
                    if 0 <= i < total_pages:
                        writer = PdfWriter()
                        writer.add_page(reader.pages[i])
                        page_io = io.BytesIO()
                        writer.write(page_io)
                        page_io.seek(0)
                        zf.writestr(f'page_{i+1}.pdf', page_io.getvalue())
        
        memory_file.seek(0)
        cleanup_files()
        return send_file(memory_file, as_attachment=True, download_name='split_pages.zip', mimetype='application/zip')
        
    except Exception as e:
        return jsonify({'error': str(e)}), 500

# 4. ROTATE PDF
@app.route('/api/rotate-pdf', methods=['POST'])
def rotate_pdf():
    try:
        file = request.files.get('pdf')
        if not file:
            return jsonify({'error': 'No PDF uploaded'}), 400
        
        angle = int(request.form.get('angle', 90))
        
        reader = PdfReader(file.stream)
        writer = PdfWriter()
        
        for page in reader.pages:
            page.rotate(angle)
            writer.add_page(page)
        
        output_path = os.path.join(UPLOAD_FOLDER, f"rotated_{int(time.time())}.pdf")
        with open(output_path, 'wb') as f:
            writer.write(f)
        
        cleanup_files()
        return send_file(output_path, as_attachment=True, download_name='rotated.pdf')
        
    except Exception as e:
        return jsonify({'error': str(e)}), 500

# 5. COMPRESS PDF
@app.route('/api/compress-pdf', methods=['POST'])
def compress_pdf():
    try:
        file = request.files.get('pdf')
        if not file:
            return jsonify({'error': 'No PDF uploaded'}), 400
        
        # Read original file
        file_content = file.read()
        original_size = len(file_content)
        
        reader = PdfReader(io.BytesIO(file_content))
        writer = PdfWriter()
        
        # Add all pages
        for page in reader.pages:
            writer.add_page(page)
        
        # Copy metadata
        if reader.metadata:
            writer.add_metadata(reader.metadata)
        
        output_path = os.path.join(UPLOAD_FOLDER, f"compressed_{int(time.time())}.pdf")
        with open(output_path, 'wb') as f:
            writer.write(f)
        
        compressed_size = os.path.getsize(output_path)
        savings = round((1 - compressed_size/original_size) * 100, 1) if original_size > 0 else 0
        
        response = send_file(output_path, as_attachment=True, download_name='compressed.pdf')
        response.headers['X-Original-Size'] = str(original_size)
        response.headers['X-Compressed-Size'] = str(compressed_size)
        response.headers['X-Savings'] = str(savings)
        
        cleanup_files()
        return response
        
    except Exception as e:
        return jsonify({'error': str(e)}), 500

# 6. PDF INFO
@app.route('/api/pdf-info', methods=['POST'])
def pdf_info():
    try:
        file = request.files.get('pdf')
        if not file:
            return jsonify({'error': 'No PDF uploaded'}), 400
        
        file_content = file.read()
        file_size_kb = round(len(file_content) / 1024, 2)
        
        reader = PdfReader(io.BytesIO(file_content))
        
        info = {
            'pages': len(reader.pages),
            'size_kb': file_size_kb,
            'encrypted': reader.is_encrypted,
            'author': '',
            'title': '',
            'creator': '',
            'width_mm': None,
            'height_mm': None
        }
        
        # Extract metadata safely
        if reader.metadata:
            meta = reader.metadata
            info['author'] = str(meta.get('/Author', '')) if meta.get('/Author') else ''
            info['title'] = str(meta.get('/Title', '')) if meta.get('/Title') else ''
            info['creator'] = str(meta.get('/Creator', '')) if meta.get('/Creator') else ''
        
        # Get page dimensions from first page
        if reader.pages:
            try:
                box = reader.pages[0].mediabox
                # Convert points to mm (1 point = 0.352777 mm)
                info['width_mm'] = round(float(box.width) * 0.352777, 1)
                info['height_mm'] = round(float(box.height) * 0.352777, 1)
            except:
                pass
        
        return jsonify(info)
        
    except Exception as e:
        return jsonify({'error': str(e)}), 500

# 7. PASSWORD PDF (Encrypt/Decrypt)
@app.route('/api/password-pdf', methods=['POST'])
def password_pdf():
    try:
        file = request.files.get('pdf')
        if not file:
            return jsonify({'error': 'No PDF uploaded'}), 400
        
        password = request.form.get('password', '')
        action = request.form.get('action', 'encrypt')
        
        if not password:
            return jsonify({'error': 'Password is required'}), 400
        
        reader = PdfReader(file.stream)
        writer = PdfWriter()
        
        # Add all pages to writer
        for page in reader.pages:
            writer.add_page(page)
        
        output_path = os.path.join(UPLOAD_FOLDER, f"password_{int(time.time())}.pdf")
        
        if action == 'encrypt':
            # Encrypt with password
            writer.encrypt(password)
            with open(output_path, 'wb') as f:
                writer.write(f)
            cleanup_files()
            return send_file(output_path, as_attachment=True, download_name='protected.pdf')
        else:
            # Decrypt
            if reader.is_encrypted:
                try:
                    reader.decrypt(password)
                    # Re-create writer with decrypted pages
                    writer = PdfWriter()
                    for page in reader.pages:
                        writer.add_page(page)
                except:
                    return jsonify({'error': 'Incorrect password'}), 400
            
            with open(output_path, 'wb') as f:
                writer.write(f)
            cleanup_files()
            return send_file(output_path, as_attachment=True, download_name='decrypted.pdf')
            
    except Exception as e:
        return jsonify({'error': str(e)}), 500

# 8. PDF TO IMAGES
@app.route('/api/pdf-to-images', methods=['POST'])
def pdf_to_images():
    try:
        file = request.files.get('pdf')
        if not file:
            return jsonify({'error': 'No PDF uploaded'}), 400
        
        fmt = request.form.get('format', 'JPEG')
        dpi = int(request.form.get('dpi', 150))
        pages_str = request.form.get('pages', 'all')
        
        # Read PDF into memory
        pdf_bytes = file.read()
        doc = fitz.open(stream=pdf_bytes, filetype="pdf")
        
        memory_file = io.BytesIO()
        
        with zipfile.ZipFile(memory_file, 'w', zipfile.ZIP_DEFLATED) as zf:
            # Determine which pages to process
            if pages_str == 'all':
                page_nums = range(len(doc))
            else:
                try:
                    # Parse range like "1-5" or "1,3,5"
                    if '-' in pages_str:
                        start, end = map(int, pages_str.split('-'))
                        page_nums = range(start-1, end)
                    else:
                        page_nums = [int(p.strip())-1 for p in pages_str.split(',')]
                except:
                    return jsonify({'error': 'Invalid page range. Use: 1-5 or 1,3,5'}), 400
            
            for i in page_nums:
                if 0 <= i < len(doc):
                    page = doc.load_page(i)
                    # Create matrix for DPI
                    mat = fitz.Matrix(dpi/72, dpi/72)
                    pix = page.get_pixmap(matrix=mat)
                    
                    # Convert to desired format
                    if fmt == 'PNG':
                        img_data = pix.tobytes("png")
                        ext = 'png'
                    else:
                        img_data = pix.tobytes("jpeg")
                        ext = 'jpg'
                    
                    zf.writestr(f'page_{i+1}.{ext}', img_data)
        
        doc.close()
        memory_file.seek(0)
        cleanup_files()
        return send_file(memory_file, as_attachment=True, download_name='pdf_images.zip', mimetype='application/zip')
        
    except Exception as e:
        return jsonify({'error': str(e)}), 500

if __name__ == '__main__':
    port = int(os.environ.get('PORT', 10000))
    app.run(host='0.0.0.0', port=port)