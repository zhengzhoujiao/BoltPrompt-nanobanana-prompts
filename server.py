"""
本地提示词库 HTTP 服务（含 POST 上传、PUT 保存 JSON）。

请在本目录执行:  python server.py

不要使用:  python -m http.server
内置静态服务器不支持 POST，会出现 501 Unsupported method ('POST')。
"""
import os
import re
import json
import shutil
import tempfile
import mimetypes
from socketserver import ThreadingMixIn
from http.server import HTTPServer, BaseHTTPRequestHandler
from urllib.parse import urlparse, parse_qs, unquote

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
PROMPT_JSON = os.path.join(BASE_DIR, 'prompt_local.json')
MAX_PUT_BYTES = 100 * 1024 * 1024
MAX_META_PUT_BYTES = 20 * 1024 * 1024
MAX_UPLOAD_BYTES = 25 * 1024 * 1024
_SAFE_NAME = re.compile(r"^[a-zA-Z0-9._\- \u4e00-\u9fff]+\.[a-zA-Z0-9]{1,8}$")
_ALLOWED_UPLOAD_EXT = {".webp", ".jpg", ".jpeg", ".png", ".gif", ".bmp", ".avif"}


def _multipart_boundary(content_type: str) -> bytes | None:
    for segment in content_type.split(';'):
        segment = segment.strip()
        low = segment.lower()
        if low.startswith('boundary='):
            raw = segment.split('=', 1)[1].strip().strip('"')
            return raw.encode('latin-1')
    return None


def _parse_upload_multipart(content_type: str, body: bytes):
    """解析 multipart/form-data，返回 (dir 相对路径, 原始文件名, 文件二进制) 或 (None, None, None)。"""
    boundary = _multipart_boundary(content_type)
    if not boundary:
        return None, None, None
    delim = b'--' + boundary
    parts = body.split(delim)
    dir_val = None
    upload_name = None
    upload_bytes = None
    for seg in parts:
        seg = seg.lstrip(b'\r\n')
        if not seg or seg == b'--':
            continue
        sep = b'\r\n\r\n'
        nl = seg.find(sep)
        if nl < 0:
            sep = b'\n\n'
            nl = seg.find(sep)
            if nl < 0:
                continue
            header_blob = seg[:nl].decode('utf-8', 'replace')
            step = 2
        else:
            header_blob = seg[:nl].decode('utf-8', 'replace')
            step = 4
        content = seg[nl + step :]
        name_m = re.search(r'\bname="([^"]+)"', header_blob, re.I)
        if not name_m:
            name_m = re.search(r'\bname=([^;\r\n]+)', header_blob, re.I)
        if not name_m:
            continue
        field = name_m.group(1).strip().strip('"')
        if field == 'dir':
            dir_val = content.decode('utf-8').strip()
        elif field == 'file':
            fn_m = re.search(r'\bfilename="([^"]*)"', header_blob, re.I)
            if not fn_m:
                fn_m = re.search(r'\bfilename=([^\s;\r\n]+)', header_blob, re.I)
            raw_fn = (fn_m.group(1).strip().strip('"') if fn_m else '') or 'upload.bin'
            upload_name = raw_fn
            c = content
            if len(c) >= 2 and c.endswith(b'\r\n'):
                c = c[:-2]
            upload_bytes = c
    if dir_val is None or upload_bytes is None:
        return None, None, None
    return dir_val, upload_name, upload_bytes


