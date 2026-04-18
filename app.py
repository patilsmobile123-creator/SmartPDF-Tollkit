from flask import Flask, request, send_file, jsonify, g
from flask_cors import CORS
import os
import io
import zipfile
import tempfile
import time
import csv
import json
from datetime import datetime
from functools import wraps
from pypdf import PdfReader, PdfWriter
from PIL import Image
from reportlab.lib.pagesizes import A4, letter
from reportlab.pdfgen import canvas
from reportlab.lib.utils import ImageReader
import fitz  # PyMuPDF
import uuid
import hashlib

app = Flask(__name__, static_folder='.', static_url_path='')
CORS(app)

# Paths
UPLOAD_FOLDER = tempfile.mkdtemp()
os.makedirs(UPLOAD_FOLDER, exist_ok=True)

# CSV file for visitor logs
VISITOR_LOG_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'visitor_logs.csv')

# Ensure CSV exists with headers
if not os.path.exists(VISITOR_LOG_FILE):
    with open(VISITOR_LOG_FILE, 'w', newline='', encoding='utf-8') as f:
        writer = csv.writer(f)
        writer.writerow([
            'timestamp', 'date', 'time', 'ip_address', 'anonymous_id', 
            'tool_used', 'file_count', 'file_names', 'file_sizes_mb',
            'user_agent', 'referrer', 'country', 'city', 'page_url',
            'processing_time_ms', 'success', 'error_message'
        ])

# Simple in-memory rate limiting (resets on restart)
request_times = {}

def log_visitor(tool_name='', file_count=0, file_names='', file_sizes=0, 
                success=True, error_msg='', processing_time=0):
    """Log visitor activity to CSV"""
    try:
        now = datetime.now()
        
        # Get IP (handle proxies)
        ip = request.headers.get('X-Forwarded-For', request.remote_addr)
        if ip and ',' in ip:
            ip = ip.split(',')[0].strip()
        
        # Generate anonymous visitor ID (hashed IP + user agent)
        visitor_hash = hashlib.sha256(f"{ip}{request.user_agent.string}".encode()).hexdigest()[:16]
        
        # Get location from IP (basic free approach)
        country, city = get_location_from_ip(ip)
        
        row = [
            now.isoformat(),
            now.strftime('%Y-%m-%d'),
            now.strftime('%H:%M:%S'),
            ip or 'unknown',
            visitor_hash,
            tool_name,
            file_count,
            file_names[:500],  # Truncate long filenames
            round(file_sizes, 2),
            request.user_agent.string[:200] if request.user_agent else 'unknown',
            request.referrer or 'direct',
            country,
            city,
            request.url,
            processing_time,
            'yes' if success else 'no',
            error_msg[:200] if error_msg else ''
        ]
        
        with open(VISITOR_LOG_FILE, 'a', newline='', encoding='utf-8') as f:
            writer = csv.writer(f)
            writer.writerow(row)
            
    except Exception as e:
        print(f"Logging error: {e}")

def get_location_from_ip(ip):
    """Basic IP geolocation (optional - requires ipinfo.io token for accuracy)"""
    try:
        # Free tier - returns rough location or unknown
        # For accurate data, add ipinfo.io API token
        import requests
        response = requests.get(f'https://ipinfo.io/{ip}/json', timeout=2)
        if response.status_code == 200:
            data = response.json()
            return data.get('country', 'unknown'), data.get('city', 'unknown')
    except:
        pass
    return 'unknown', 'unknown'

def track_usage(tool_name):
    """Decorator to track tool usage"""
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
                if 'files' in request.files:
                    files = request.files.getlist('files') or request.files.getlist('pdfs') or request.files.getlist('images')
                    file_count = len(files)
                    file_names = ', '.join([f.filename for f in files if f])
                    total_size = sum([len(f.read()) for f in files if f]) / (1024*1024)  # MB
                    # Reset file pointers
                    for f in files:
                        f.seek(0)
                elif request.files:
                    file_count = len(request.files)
                    files = list(request.files.values())
                    file_names = ', '.join([f.filename for f in files if f])
                    total_size = sum([len(f.read()) for f in files if f]) / (1024*1024)
                    for f in files:
                        f.seek(0)
                        
                result = f(*args, **kwargs)
                return result
                
            except Exception as e:
                success = False
                error_msg = str(e)
                raise e
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

# ADMIN ENDPOINT - Download CSV logs (add password protection!)
@app.route('/admin/logs')
def download_logs():
    """Download visitor logs as CSV (PROTECT THIS IN PRODUCTION!)"""
    password = request.args.get('password', '')
    
    # Simple password protection - CHANGE THIS!
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

# ADMIN ENDPOINT - View stats JSON
@app.route('/admin/stats')
def view_stats():
    """View usage statistics (PROTECT THIS!)"""
    password = request.args.get('password', '')
    if password != 'your-secret-password-123':
        return jsonify({'error': 'Unauthorized'}), 401
    
    try:
        stats = {
            'total_visits': 0,
            'unique_visitors': set(),
            'tool_usage': {},
            'daily_stats': {},
            'errors': 0
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
        
        # Convert sets to counts for JSON serialization
        stats['unique_visitors'] = len(stats['unique_visitors'])
        for date in stats['daily_stats']:
            stats['daily_stats'][date]['unique'] = len(stats['daily_stats'][date]['unique'])
        
        return jsonify(stats)
        
    except Exception as e:
        return jsonify({'error': str(e)}), 500

# Track page views
@app.route('/')
def home():
    log_visitor(tool_name='page_view')
    return app.send_static_file('index.html')

# 1. IMAGES TO PDF
@app.route('/api/images-to-pdf', methods=['POST'])
@track_usage('images_to_pdf')
def images_to_pdf():
    try:
        files = request.files.getlist('images')
        if not files:
            return jsonify({'error': 'No images uploaded'}), 400
        
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
        
        for file in files:
            try:
                img = Image.open(file.stream)
                img_width, img_height = img.size
                aspect = img_height / float(img_width)
                
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
            except Exception as e:
                return jsonify({'error': f'Error processing image: {str(e)}'}), 400
        
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
        
        split_type = request.form.get('split_type', 'all')
        pages_str = request.form.get('pages', '')
        
        reader = PdfReader(file.stream)
        total_pages = len(reader.pages)
        
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

# 8. PDF TO IMAGES
@app.route('/api/pdf-to-images', methods=['POST'])
@track_usage('pdf_to_images')
def pdf_to_images():
    try:
        file = request.files.get('pdf')
        if not file:
            return jsonify({'error': 'No PDF uploaded'}), 400
        
        fmt = request.form.get('format', 'JPEG')
        dpi = int(request.form.get('dpi', 150))
        pages_str = request.form.get('pages', 'all')
        
        pdf_bytes = file.read()
        doc = fitz.open(stream=pdf_bytes, filetype="pdf")
        
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
