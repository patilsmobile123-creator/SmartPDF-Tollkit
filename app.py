from flask import Flask, request, send_file, jsonify, abort
from flask_cors import CORS
from werkzeug.exceptions import RequestEntityTooLarge
import os
import io
import zipfile
import tempfile
import time
import csv
import hashlib
import requests
from datetime import datetime
from functools import wraps
from pypdf import PdfReader, PdfWriter
from PIL import Image
from reportlab.lib.pagesizes import A4, letter
from reportlab.pdfgen import canvas
from reportlab.lib.utils import ImageReader
import fitz

app = Flask(__name__, static_folder='.', static_url_path='')
CORS(app)

# CRITICAL: Increase file size limit for mobile (Render max is ~100MB free tier)
app.config['MAX_CONTENT_LENGTH'] = 100 * 1024 * 1024  # 100MB

# Paths
UPLOAD_FOLDER = tempfile.mkdtemp()
os.makedirs(UPLOAD_FOLDER, exist_ok=True)
VISITOR_LOG_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'visitor_logs.csv')

# Ensure CSV exists
if not os.path.exists(VISITOR_LOG_FILE):
    with open(VISITOR_LOG_FILE, 'w', newline='', encoding='utf-8') as f:
        writer = csv.writer(f)
        writer.writerow([
            'timestamp', 'date', 'time', 'ip_address', 'anonymous_id', 
            'tool_used', 'file_count', 'file_names', 'file_sizes_mb',
            'user_agent', 'referrer', 'country', 'city', 'page_url',
            'processing_time_ms', 'success', 'error_message'
        ])

# Handle file too large error gracefully
@app.errorhandler(RequestEntityTooLarge)
def handle_file_too_large(e):
    return jsonify({
        'error': 'File too large. Maximum size is 100MB per upload. Try compressing your PDF or splitting it into smaller files.'
    }), 413

@app.errorhandler(413)
def handle_413(e):
    return jsonify({
        'error': 'File too large. Maximum size is 100MB per upload.'
    }), 413

def log_visitor(tool_name='', file_count=0, file_names='', file_sizes=0, 
                success=True, error_msg='', processing_time=0):
    """Log visitor activity to CSV"""
    try:
        now = datetime.now()
        ip = request.headers.get('X-Forwarded-For', request.remote_addr)
        if ip and ',' in ip:
            ip = ip.split(',')[0].strip()
        
        visitor_hash = hashlib.sha256(f"{ip}{request.user_agent.string if request.user_agent else 'unknown'}".encode()).hexdigest()[:16]
        
        # Simple geo lookup (free)
        country, city = 'unknown', 'unknown'
        try:
            geo_resp = requests.get(f'https://ipinfo.io/{ip}/json', timeout=1)
            if geo_resp.status_code == 200:
                geo_data = geo_resp.json()
                country = geo_data.get('country', 'unknown')
                city = geo_data.get('city', 'unknown')
        except:
            pass

        row = [
            now.isoformat(),
            now.strftime('%Y-%m-%d'),
            now.strftime('%H:%M:%S'),
            ip or 'unknown',
            visitor_hash,
            tool_name,
            file_count,
            str(file_names)[:500],
            round(file_sizes, 2),
            (request.user_agent.string[:200] if request.user_agent else 'unknown'),
            (request.referrer or 'direct'),
            country,
            city,
            request.url,
            processing_time,
            'yes' if success else 'no',
            str(error_msg)[:200] if error_msg else ''
        ]
        
        with open(VISITOR_LOG_FILE, 'a', newline='', encoding='utf-8') as f:
            writer = csv.writer(f)
            writer.writerow(row)
            
    except Exception as e:
        print(f"Logging error: {e}")

