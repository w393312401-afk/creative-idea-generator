"""Static HTTP delivery; independent of generation state and service credentials."""
import os
import re
from urllib.parse import parse_qs, unquote, urlsplit

from .assets import content_version, is_frontend_asset, rewrite_html_assets
from .compression import gzip_body, accepts_gzip, compress_response


class StaticFilesMixin:
    _MIME_TYPES = {
        '.webp': 'image/webp',
        '.mp4': 'video/mp4',
        '.webm': 'video/webm',
        '.wav': 'audio/wav',
        '.mp3': 'audio/mpeg',
        '.ogg': 'audio/ogg',
        '.json': 'application/json',
        '.js': 'application/javascript',
        '.css': 'text/css',
        '.html': 'text/html; charset=utf-8',
        '.svg': 'image/svg+xml',
        '.png': 'image/png',
        '.jpg': 'image/jpeg',
        '.jpeg': 'image/jpeg',
    }

    def _serve_static_file(self, is_head=False):
        """静态文件服务，支持 HTTP 206 Partial Content (Range)、If-Modified-Since 与流式分片传输。"""
        # A HTTP/1.1 handler is reused across requests. A previous asset's policy
        # must never become the next response's cache policy.
        self._spark_static_cache_control = 'no-store'
        full_path = self.translate_path(self.path)
        if os.path.isdir(full_path):
            index_path = os.path.join(full_path, "index.html")
            if os.path.isfile(index_path):
                full_path = index_path
            else:
                if is_head:
                    super().do_HEAD()
                else:
                    super().do_GET()
                return

        if not os.path.isfile(full_path):
            self.send_error(404, "File not found")
            return

        ext = os.path.splitext(full_path)[1].lower()
        ctype = self._MIME_TYPES.get(ext) or self.guess_type(full_path) or 'application/octet-stream'

        try:
            f = open(full_path, 'rb')
        except OSError:
            self.send_error(404, "File not found")
            return

        try:
            fs = os.fstat(f.fileno())
            file_size = fs.st_size
            last_modified = self.date_time_string(fs.st_mtime)
            if ext in {'.html', '.css', '.js'}:
                self._serve_text_asset(f, fs, ext, is_head)
                return
            generated_media = (unquote(urlsplit(self.path).path).startswith('/outputs/')
                               and ctype.startswith(('image/', 'video/', 'audio/')))

            # Workers can create the output before filling it. An empty 200 becomes
            # a broken cached image that second-resolution Last-Modified cannot
            # distinguish from the completed file written within that same second.
            if generated_media and file_size == 0:
                f.close()
                self.send_error(404, "Media file is not ready")
                return

            compressible = ext in {'.html', '.css', '.js', '.svg'} and 1024 <= file_size <= 2 * 1024 * 1024

            # If-Modified-Since 校验（仅在无 Range 且未被缓存控制压制时）
            ims = self.headers.get('If-Modified-Since') if hasattr(self, 'headers') else None
            inm = self.headers.get('If-None-Match') if hasattr(self, 'headers') else None
            range_header = self.headers.get('Range') if hasattr(self, 'headers') else None
            etag = None
            if generated_media:
                version = f'{fs.st_mtime_ns:x}-{file_size:x}'
                if compressible:
                    # SVGs are images too; a strong tag must identify the selected
                    # representation so identity and gzip cannot share a 304.
                    gzipped = not range_header and accepts_gzip(self.headers.get('Accept-Encoding', ''))
                    version += '-gzip' if gzipped else '-identity'
                etag = f'"{version}"'
            if etag and inm is not None and not range_header:
                # GET/HEAD use weak comparison, including weak tags in a list.
                tags = [tag.strip() for tag in inm.split(',')]
                if '*' in tags or any(tag.removeprefix('W/') == etag for tag in tags):
                    f.close()
                    self.send_response(304, "Not Modified")
                    self.send_header('ETag', etag)
                    self.send_header('Last-Modified', last_modified)
                    if compressible:
                        self.send_header('Vary', 'Accept-Encoding')
                    self.end_headers()
                    return
            # Output media require the precise validator: ignore IMS-only clients
            # so legacy cached empty/truncated responses are replaced once.
            if ims and inm is None and not generated_media and not range_header:
                try:
                    from email.utils import parsedate_to_datetime
                    import datetime
                    ims_dt = parsedate_to_datetime(ims)
                    mtime_dt = datetime.datetime.fromtimestamp(int(fs.st_mtime), datetime.timezone.utc)
                    if mtime_dt <= ims_dt:
                        f.close()
                        self.send_response(304, "Not Modified")
                        if compressible:
                            self.send_header('Vary', 'Accept-Encoding')
                        self.end_headers()
                        return
                except Exception:
                    pass

            # Range 请求解析 (HTTP 206 Partial Content)
            if range_header and range_header.strip().startswith('bytes='):
                range_str = range_header.strip()[6:].strip()
                range_match = re.match(r'^(\d*)-(\d*)$', range_str)
                if range_match:
                    raw_start, raw_end = range_match.groups()
                    if raw_start and raw_end:
                        start = int(raw_start)
                        end = int(raw_end)
                    elif raw_start and not raw_end:
                        start = int(raw_start)
                        end = file_size - 1
                    elif not raw_start and raw_end:
                        suffix = int(raw_end)
                        if suffix == 0:
                            start = file_size
                            end = file_size - 1
                        else:
                            start = max(0, file_size - suffix)
                            end = file_size - 1
                    else:
                        start, end = 0, file_size - 1

                    if file_size == 0 or start < 0 or start > end or start >= file_size:
                        f.close()
                        self.send_response(416, "Range Not Satisfiable")
                        self.send_header("Content-Range", f"bytes */{file_size}")
                        self.end_headers()
                        return

                    end = min(end, file_size - 1)
                    content_length = end - start + 1

                    self.send_response(206, "Partial Content")
                    self.send_header("Content-Type", ctype)
                    self.send_header("Content-Range", f"bytes {start}-{end}/{file_size}")
                    self.send_header("Content-Length", str(content_length))
                    self.send_header("Accept-Ranges", "bytes")
                    self.send_header("Last-Modified", last_modified)
                    if etag:
                        self.send_header('ETag', etag)
                    if compressible:
                        self.send_header('Vary', 'Accept-Encoding')
                    self.end_headers()

                    if not is_head:
                        try:
                            f.seek(start)
                            remaining = content_length
                            chunk_size = 64 * 1024
                            while remaining > 0:
                                to_read = min(chunk_size, remaining)
                                chunk = f.read(to_read)
                                if not chunk:
                                    break
                                self.wfile.write(chunk)
                                remaining -= len(chunk)
                        except (ConnectionError, BrokenPipeError):
                            self.close_connection = True
                        finally:
                            f.close()
                    else:
                        f.close()
                    return

            encoded = None
            if compressible and not range_header and accepts_gzip(self.headers.get('Accept-Encoding', '')):
                encoded = gzip_body(f, full_path, fs)

            # 无 Range 请求：200 OK 全量响应
            self.send_response(200, "OK")
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(encoded) if encoded is not None else file_size))
            if compressible:
                self.send_header('Vary', 'Accept-Encoding')
            if encoded is not None:
                self.send_header('Content-Encoding', 'gzip')
            self.send_header("Accept-Ranges", "bytes")
            self.send_header("Last-Modified", last_modified)
            if etag:
                self.send_header('ETag', etag)
            self.end_headers()

            if not is_head:
                try:
                    if encoded is not None:
                        self.wfile.write(encoded)
                        return
                    chunk_size = 64 * 1024
                    while True:
                        chunk = f.read(chunk_size)
                        if not chunk:
                            break
                        self.wfile.write(chunk)
                except (ConnectionError, BrokenPipeError):
                    self.close_connection = True
                finally:
                    f.close()
            else:
                f.close()

        except Exception as e:
            try:
                f.close()
            except Exception:
                pass
            if not getattr(self, '_spark_status_code', None):
                self.send_error(500, f"Internal server error: {e}")

    def _html_asset_digest(self, path):
        if not is_frontend_asset(path) or not self._static_path_allowed(path):
            return None
        root = os.path.realpath(self.translate_path('/'))
        target = os.path.realpath(self.translate_path(path))
        try:
            if os.path.commonpath((root, target)) != root:
                return None
            actual_path = '/' + os.path.relpath(target, root).replace(os.sep, '/')
            if not self._static_path_allowed(actual_path):
                return None
            with open(target, 'rb') as stream:
                return content_version(stream.read())
        except (OSError, ValueError):
            return None

    def _serve_text_asset(self, stream, stat, ext, is_head):
        # Hash and send one snapshot, including Range requests. Metadata-only
        # validators can miss same-size rewrites with a preserved timestamp.
        try:
            raw = stream.read()
        finally:
            stream.close()
        parsed = urlsplit(self.path)
        path = unquote(parsed.path)
        frontend = ext in {'.js', '.css'} and is_frontend_asset(path)
        if ext == '.html' and not path.startswith(('/api/', '/outputs/', '/shared/')):
            host = self.headers.get('Host') or 'static.invalid'
            raw = rewrite_html_assets(raw, 'http://' + host + self.path, self._html_asset_digest)
        digest = content_version(raw)
        if frontend:
            versions = parse_qs(parsed.query, keep_blank_values=True).get('v', [])
            self._spark_static_cache_control = ('public, max-age=31536000, immutable'
                if versions == [digest] else 'no-cache')
        range_header = self.headers.get('Range')
        compressible = 1024 <= len(raw) <= 2 * 1024 * 1024
        payload, encoding = (compress_response(raw, self.headers.get('Accept-Encoding', ''))
                             if compressible and not range_header else (raw, None))
        etag = '"sha256-' + content_version(payload) + '-' + (encoding or 'identity') + '"'
        modified = self.date_time_string(stat.st_mtime)
        inm = self.headers.get('If-None-Match')
        # HTML always refreshes dependency versions. For JS/CSS use precise ETags,
        # never IMS alone: the filesystem timestamp may be unchanged.
        if ext != '.html' and inm is not None and not range_header:
            tags = [tag.strip().removeprefix('W/') for tag in inm.split(',')]
            if '*' in tags or etag in tags:
                self.send_response(304, 'Not Modified')
                self.send_header('ETag', etag)
                self.send_header('Last-Modified', modified)
                if compressible:
                    self.send_header('Vary', 'Accept-Encoding')
                self.end_headers()
                return
        status, start, end = 200, 0, len(raw) - 1
        if range_header and range_header.strip().startswith('bytes='):
            match = re.fullmatch(r'(\d*)-(\d*)', range_header.strip()[6:].strip())
            if match:
                first, last = match.groups()
                if first:
                    start, end = int(first), int(last) if last else len(raw) - 1
                elif last:
                    suffix = int(last)
                    start = max(0, len(raw) - suffix) if suffix else len(raw)
                if not raw or start > end or start >= len(raw):
                    self.send_response(416, 'Range Not Satisfiable')
                    self.send_header('Content-Range', f'bytes */{len(raw)}')
                    self.send_header('Content-Length', '0')
                    self.end_headers()
                    return
                end = min(end, len(raw) - 1)
                payload = raw[start:end + 1]
                status = 206
        self.send_response(status, 'Partial Content' if status == 206 else 'OK')
        self.send_header('Content-Type', self._MIME_TYPES[ext])
        self.send_header('Content-Length', str(len(payload)))
        self.send_header('Accept-Ranges', 'bytes')
        self.send_header('Last-Modified', modified)
        self.send_header('ETag', etag)
        if compressible:
            self.send_header('Vary', 'Accept-Encoding')
        if encoding:
            self.send_header('Content-Encoding', encoding)
        if status == 206:
            self.send_header('Content-Range', f'bytes {start}-{end}/{len(raw)}')
        self.end_headers()
        if not is_head:
            try:
                self.wfile.write(payload)
            except (ConnectionError, BrokenPipeError):
                self.close_connection = True
