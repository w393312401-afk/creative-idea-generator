"""Static HTTP delivery; independent of generation state and service credentials."""
import os
import re

from .compression import gzip_body, accepts_gzip


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

            compressible = ext in {'.html', '.css', '.js', '.svg'} and 1024 <= file_size <= 2 * 1024 * 1024

            # If-Modified-Since 校验（仅在无 Range 且未被缓存控制压制时）
            ims = self.headers.get('If-Modified-Since') if hasattr(self, 'headers') else None
            range_header = self.headers.get('Range') if hasattr(self, 'headers') else None
            if ims and not range_header:
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