def track_usage(tool_name):
    """Decorator to track tool usage with better error handling"""
    def decorator(f):
        @wraps(f)
        def wrapper(*args, **kwargs):
            start_time = time.time()
            success = True
            error_msg = ''
            file_count = 0
            file_names = ''
            total_size = 0
            
            try:
                # Count files before processing
                files = []
                if 'files' in request.files:
                    files = request.files.getlist('files') or request.files.getlist('pdfs') or request.files.getlist('images')
                elif request.files:
                    files = list(request.files.values())
                
                file_count = len(files)
                file_names = ', '.join([f.filename for f in files if hasattr(f, 'filename')])
                
                # Calculate size without reading into memory
                total_size = 0
                for f in files:
                    if hasattr(f, 'seek') and hasattr(f, 'tell'):
                        f.seek(0, 2)  # Seek to end
                        size = f.tell()
                        f.seek(0)     # Reset
                        total_size += size / (1024*1024)  # MB
                
                # Check mobile constraints
                if total_size > 100:
                    return jsonify({
                        'error': f'Files too large ({round(total_size,1)}MB). Maximum is 100MB. Please split your PDF or use smaller images.'
                    }), 413
                
                result = f(*args, **kwargs)
                return result
                
            except Exception as e:
                success = False
                error_msg = str(e)
                # Log the error but don't crash
                print(f"Error in {tool_name}: {e}")
                return jsonify({'error': f'Server error: {str(e)}'}), 500
            finally:
                processing_time = int((time.time() - start_time) * 1000)
                log_visitor(
                    tool_name=tool_name,
                    file_count=file_count,
                    file_names=file_names,
                    file_sizes=total_size,
                    success=success,
                    error_msg=error_msg,
                    processing_time=processing_time
                )
        return wrapper
    return decorator

# Admin endpoints
@app.route('/admin/logs')
def download_logs():
    password = request.args.get('password', '')
    if password != 'your-secret-password-123':
        return jsonify({'error': 'Unauthorized'}), 401
    
    if not os.path.exists(VISITOR_LOG_FILE):
        return jsonify({'error': 'No logs found'}), 404
    
    return send_file(
        VISITOR_LOG_FILE,
        as_attachment=True,
        download_name=f'visitor_logs_{datetime.now().strftime("%Y%m%d")}.csv',
        mimetype='text/csv'
    )

@app.route('/admin/stats')
def view_stats():
    password = request.args.get('password', '')
    if password != 'your-secret-password-123':
        return jsonify({'error': 'Unauthorized'}), 401
    
    try:
        stats = {
            'total_visits': 0,
            'unique_visitors': set(),
            'tool_usage': {},
            'daily_stats': {},
            'errors': 0,
            'mobile_users': 0
        }
        
        with open(VISITOR_LOG_FILE, 'r', encoding='utf-8') as f:
            reader = csv.DictReader(f)
            for row in reader:
                stats['total_visits'] += 1
                stats['unique_visitors'].add(row['anonymous_id'])
                
                tool = row['tool_used'] or 'page_view'
                stats['tool_usage'][tool] = stats['tool_usage'].get(tool, 0) + 1
                
                date = row['date']
                if date not in stats['daily_stats']:
                    stats['daily_stats'][date] = {'visits': 0, 'unique': set()}
                stats['daily_stats'][date]['visits'] += 1
                stats['daily_stats'][date]['unique'].add(row['anonymous_id'])
                
                if row['success'] == 'no':
                    stats['errors'] += 1
                
                # Detect mobile
                ua = row.get('user_agent', '').lower()
                if any(x in ua for x in ['mobile', 'android', 'iphone', 'ipad']):
                    stats['mobile_users'] += 1
        
        stats['unique_visitors'] = len(stats['unique_visitors'])
        for date in stats['daily_stats']:
            stats['daily_stats'][date]['unique'] = len(stats['daily_stats'][date]['unique'])
        
        return jsonify(stats)
        
    except Exception as e:
        return jsonify({'error': str(e)}), 500

@app.route('/')
def home():
    log_visitor(tool_name='page_view')
    return app.send_static_file('index.html')

