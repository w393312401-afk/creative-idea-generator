"""Recover Flow videos by project media identity, independent of transient DOM IDs.

The Angular project response contains the full submitted prompt, ordered input
media IDs and output media ID. Never attribute a video by a shared prompt prefix
or by its position in the grid. A temporary reader tab avoids reloading the live
generation page; it only loads the existing project and hovers matched results.
"""
import json
import re
import time
from urllib.parse import urlparse

from ..utils.logger import log
from ..utils.browser import flow_project_id

_UUID = re.compile(r'^[0-9a-fA-F-]{36}$')


def _normal_prompt(value):
    # Compatibility for historical submissions corrupted by our old
    # click/End/type-space sequence ("close-up" -> "close- up"). New requests
    # are verified before sending; do not broadly delete whitespace here.
    text = re.sub(r'(?<=\w)-\s+(?=\w)', '-', str(value or ''))
    return re.sub(r'\s+', ' ', text).strip()


def parse_project_media(body, project_id):
    """Parse framed batchexecute responses, validating the observed media schema."""
    records = {}

    def visit(node):
        if not isinstance(node, list):
            return
        if (len(node) >= 6 and isinstance(node[0], str) and _UUID.fullmatch(node[0])
                and node[1] == project_id and isinstance(node[5], list)):
            meta = node[5]
            try:
                prompt = meta[1]
                spec = meta[6][1]
                model = spec[0][0]
                refs = [r[2] for r in spec[1]]
                created = float(meta[0][0])
                if (isinstance(prompt, str) and prompt and isinstance(model, str)
                        and ('i2v' in model or 't2v' in model)
                        and all(isinstance(r, str) and _UUID.fullmatch(r) for r in refs)):
                    records[node[0]] = dict(media_id=node[0], project_id=project_id,
                                           prompt=prompt, refs=refs, created=created,
                                           thumbnail_path=urlparse(meta[5]).path
                                           if isinstance(meta[5], str) else '')
            except (IndexError, TypeError, ValueError):
                pass
        for child in node:
            if isinstance(child, list):
                visit(child)
            elif isinstance(child, str) and child.startswith('['):
                try:
                    visit(json.loads(child))
                except (ValueError, RecursionError):
                    pass

    for line in body.splitlines():
        if line.startswith('['):
            try:
                visit(json.loads(line))
            except (ValueError, RecursionError):
                pass
    return list(records.values())


def match_project_media(records, prompt, refs=(), submitted_at=0, excluded=()):
    """Only a unique full-prompt match with compatible ordered refs is usable."""
    wanted = _normal_prompt(prompt)
    if not wanted:
        return None
    candidates = [r for r in records
                  if r['media_id'] not in excluded
                  and _normal_prompt(r['prompt']) == wanted
                  and (not refs or list(refs) == r['refs'])
                  and r['created'] >= submitted_at - 10]
    return candidates[0] if len(candidates) == 1 else None


class ProjectVideoRecovery:
    """One reader per generation round; listen to live media updates as well.

    Never reload the generating tab. A bounded, backoff refresh of the same
    reader replaces the old open/navigate/close loop on every missing poll.
    """
    def __init__(self, page):
        self.page = page
        self.project_id = flow_project_id(page.url)
        self.records = {}
        self.bindings = {}
        self.reader = None
        self.loads = 0
        self.loaded_at = 0

    def on_response(self, response):
        if '/data/batchexecute' in response.url:
            try:
                for row in parse_project_media(response.text(), self.project_id):
                    self.records[row['media_id']] = row
            except Exception:
                pass

    def __enter__(self):
        self.page.on('response', self.on_response)
        return self

    def __exit__(self, *args):
        self.page.remove_listener('response', self.on_response)
        if self.reader:
            try:
                self.reader.close()
            except Exception:
                pass

    def match_request(self, request, excluded=()):
        # Cache the verified output identity independently of regenerated DOM
        # stamps. Include the entire request so a retry cannot reuse a binding
        # after its prompt, refs or submission time has changed.
        key = (request['tile_id'], request['prompt'], tuple(request.get('refs', ())),
               request.get('click_time', 0))
        record = self.bindings.get(key)
        if record:
            return record if record['media_id'] not in excluded else None
        record = match_project_media(list(self.records.values()), request['prompt'],
                                     request.get('refs', ()), request.get('click_time', 0), excluded)
        if record:
            self.bindings[key] = dict(record)
        return record

    def read(self, check_cancel):
        if self.reader is None:
            self.reader = self.page.context.new_page()
            self.reader.on('response', self.on_response)
        if self.loads < 3 and (not self.loads or time.monotonic() - self.loaded_at >= 90):
            self.loads += 1
            self.loaded_at = time.monotonic()
            self.reader.goto(self.page.url, wait_until='domcontentloaded', timeout=15000)
            # Wait for all hydration responses, not just the first media row.
            for _ in range(12):
                check_cancel()
                self.reader.wait_for_timeout(250)
        return self.reader


