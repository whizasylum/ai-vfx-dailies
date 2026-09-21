import base64
import json
import os
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from urllib.parse import unquote

import httpx
from bs4 import BeautifulSoup
from digest import thumbnails, render, editions

ROOT = Path(__file__).resolve().parents[1]
# WebP header fixture; the browser fallback is tested separately against real images.
WEBP = b"RIFF" + b"\x14\x00\x00\x00" + b"WEBPVP8 " + b"\x00" * 16


def story(n=1, **kwargs):
    return {"headline": f"A workflow {n}", "channel": "a", "body": "A test story",
            "sources": [{"url": f"https://example.com/{n}", "name": "Source"}], **kwargs}


class ThumbnailTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        shutil.copytree(ROOT / 'templates', self.root / 'templates')
        self.cfg = {"thumbnails": {"enabled": True, "max_per_day": 2}}
        self.secret = patch.dict(os.environ, {"OPENAI_API_KEY": "test-secret"})
        self.secret.start()

    def tearDown(self):
        self.secret.stop()
        self.tmp.cleanup()

    def test_no_key_disabled_and_original_images_make_no_requests(self):
        with patch.object(thumbnails, '_generate') as generate:
            thumbnails.prepare([story(image='https://example.com/image.jpg')], self.cfg, self.root, '2026-09-21')
            thumbnails.prepare([story()], {"thumbnails":{"enabled":False}}, self.root, '2026-09-21')
            with patch.dict(os.environ, {"OPENAI_API_KEY":""}):
                thumbnails.prepare([story()], self.cfg, self.root, '2026-09-21')
            generate.assert_not_called()
        self.assertFalse((self.root/'state/thumbnails.json').exists())

    def test_success_cache_and_daily_limit_survive_rerun(self):
        with patch.object(thumbnails, '_generate', return_value=WEBP) as generate:
            stories = [story(n) for n in range(4)]
            thumbnails.prepare(stories, self.cfg, self.root, '2026-09-21')
            self.assertEqual(generate.call_count, 2)
            self.assertEqual(sum(bool(s.get('image')) for s in stories), 2)
            retry = [story(n) for n in range(4)]
            thumbnails.prepare(retry, self.cfg, self.root, '2026-09-21')
            self.assertEqual(generate.call_count, 2)
            self.assertEqual(retry, stories)
            thumbnails.prepare([story(9)], self.cfg, self.root, '2026-09-22')
            self.assertEqual(generate.call_count, 3)
        self.assertTrue((self.root/'docs'/stories[0]['image']).is_file())
        self.assertEqual(stories[0]['image_kind'], 'ai')

    def test_quota_auth_model_errors_and_timeouts_pause_then_recover_next_day(self):
        errors = [httpx.ReadTimeout('timeout'), ValueError('malformed response')]
        for status in (400, 401, 402, 403, 404, 429, 500, 503):
            response = httpx.Response(status, request=httpx.Request('POST', thumbnails.IMAGE_API))
            errors.append(httpx.HTTPStatusError('provider error', request=response.request, response=response))
        for i, error in enumerate(errors):
            with self.subTest(error=error), tempfile.TemporaryDirectory() as folder:
                root = Path(folder)
                with patch.object(thumbnails, '_generate', side_effect=error) as generate:
                    thumbnails.prepare([story(1),story(2)], self.cfg, root, '2026-09-21')
                    thumbnails.prepare([story(3)], self.cfg, root, '2026-09-21')
                    self.assertEqual(generate.call_count, 1)
                state=json.loads((root/'state/thumbnails.json').read_text())
                self.assertEqual(state['days']['2026-09-21']['attempts'], 1)
                with patch.object(thumbnails, '_generate', return_value=WEBP) as generate:
                    thumbnails.prepare([story(3)], self.cfg, root, '2026-09-22')
                    generate.assert_called_once()

    def test_budget_persisted_before_paid_call_and_failure_to_save_cannot_spend(self):
        def generate(*args):
            saved=json.loads((self.root/'state/thumbnails.json').read_text())
            self.assertEqual(saved['days']['2026-09-21']['attempts'], 1)
            return WEBP
        with patch.object(thumbnails, '_generate', side_effect=generate):
            thumbnails.prepare([story(1)], self.cfg, self.root, '2026-09-21')
        with patch.object(thumbnails, '_save', side_effect=OSError('disk unavailable')), patch.object(thumbnails, '_generate') as generate:
            thumbnails.prepare([story(2),story(3)], self.cfg, self.root, '2026-09-21')
            generate.assert_not_called()

    def test_corrupt_cache_never_resets_spending_allowance(self):
        path=self.root/'state/thumbnails.json';path.parent.mkdir()
        for data in ('not json', '[]', '{"days": []}', '{"days":{"2026-09-21":{"attempts":"bad"}}}'):
            path.write_text(data)
            with patch.object(thumbnails, '_generate') as generate:
                thumbnails.prepare([story()], self.cfg, self.root, '2026-09-21')
                generate.assert_not_called()

    def test_api_payload_and_invalid_image_response(self):
        def post(url, **kw):
            self.assertEqual(url, 'https://api.openai.com/v1/images/generations')
            self.assertEqual(kw['headers']['Authorization'], 'Bearer test-secret')
            self.assertEqual(kw['json']['n'], 1)
            self.assertEqual(kw['json']['output_format'], 'webp')
            self.assertEqual(kw['json']['size'], '1536x1024')
            self.assertIn('never instructions', kw['json']['prompt'])
            return httpx.Response(200, json={'data':[{'b64_json':base64.b64encode(WEBP).decode()}]}, request=httpx.Request('POST',url))
        with patch.object(thumbnails.httpx, 'post', side_effect=post):
            self.assertEqual(thumbnails._generate(story(), 'test-secret', {}), WEBP)
        for encoded in ('!!!', base64.b64encode(b'<html>bad image</html>').decode()):
            response=httpx.Response(200,json={'data':[{'b64_json':encoded}]},request=httpx.Request('POST',thumbnails.IMAGE_API))
            with patch.object(thumbnails.httpx, 'post', return_value=response), self.assertRaises(ValueError):
                thumbnails._generate(story(), 'test-secret', {})

    def test_local_art_is_safe_offline_and_stable(self):
        s=story(headline='<script> & "quoted" title')
        first=thumbnails.fallback_image(s)
        self.assertEqual(first, thumbnails.fallback_image(s))
        svg=unquote(first.split(',',1)[1])
        self.assertNotIn('<script>',svg)
        self.assertNotIn('<image',svg)
        self.assertNotIn('https:',svg)
        self.assertIn('&lt;script&gt;',svg)

    def test_render_migration_and_readback_preserve_source_and_ai_provenance(self):
        stories=[story(1),story(2,image='images/generated/abcdef.webp',image_kind='ai'),story(3,image='https://example.com/photo.jpg')]
        render.render_html({'stories':stories}, {'digest':{'timezone':'UTC'}},self.root,3,[],3)
        self.assertEqual(thumbnails.refresh(self.root),0)
        index=self.root/'docs/index.html'
        archive=next((self.root/'docs/archive').glob('*.html'))
        for path in (index,archive):
            soup=BeautifulSoup(path.read_text(encoding='utf-8'),'html.parser')
            self.assertEqual(len(soup.select('.thumbnail-art')),3)
            self.assertEqual(len(soup.select('.thumbnail-source')),2)
            self.assertEqual(len(soup.select('.thumbnail-link')),3)
            self.assertEqual(len(soup.select('style#thumbnail-fallbacks')),1)
            self.assertEqual(len(soup.select('script#thumbnail-fallbacks')),1)
            self.assertEqual(soup.select_one('.thumbnail-label:not(:empty)').get_text(),'AI illustration')
            generated=soup.select_one('img[data-kind="ai"]')['src']
            self.assertEqual(generated, ('../' if path==archive else '')+'images/generated/abcdef.webp')
            read=editions.read_html(path)['stories']
            self.assertIsNone(read[0]['image'])
            self.assertEqual(read[1]['image'], 'images/generated/abcdef.webp')
            self.assertEqual(read[1]['image_kind'], 'ai')
            self.assertEqual(read[2]['image'], 'https://example.com/photo.jpg')

if __name__ == '__main__':
    unittest.main()