# 1. IMAGES TO PDF - Mobile optimized with chunking
@app.route('/api/images-to-pdf', methods=['POST'])
@track_usage('images_to_pdf')
def images_to_pdf():
    try:
        files = request.files.getlist('images')
        if not files:
            return jsonify({'error': 'No images uploaded'}), 400
        
        # Check total size first
        total_size = sum(len(f.read()) for f in files if f) / (1024*1024)
        for f in files:
            if hasattr(f, 'seek'):
                f.seek(0)
        
        if total_size > 50:  # 50MB limit for images
            return jsonify({'error': f'Total images size {round(total_size,1)}MB exceeds 50MB limit. Please upload fewer or smaller images.'}), 413
        
        page_size = request.form.get('page_size', 'A4')
        orientation = request.form.get('orientation', 'portrait')
        fit_mode = request.form.get('fit_mode', 'fit')
        
        if page_size == 'A4':
            size = A4
        else:
            size = letter
            
        if orientation == 'landscape':
            size = (size[1], size[0])
        
        output_path = os.path.join(UPLOAD_FOLDER, f"images_{int(time.time())}.pdf")
        c = canvas.Canvas(output_path, pagesize=size)
        width, height = size
        
        processed = 0
        for file in files:
            try:
                # Stream read for mobile
                img_data = file.stream.read()
                img = Image.open(io.BytesIO(img_data))
                img_width, img_height = img.size
                aspect = img_height / float(img_width) if img_width > 0 else 1
                
                if fit_mode == 'fit':
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
                    c.drawImage(ImageReader(img), 0, 0, width=width, height=height)
                else:
                    c.drawImage(ImageReader(img), 0, 0)
                
                c.showPage()
                processed += 1
                
                # Clear memory
                img_data = None
                img = None
                
            except Exception as e:
                print(f"Image error: {e}")
                continue
        
        if processed == 0:
            return jsonify({'error': 'No valid images could be processed'}), 400
            
        c.save()
        return send_file(output_path, as_attachment=True, download_name='converted_images.pdf')
        
    except Exception as e:
        return jsonify({'error': str(e)}), 500

# 2. MERGE PDFs
@app.route('/api/merge-pdf', methods=['POST'])
@track_usage('merge_pdf')
def merge_pdfs():
    try:
        files = request.files.getlist('pdfs')
        if len(files) < 2:
            return jsonify({'error': 'Please upload at least 2 PDFs'}), 400
        
        # Check size
        total_size = sum(len(f.read()) for f in files if f) / (1024*1024)
        for f in files:
            if hasattr(f, 'seek'):
                f.seek(0)
        
        if total_size > 100:
            return jsonify({'error': f'Total size {round(total_size,1)}MB exceeds 100MB limit'}), 413
        
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
        
        return send_file(output_path, as_attachment=True, download_name='merged.pdf')
        
    except Exception as e:
        return jsonify({'error': str(e)}), 500

# 3. SPLIT PDF
@app.route('/api/split-pdf', methods=['POST'])
@track_usage('split_pdf')
def split_pdf():
    try:
        file = request.files.get('pdf')
        if not file:
            return jsonify({'error': 'No PDF uploaded'}), 400
        
        # Check file size
        file_size = len(file.read()) / (1024*1024)
        file.seek(0)
        
        if file_size > 100:
            return jsonify({'error': f'File size {round(file_size,1)}MB exceeds 100MB limit'}), 413
        
        split_type = request.form.get('split_type', 'all')
        pages_str = request.form.get('pages', '')
        
        reader = PdfReader(file.stream)
        total_pages = len(reader.pages)
        
        # Limit pages for mobile
        if total_pages > 100:
            return jsonify({'error': f'PDF has {total_pages} pages. Maximum is 100 pages for mobile.'}), 413
        
        memory_file = io.BytesIO()
        
        with zipfile.ZipFile(memory_file, 'w', zipfile.ZIP_DEFLATED) as zf:
            if split_type == 'all':
                for i, page in enumerate(reader.pages):
                    writer = PdfWriter()
                    writer.add_page(page)
                    page_io = io.BytesIO()
                    writer.write(page_io)
                    page_io.seek(0)
                    zf.writestr(f'page_{i+1}.pdf', page_io.getvalue())
            else:
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
                
                for i in pages_to_extract:
                    if 0 <= i < total_pages:
                        writer = PdfWriter()
                        writer.add_page(reader.pages[i])
                        page_io = io.BytesIO()
                        writer.write(page_io)
                        page_io.seek(0)
                        zf.writestr(f'page_{i+1}.pdf', page_io.getvalue())
        
        memory_file.seek(0)
        return send_file(memory_file, as_attachment=True, download_name='split_pages.zip', mimetype='application/zip')
        
    except Exception as e:
        return jsonify({'error': str(e)}), 500