class ThreadingHTTPServer(ThreadingMixIn, HTTPServer):
    daemon_threads = True


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        parsed = urlparse(self.path)
        # 兼容本地直接访问和 nginx /prompt/ 子路径两种场景
        path = parsed.path.removeprefix('/prompt')
        if not path:
            path = '/'

        if path in ('/', '/index.html'):
            self._serve_file(os.path.join(BASE_DIR, 'index.html'), 'text/html')
        elif path in ('/manage', '/manage.html'):
            self._serve_file(os.path.join(BASE_DIR, 'manage.html'), 'text/html')
        elif path == '/api/data':
            self._serve_json()
        elif path == '/image':
            params = parse_qs(parsed.query)
            img_path = unquote(params.get('path', [''])[0])
            self._serve_image(img_path)
        elif path == '/meta':
            params = parse_qs(parsed.query)
            meta_path = unquote(params.get('path', [''])[0])
            self._serve_meta(meta_path)
        elif path == '/logo.png':
            self._serve_logo_png()
        else:
            self.send_response(404)
            self.end_headers()

    def do_DELETE(self):
        parsed = urlparse(self.path)
        path = parsed.path.removeprefix('/prompt')
        if path != '/api/file':
            self.send_response(404)
            self.end_headers()
            return
        params = parse_qs(parsed.query)
        rel = unquote(params.get('path', [''])[0])
        abs_path = os.path.normpath(os.path.join(BASE_DIR, rel.replace('/', os.sep)))
        if not abs_path.startswith(BASE_DIR):
            self._json_error(403, '路径非法')
            return
        if os.path.isdir(abs_path) or not os.path.isfile(abs_path):
            self._json_error(404, '文件不存在')
            return
        try:
            os.remove(abs_path)
        except OSError as e:
            self._json_error(500, str(e))
            return
        self.send_response(204)
        self.end_headers()

    def do_POST(self):
        parsed = urlparse(self.path)
        path = parsed.path.removeprefix('/prompt')
        if path != '/api/upload':
            self.send_response(404)
            self.end_headers()
            return
        try:
            self._handle_upload()
        except Exception as e:
            self._json_error(500, f'上传处理异常: {e}')

    def do_OPTIONS(self):
        self.send_response(204)
        self.send_header('Allow', 'GET, HEAD, POST, PUT, DELETE, OPTIONS')
        self.end_headers()

    def _handle_upload(self):
        try:
            length = int(self.headers.get('Content-Length', '0'))
        except ValueError:
            self._json_error(400, 'Invalid Content-Length')
            return
        if length <= 0 or length > MAX_UPLOAD_BYTES:
            self._json_error(400, '文件过大或为空')
            return
        ctype = self.headers.get('Content-Type', '')
        if 'multipart/form-data' not in ctype:
            self._json_error(400, '需要 multipart/form-data')
            return
        raw = self.rfile.read(length)
        if len(raw) != length:
            self._json_error(400, '请求体不完整')
            return
        dir_raw, raw_fn, file_bytes = _parse_upload_multipart(ctype, raw)
        if dir_raw is None or file_bytes is None:
            self._json_error(400, '缺少 dir 或 file 字段')
            return
        if not isinstance(dir_raw, str) or not dir_raw.strip():
            self._json_error(400, 'dir 无效')
            return
        dir_rel = dir_raw.strip().replace('\\', '/').strip('/')
        target_dir = os.path.normpath(os.path.join(BASE_DIR, dir_rel.replace('/', os.sep)))
        if not target_dir.startswith(BASE_DIR):
            self._json_error(403, '目录非法')
            return
        orig_name = os.path.basename((raw_fn or '').replace('\\', '/'))
        if not orig_name or orig_name in ('.', '..'):
            self._json_error(400, '文件名非法')
            return
        ext = os.path.splitext(orig_name)[1].lower()
        if ext not in _ALLOWED_UPLOAD_EXT:
            self._json_error(400, f'不支持的扩展名: {ext}')
            return
        if not _SAFE_NAME.match(orig_name):
            self._json_error(400, '文件名仅允许字母数字、中文、._- 及常见后缀')
            return
        dest = os.path.join(target_dir, orig_name)
        if not dest.startswith(BASE_DIR):
            self._json_error(403, '路径非法')
            return
        try:
            os.makedirs(target_dir, exist_ok=True)
        except OSError as e:
            self._json_error(500, f'无法创建目录: {e}')
            return
        try:
            with open(dest, 'wb') as out:
                out.write(file_bytes)
        except OSError as e:
            self._json_error(500, f'写入失败: {e}')
            return
        body = json.dumps({'filename': orig_name}, ensure_ascii=False).encode('utf-8')
        self.send_response(200)
        self.send_header('Content-Type', 'application/json; charset=utf-8')
        self.send_header('Content-Length', str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_PUT(self):
        parsed = urlparse(self.path)
        path = parsed.path.removeprefix('/prompt')
        if path == '/api/meta':
            self._put_meta(parsed)
            return
        if path != '/api/data':
            self.send_response(404)
            self.end_headers()
            return
        try:
            length = int(self.headers.get('Content-Length', '0'))
        except ValueError:
            self._json_error(400, 'Invalid Content-Length')
            return
        if length <= 0 or length > MAX_PUT_BYTES:
            self._json_error(400, 'Body too large or empty')
            return
        raw = self.rfile.read(length)
        try:
            text = raw.decode('utf-8')
            payload = json.loads(text)
        except (UnicodeDecodeError, json.JSONDecodeError) as e:
            self._json_error(400, f'Invalid JSON: {e}')
            return
        err = self._validate_prompt_data(payload)
        if err:
            self._json_error(400, err)
            return
        try:
            self._atomic_write_json(PROMPT_JSON, payload)
        except OSError as e:
            self._json_error(500, f'Write failed: {e}')
            return
        self.send_response(204)
        self.end_headers()

    def _put_meta(self, parsed):
        params = parse_qs(parsed.query)
        meta_rel = unquote(params.get('path', [''])[0]).strip()
        if not meta_rel:
            self._json_error(400, '缺少 path 参数')
            return
        meta_rel = meta_rel.replace('\\', '/')
        if not meta_rel.endswith('.json'):
            self._json_error(400, 'path 须为 .json 文件')
            return
        try:
            length = int(self.headers.get('Content-Length', '0'))
        except ValueError:
            self._json_error(400, 'Invalid Content-Length')
            return
        if length <= 0 or length > MAX_META_PUT_BYTES:
            self._json_error(400, 'Body 无效')
            return
        raw = self.rfile.read(length)
        try:
            text = raw.decode('utf-8')
            payload = json.loads(text)
        except (UnicodeDecodeError, json.JSONDecodeError) as e:
            self._json_error(400, f'Invalid JSON: {e}')
            return
        if not isinstance(payload, dict):
            self._json_error(400, 'meta 须为 JSON 对象')
            return
        abs_path = os.path.normpath(os.path.join(BASE_DIR, meta_rel.replace('/', os.sep)))
        if not abs_path.startswith(BASE_DIR):
            self._json_error(403, '路径非法')
            return
        err = self._validate_meta_object(payload)
        if err:
            self._json_error(400, err)
            return
        try:
            parent = os.path.dirname(abs_path)
            if parent and not os.path.isdir(parent):
                os.makedirs(parent, exist_ok=True)
            self._atomic_write_json(abs_path, payload)
        except OSError as e:
            self._json_error(500, f'Write failed: {e}')
            return
        self.send_response(204)
        self.end_headers()

    def _validate_meta_object(self, m):
        if 'uuid' not in m or not isinstance(m.get('uuid'), str) or not str(m['uuid']).strip():
            return 'meta 缺少 uuid'
        for key in ('title', 'author', 'prompt_origin', 'prompt_cn'):
            if key in m and not isinstance(m[key], str):
                return f'meta.{key} 须为字符串'
        imgs = m.get('imgs')
        if imgs is not None and not isinstance(imgs, list):
            return 'meta.imgs 须为数组'
        if isinstance(imgs, list):
            for i, x in enumerate(imgs):
                if not isinstance(x, str) or not x.strip():
                    return f'meta.imgs[{i}] 无效'
        return None

    def _json_error(self, code, message):
        body = json.dumps({'error': message}, ensure_ascii=False).encode('utf-8')
        self.send_response(code)
        self.send_header('Content-Type', 'application/json; charset=utf-8')
        self.send_header('Content-Length', str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _validate_prompt_data(self, data):
        if not isinstance(data, list):
            return '根节点必须是数组（分类列表）'
        for i, cat in enumerate(data):
            if not isinstance(cat, dict):
                return f'分类 #{i} 必须是对象'
            if 'name' not in cat or not isinstance(cat.get('name'), str):
                return f'分类 #{i} 缺少字符串字段 name'
            projects = cat.get('projects')
            if not isinstance(projects, list):
                return f'分类「{cat["name"]}」的 projects 必须是数组'
            for j, p in enumerate(projects):
                if not isinstance(p, dict):
                    return f'分类「{cat["name"]}」第 {j} 条提示词必须是对象'
                if 'uuid' not in p or not isinstance(p.get('uuid'), str) or not p['uuid'].strip():
                    return f'分类「{cat["name"]}」第 {j} 条缺少有效 uuid'
        return None

    def _atomic_write_json(self, filepath, obj):
        dir_name = os.path.dirname(filepath) or '.'
        fd, tmp_path = tempfile.mkstemp(suffix='.json.tmp', dir=dir_name, text=True)
        try:
            with os.fdopen(fd, 'w', encoding='utf-8') as f:
                json.dump(obj, f, ensure_ascii=False, indent=2)
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp_path, filepath)
        except BaseException:
            try:
                os.unlink(tmp_path)
            except OSError:
                pass
            raise

    def _serve_logo_png(self):
        fp = os.path.join(BASE_DIR, 'logo.png')
        try:
            with open(fp, 'rb') as f:
                data = f.read()
            self.send_response(200)
            self.send_header('Content-Type', 'image/png')
            self.send_header('Cache-Control', 'public, max-age=86400')
            self.end_headers()
            self.wfile.write(data)
        except FileNotFoundError:
            self.send_response(404)
            self.end_headers()

    def _serve_file(self, filepath, content_type):
        try:
            with open(filepath, 'rb') as f:
                data = f.read()
            self.send_response(200)
            self.send_header('Content-Type', f'{content_type}; charset=utf-8')
            self.end_headers()
            self.wfile.write(data)
        except FileNotFoundError:
            self.send_response(404)
            self.end_headers()

    def _serve_json(self):
        json_path = PROMPT_JSON
        with open(json_path, 'r', encoding='utf-8') as f:
            data = f.read()
        self.send_response(200)
        self.send_header('Content-Type', 'application/json; charset=utf-8')
        self.end_headers()
        self.wfile.write(data.encode('utf-8'))

    def _serve_image(self, img_path):
        # img_path 是相对路径，基于 BASE_DIR 解析
        abs_path = os.path.normpath(os.path.join(BASE_DIR, img_path))
        # 防止路径穿越
        if not abs_path.startswith(BASE_DIR):
            self.send_response(403)
            self.end_headers()
            return
        if not os.path.exists(abs_path):
            self.send_response(404)
            self.end_headers()
            return
        mime_type, _ = mimetypes.guess_type(abs_path)
        with open(abs_path, 'rb') as f:
            data = f.read()
        self.send_response(200)
        self.send_header('Content-Type', mime_type or 'application/octet-stream')
        self.send_header('Cache-Control', 'no-store, max-age=0')
        self.end_headers()
        self.wfile.write(data)

    def _serve_meta(self, meta_path):
        abs_path = os.path.normpath(os.path.join(BASE_DIR, meta_path))
        if not abs_path.startswith(BASE_DIR):
            self.send_response(403)
            self.end_headers()
            return
        if not os.path.exists(abs_path):
            self.send_response(404)
            self.end_headers()
            return
        with open(abs_path, 'r', encoding='utf-8') as f:
            data = f.read()
        self.send_response(200)
        self.send_header('Content-Type', 'application/json; charset=utf-8')
        self.end_headers()
        self.wfile.write(data.encode('utf-8'))

    def log_message(self, format, *args):
        pass  # suppress request logs


if __name__ == '__main__':
    port = 8080
    if not hasattr(Handler, 'do_POST'):
        raise RuntimeError('Handler 缺少 do_POST，请勿使用被裁剪过的 server.py')
    server = ThreadingHTTPServer(('0.0.0.0', port), Handler)
    print(f'  http://localhost:{port}')
    server.serve_forever()