def recover_project_videos(page, requests, cancel_check=None, on_resolved=None, session=None):
    """Return {task tile id: done state} for provably matching existing outputs.

    No Generate, prompt edits, project changes, or HTTP write replay. URLs come
    from the actual matched video's player, never from guessing a media URL.
    """
    project_id = flow_project_id(page.url)
    if not requests or not project_id:
        return {}
    reader = None
    records = {}
    results = {}

    def on_response(response):
        if '/data/batchexecute' not in response.url:
            return
        try:
            for row in parse_project_media(response.text(), project_id):
                records[row['media_id']] = row
        except Exception:
            pass

    def check_cancel():
        if cancel_check:
            cancel_check()

    try:
        check_cancel()
        if session is not None:
            reader = session.read(check_cancel)
            records = session.records
        else:
            reader = page.context.new_page()
            reader.on('response', on_response)
            reader.goto(page.url, wait_until='domcontentloaded', timeout=15000)
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline and not records:
            check_cancel()
            reader.wait_for_timeout(250)
        used = set()
        for request in requests:
            check_cancel()
            record = (session.match_request(request, used) if session is not None else
                      match_project_media(list(records.values()), request['prompt'],
                                          request.get('refs', ()), request.get('click_time', 0), used))
            if not record:
                log(f"🔎 视频归属暂未唯一匹配: tile={request['tile_id']}, "
                    f"项目视频数={len(records)}, "
                    f"完整提示词候选={sum(_normal_prompt(r['prompt']) == _normal_prompt(request['prompt']) for r in records.values())}", 'GoogleFX-Video')
                continue
            media_id = record['media_id']
            # The result thumbnail and video share the media UUID. Scope to video
            # tiles so a reference image can never be mistaken for a result.
            tile_selector = (
                f'flow-grid-tile-container:has(flow-video-tile img[src*="{media_id}"]), '
                f'flow-grid-tile-container:has(video[src*="{media_id}"])')
            thumbnail_path = record.get('thumbnail_path', '')
            if re.fullmatch(r'/asb/[A-Za-z0-9_-]+', thumbnail_path):
                tile_selector = (
                    f'flow-grid-tile-container:has(flow-video-tile img[src*="{media_id}"]), '
                    f'flow-grid-tile-container:has(video[src*="{media_id}"]), '
                    f'flow-grid-tile-container:has(flow-video-tile img[src*="{thumbnail_path}"])')
            # Prefer the live canvas: the retained reader may predate this
            # particular output, while its response identity is already known.
            target_page = page if page.locator(tile_selector).count() == 1 else reader
            tile = target_page.locator(tile_selector)
            try:
                tile.first.wait_for(state='attached', timeout=4000)
                if tile.count() != 1:
                    continue
                # Hover replaces the thumbnail, so don't keep using a locator
                # whose :has(img) predicate disappears during that interaction.
                stamp = 'media_' + media_id
                tile.evaluate('(el, id) => el.setAttribute("data-spark-tile-id", id)', stamp)
                from .google_fx_helpers import wake_flow_tile_video
                src = wake_flow_tile_video(target_page, stamp, allow_click=True)
                # Angular initially mounts the thumbnail URL on <video>, then
                # switches to the playable =mm rendition asynchronously.
                stable = target_page.locator(f'[data-spark-tile-id="{stamp}"]').first
                deadline = time.monotonic() + 6
                while time.monotonic() < deadline:
                    check_cancel()
                    ready = stable.locator('video').evaluate(
                        '(v) => ({src:v.currentSrc || v.src, ready:v.readyState})')
                    if ready['ready'] >= 1:
                        src = ready['src']
                        break
                    target_page.wait_for_timeout(250)
                else:
                    continue
                # Freshly loaded projects use opaque /asb/ player URLs. Identity
                # is proven by the matched tile, not by spelling of its URL.
                source = urlparse(src)
                if source.scheme != 'https' or source.hostname not in (
                        'flow.google.com', 'flow-content.google', 'lh3.googleusercontent.com'):
                    continue
                used.add(media_id)
                results[request['tile_id']] = dict(status='done', videoSrc=src,
                    resolvedBy='project_media_identity', mediaId=media_id, needsMediaWake=False)
                log(f"🔗 完整提示词与项目资产核对成功，恢复视频 {media_id}", 'GoogleFX-Video')
                if on_resolved:
                    on_resolved(request['tile_id'], results[request['tile_id']])
            except Exception as exc:
                log(f"⚠️ 项目资产已定位但播放器未就绪: {type(exc).__name__}", 'GoogleFX-Video')
    finally:
        if reader and session is None:
            try:
                reader.close()
            except Exception:
                pass
    return results