# 4. ROTATE PDF
@app.route('/api/rotate-pdf', methods=['POST'])
@track_usage('rotate_pdf')
def rotate_pdf():
    try:
        file = request.files.get('pdf')
        if not file:
            return jsonify({'error': 'No PDF uploaded'}), 400
        
        file_size = len(file.read()) / (1024*1024)
        file.seek(0)
        
        if file_size > 100:
            return jsonify({'error': f'File size {round(file_size,1)}MB exceeds 100MB limit'}), 413
        
        angle = int(request.form.get('angle', 90))
        
        reader = PdfReader(file.stream)
        writer = PdfWriter()
        
        for page in reader.pages:
            page.rotate(angle)
            writer.add_page(page)
        
        output_path = os.path.join(UPLOAD_FOLDER, f"rotated_{int(time.time())}.pdf")
        with open(output_path, 'wb') as f:
            writer.write(f)
        
        return send_file(output_path, as_attachment=True, download_name='rotated.pdf')
        
    except Exception as e:
        return jsonify({'error': str(e)}), 500

# 5. COMPRESS PDF
@app.route('/api/compress-pdf', methods=['POST'])
@track_usage('compress_pdf')
def compress_pdf():
    try:
        file = request.files.get('pdf')
        if not file:
            return jsonify({'error': 'No PDF uploaded'}), 400
        
        file_content = file.read()
        original_size = len(file_content)
        original_size_mb = original_size / (1024*1024)
        
        if original_size_mb > 100:
            return jsonify({'error': f'File size {round(original_size_mb,1)}MB exceeds 100MB limit'}), 413
        
        reader = PdfReader(io.BytesIO(file_content))
        writer = PdfWriter()
        
        for page in reader.pages:
            writer.add_page(page)
        
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
        
        return response
        
    except Exception as e:
        return jsonify({'error': str(e)}), 500

# 6. PDF INFO
@app.route('/api/pdf-info', methods=['POST'])
@track_usage('pdf_info')
def pdf_info():
    try:
        file = request.files.get('pdf')
        if not file:
            return jsonify({'error': 'No PDF uploaded'}), 400
        
        file_content = file.read()
        file_size_mb = len(file_content) / (1024*1024)
        
        if file_size_mb > 100:
            return jsonify({'error': f'File size {round(file_size_mb,1)}MB exceeds 100MB limit'}), 413
        
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
        
        if reader.metadata:
            meta = reader.metadata
            info['author'] = str(meta.get('/Author', '')) if meta.get('/Author') else ''
            info['title'] = str(meta.get('/Title', '')) if meta.get('/Title') else ''
            info['creator'] = str(meta.get('/Creator', '')) if meta.get('/Creator') else ''
        
        if reader.pages:
            try:
                box = reader.pages[0].mediabox
                info['width_mm'] = round(float(box.width) * 0.352777, 1)
                info['height_mm'] = round(float(box.height) * 0.352777, 1)
            except:
                pass
        
        return jsonify(info)
        
    except Exception as e:
        return jsonify({'error': str(e)}), 500

