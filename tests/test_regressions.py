import argparse
import contextlib
import io
import json
import os
import re
import shutil
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

import httpx
from bs4 import BeautifulSoup
from digest import cli, editions, feedback, navigation, render, watch
from digest.collect import Item

ROOT = Path(__file__).resolve().parents[1]


def verdict(**updates):
    data = dict(video_id='mNjBBO1gWFI', channel='Stefan 3D AI',
                tools_and_platforms=[dict(name='Blender', used_for='Assembly')],
                whats_new='Method', whats_transferable=['Assembly'], pipeline_notes='Adapt to Houdini',
                visual_quality_notes='Example', category='workflow', topic_relevance=2,
                transferable_craft=8, reason_if_low=None, summary_bullets=['Method'])
    return {**data, **updates}


class RegressionTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.state = self.root / 'state'
        self.state.mkdir()
        shutil.copytree(ROOT / 'templates', self.root / 'templates')
        self.archive = self.root / 'docs/archive'
        self.archive.mkdir(parents=True)

    def tearDown(self):
        self.tmp.cleanup()

    def test_all_real_archives_are_bounded_chronological_and_content_is_preserved(self):
        shutil.copytree(ROOT / 'docs', self.root / 'docs', dirs_exist_ok=True)
        paths = list(self.archive.glob('*.html')) + [self.root / 'docs/index.html']
        before = {p.name: re.findall(r'<article\b[\s\S]*?</article>', p.read_text(encoding='utf-8')) for p in paths}
        navigation.refresh(self.root)
        dates = sorted(p.stem for p in self.archive.glob('*.html'))
        for p in paths:
            html = p.read_text(encoding='utf-8')
            self.assertEqual(before[p.name], re.findall(r'<article\b[\s\S]*?</article>', html))
            soup = BeautifulSoup(html, 'html.parser')
            nav = soup.select_one('.edition-nav')
            self.assertEqual([n['datetime'] for n in nav.select('time')], dates)
            self.assertEqual(len(soup.select('style#edition-navigation')), 1)
            self.assertEqual(len(soup.select('script#edition-navigation')), 1)
            date = dates[-1] if p.name == 'index.html' else p.stem
            index = dates.index(date)
            for direction, neighbor in [('prev', index-1), ('next', index+1)]:
                link = nav.select_one('a.' + direction)
                if 0 <= neighbor < len(dates):
                    self.assertEqual(Path(link['href']).stem, dates[neighbor])
                    self.assertTrue((p.parent / link['href']).exists())
                else:
                    self.assertIsNone(link)
        self.assertEqual(navigation.refresh(self.root), 0)

    def test_empty_rerun_and_new_story_preserve_migrated_edition(self):
        day = '2026-09-20'
        shutil.copy(ROOT / f'docs/archive/{day}.html', self.archive / f'{day}.html')
        original = editions.read_html(self.archive / f'{day}.html')
        merged = editions.merge(self.root, day, {'stories': [], 'verdict': 'Nothing new'})
        self.assertEqual(merged['stories'], original['stories'])
        new = dict(headline='New story', sources=[dict(url='https://example.com/new')])
        merged = editions.merge(self.root, day, {'stories': [new]})
        self.assertEqual(len(merged['stories']), len(original['stories'])+1)
        self.assertEqual(editions.merge(self.root, day, {'stories': [new]})['stories'], merged['stories'])

    def test_render_on_windows_and_pruning_have_no_dead_date_links(self):
        from datetime import timedelta
        now = datetime.now(timezone.utc)
        for n in range(33):
            day = (now - timedelta(days=n+1)).date().isoformat()
            (self.archive / f'{day}.html').write_text('<html><head></head><body><header></header></body></html>')
        path = render.render_html({'stories': [], 'verdict': ''}, {'digest': {'timezone':'UTC'}}, self.root, 0, [], 0)
        self.assertTrue(path.exists())
        self.assertEqual(len(list(self.archive.glob('*.html'))), render.ARCHIVE_KEEP)
        for p in self.archive.glob('*.html'):
            soup = BeautifulSoup(p.read_text(encoding='utf-8'), 'html.parser')
            self.assertTrue(all((p.parent / a['href']).exists() for a in soup.select('.edition-nav a')))

    def test_shortlisted_stories_survive_full_cli_rerun(self):
        config = {"digest": {"timezone": "UTC"}, "delivery": {"html": True, "discord": False}, "feedback": {"enabled": False}}
        story = dict(headline="An existing story", body="Details", why="Useful", channel="a", sources=[dict(url="https://example.com/story", name="Source")])
        stats = dict(watched=0, published=0, rejected=0, deferred=0, errors=0)
        args = argparse.Namespace(no_feedback=True, dry=False, no_discord=True, page_url=None)
        with patch.object(cli, 'ROOT', self.root), patch.object(cli, '_load', return_value=(config, [])), patch.object(cli.collect, 'collect_all', return_value=([], [])), patch.object(cli.watch, 'run', return_value=([], stats)), patch.object(cli.summarize, 'summarize', side_effect=[{'stories':[story], 'verdict':'Useful work'}, {'stories':[], 'verdict':'No new items'}]):
            cli.cmd_run(args)
            cli.cmd_run(args)
        soup = BeautifulSoup((self.root/'docs/index.html').read_text(encoding='utf-8'), 'html.parser')
        self.assertEqual(len(soup.select('article.story')), 1)
        self.assertIn('An existing story', soup.get_text())

    def test_bad_scores_and_unexplained_rejections_are_rejected(self):
        for bad in ['9', True, -1, 11, float('nan')]:
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                watch.validate_verdict(verdict(topic_relevance=bad))
        for reason in [None, '', 'n/a']:
            with self.assertRaises(ValueError):
                watch.validate_verdict(verdict(topic_relevance=4, transferable_craft=4, reason_if_low=reason))
        self.assertEqual(watch.validate_verdict(verdict())['transferable_craft'], 8)

    def test_watch_failure_is_nonzero(self):
        args = argparse.Namespace(url=['https://youtube.com/watch?v=mNjBBO1gWFI'], force=False, long_form=False, channel='test')
        with patch.dict(os.environ, {'GEMINI_API_KEY':'dummy'}), patch.object(watch, 'probe_metadata', return_value=None), contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(cli.cmd_watch(args), 1)
        with patch.dict(os.environ, {'GEMINI_API_KEY':'dummy'}), patch.object(watch, 'probe_metadata', return_value=dict(duration_s=600, live=False, upcoming=False)), patch.object(watch,'watch_video', side_effect=RuntimeError('failed')), contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(cli.cmd_watch(args), 1)

    def test_watched_story_survives_retry_without_second_model_call(self):
        item = Item('Workflow', 'https://youtube.com/watch?v=mNjBBO1gWFI', 'Stefan 3D AI', datetime.now(timezone.utc).isoformat())
        with patch.dict(os.environ, {'GEMINI_API_KEY':'dummy'}), patch.object(watch, 'probe_metadata', return_value=dict(duration_s=600, live=False, upcoming=False)), patch.object(watch,'watch_video', return_value=verdict()) as model, patch.object(watch.time,'sleep'):
            first, stats = watch.run([item], {}, set(), self.state)
            second, _ = watch.run([], {}, set(), self.state)
            self.assertEqual(first, second)
            self.assertEqual(model.call_count, 1)
            watch.mark_delivered(self.state, first)
            self.assertEqual(watch.run([item], {}, set(), self.state)[0], [])

    def test_invalid_model_result_cannot_crash_digest_and_reserves_quota(self):
        item = Item('Workflow', 'https://youtube.com/watch?v=mNjBBO1gWFI', 'Stefan 3D AI', datetime.now(timezone.utc).isoformat())
        with patch.dict(os.environ, {'GEMINI_API_KEY':'dummy'}), patch.object(watch, 'probe_metadata', return_value=dict(duration_s=600, live=False, upcoming=False)), patch.object(watch,'watch_video', return_value=verdict(topic_relevance='broken')):
            stories, stats = watch.run([item], {}, set(), self.state)
        self.assertEqual(stats['errors'], 1)
        state = watch.load_state(self.state / 'watched.json')
        self.assertEqual(state['quota']['minutes'], 10)
        self.assertEqual(state['videos'], {})

    def test_same_issue_retry_and_repeat_taps_do_not_amplify_rating(self):
        issued = [dict(number=11, title='rating +1 abcdef0123'), dict(number=12, title='rating +1 abcdef0123')]
        methods = []
        def handler(request):
            methods.append(request.method)
            return httpx.Response(200, json=issued)
        client = httpx.Client(transport=httpx.MockTransport(handler))
        with patch.dict(os.environ, {'GITHUB_TOKEN':'dummy', 'GITHUB_REPOSITORY':'test/repo'}), patch.object(feedback.httpx, 'Client', return_value=client):
            ratings = []
            feedback.ingest_github_issues(self.state, ratings)
        self.assertEqual(len(ratings), 1)
        self.assertEqual(ratings[0]['issue_ids'], [11,12])
        self.assertEqual(methods, ['GET'])  # no premature acknowledgement
        client = httpx.Client(transport=httpx.MockTransport(handler))
        with patch.dict(os.environ, {'GITHUB_TOKEN':'dummy', 'GITHUB_REPOSITORY':'test/repo'}), patch.object(feedback.httpx, 'Client', return_value=client):
            self.assertEqual(feedback.ingest_github_issues(self.state, ratings), 0)
        opposite = {**ratings[0], 'vote':-1, 'issue_ids':[13]}
        self.assertEqual(len(feedback.dedupe_ratings(ratings + [opposite])), 2)

    def test_acknowledgement_only_closes_committed_receipts_and_reports_failure(self):
        rating = dict(story_id='abcdef0123', at=datetime.now(timezone.utc).isoformat(), via='page', vote=1, issue_ids=[11])
        feedback.save_ratings(self.state,[rating])
        closed = []
        def handler(request):
            if request.method == 'GET':
                return httpx.Response(200, json=[dict(number=11),dict(number=12)])
            closed.append(request.url.path)
            return httpx.Response(403)
        client = httpx.Client(transport=httpx.MockTransport(handler))
        with patch.dict(os.environ, {'GITHUB_TOKEN':'dummy', 'GITHUB_REPOSITORY':'test/repo'}), patch.object(feedback.httpx,'Client',return_value=client):
            self.assertEqual(feedback.acknowledge_github_issues(self.state), 1)
        self.assertEqual(closed,['/repos/test/repo/issues/11'])

if __name__ == '__main__':
    unittest.main()