# 7. PASSWORD PDF
@app.route('/api/password-pdf', methods=['POST'])
@track_usage('password_pdf')
def password_pdf():
    try:
        file = request.files.get('pdf')
        if not file:
            return jsonify({'error': 'No PDF uploaded'}), 400
        
        password = request.form.get('password', '')
        action = request.form.get('action', 'encrypt')
        
        if not password:
            return jsonify({'error': 'Password is required'}), 400
        
        file_size = len(file.read()) / (1024*1024)
        file.seek(0)
        
        if file_size > 100:
            return jsonify({'error': f'File size {round(file_size,1)}MB exceeds 100MB limit'}), 413
        
        reader = PdfReader(file.stream)
        writer = PdfWriter()
        
        for page in reader.pages:
            writer.add_page(page)
        
        output_path = os.path.join(UPLOAD_FOLDER, f"password_{int(time.time())}.pdf")
        
        if action == 'encrypt':
            writer.encrypt(password)
            with open(output_path, 'wb') as f:
                writer.write(f)
            return send_file(output_path, as_attachment=True, download_name='protected.pdf')
        else:
            if reader.is_encrypted:
                try:
                    reader.decrypt(password)
                    writer = PdfWriter()
                    for page in reader.pages:
                        writer.add_page(page)
                except:
                    return jsonify({'error': 'Incorrect password'}), 400
            
            with open(output_path, 'wb') as f:
                writer.write(f)
            return send_file(output_path, as_attachment=True, download_name='decrypted.pdf')
            
    except Exception as e:
        return jsonify({'error': str(e)}), 500

# 8. PDF TO IMAGES - Limited for mobile
@app.route('/api/pdf-to-images', methods=['POST'])
@track_usage('pdf_to_images')
def pdf_to_images():
    try:
        file = request.files.get('pdf')
        if not file:
            return jsonify({'error': 'No PDF uploaded'}), 400
        
        file_size = len(file.read()) / (1024*1024)
        file.seek(0)
        
        if file_size > 50:  # Lower limit for image conversion (memory intensive)
            return jsonify({'error': f'File size {round(file_size,1)}MB exceeds 50MB limit for image conversion. Try compress first.'}), 413
        
        fmt = request.form.get('format', 'JPEG')
        dpi = int(request.form.get('dpi', 150))
        pages_str = request.form.get('pages', 'all')
        
        # Limit DPI on mobile
        if dpi > 200:
            dpi = 200  # Cap at 200 for mobile
        
        pdf_bytes = file.read()
        doc = fitz.open(stream=pdf_bytes, filetype="pdf")
        
        # Limit pages
        if len(doc) > 50:
            doc.close()
            return jsonify({'error': f'PDF has {len(doc)} pages. Maximum is 50 pages for mobile conversion.'}), 413
        
        memory_file = io.BytesIO()
        
        with zipfile.ZipFile(memory_file, 'w', zipfile.ZIP_DEFLATED) as zf:
            if pages_str == 'all':
                page_nums = range(len(doc))
            else:
                try:
                    if '-' in pages_str:
                        start, end = map(int, pages_str.split('-'))
                        page_nums = range(start-1, end)
                    else:
                        page_nums = [int(p.strip())-1 for p in pages_str.split(',')]
                except:
                    doc.close()
                    return jsonify({'error': 'Invalid page range. Use: 1-5 or 1,3,5'}), 400
            
            for i in page_nums:
                if 0 <= i < len(doc):
                    page = doc.load_page(i)
                    mat = fitz.Matrix(dpi/72, dpi/72)
                    pix = page.get_pixmap(matrix=mat)
                    
                    if fmt == 'PNG':
                        img_data = pix.tobytes("png")
                        ext = 'png'
                    else:
                        img_data = pix.tobytes("jpeg")
                        ext = 'jpg'
                    
                    zf.writestr(f'page_{i+1}.{ext}', img_data)
        
        doc.close()
        memory_file.seek(0)
        return send_file(memory_file, as_attachment=True, download_name='pdf_images.zip', mimetype='application/zip')
        
    except Exception as e:
        return jsonify({'error': str(e)}), 500

if __name__ == '__main__':
    port = int(os.environ.get('PORT', 10000))
    app.run(host='0.0.0.0', port=port)
