import shutil
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from bs4 import BeautifulSoup
from digest import editions, render, thumbnails

ROOT = Path(__file__).resolve().parents[1]


class DesignMigrationTests(unittest.TestCase):
    def test_restyle_preserves_editions_links_ids_notes_and_is_repeatable(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory)
            shutil.copytree(ROOT/'templates',root/'templates')
            shutil.copytree(ROOT/'docs',root/'docs')
            archive=root/'docs/archive'
            # The earliest layout used .rail instead of .tag; retain its category.
            legacy=archive/'2026-01-01.html'
            legacy.write_text('''<!doctype html><html><head></head><body><header></header>
<div class="stamp">1 story from 30 items</div><p class="verdict">An archived verdict.</p>
<article class="story" id="e985dae5bf"><div class="rail"><b>G</b></div><h2 class="headline">A method &amp; its limits</h2><p class="body-text">Research details.</p><p class="why">A practical use.</p><ul class="sources"><li><a href="https://example.com/paper">Paper</a></li><li><a class="talk" href="https://example.com/discussion">discussion</a></li></ul></article>
<footer>Compiled 2026-01-01 06:00 NZDT · 4/5 sources responded<details><summary>1 source failed this run</summary><ul><li>Feed — timeout</li></ul></details></footer></body></html>''',encoding='utf-8')
            # IDs in actual archives are source-derived. Build this fixture likewise.
            from digest.feedback import story_id
            sid=story_id(editions.read_html(legacy)['stories'][0])
            legacy.write_text(legacy.read_text(encoding="utf-8").replace('e985dae5bf',sid),encoding='utf-8')
            before={p:editions.read_html(p) for p in archive.glob('*.html')}
            ids={p:[n['id'] for n in BeautifulSoup(p.read_text(encoding='utf-8'),'html.parser').select('article.story')] for p in before}
            config={'sharing':{'rating_endpoint':'https://example.com/ratings','noindex':True}}
            with patch.object(thumbnails, '_generate') as generate:
                render.refresh_design(root,config)
                generate.assert_not_called()
            for path,data in before.items():
                self.assertEqual(editions.read_html(path),data,path.name)
                soup=BeautifulSoup(path.read_text(encoding='utf-8'),'html.parser')
                self.assertEqual(soup.body['data-design'],'screening-rust')
                self.assertEqual([n['id'] for n in soup.select('article.story')],ids[path])
                self.assertEqual(soup.select_one('.brand')['href'],'../index.html')
                self.assertTrue(all((path.parent/a['href']).exists() for a in soup.select('.earlier-card a, .edition-nav a')))
                self.assertEqual(len(soup.select('script#edition-navigation')),1)
                self.assertEqual(len(soup.select('script#thumbnail-fallbacks')),1)
                for button in soup.select('.up, .down'):
                    self.assertEqual(button['data-sid'],button.find_parent('article')['id'])
            text=BeautifulSoup(legacy.read_text(encoding='utf-8'),'html.parser').select_one('.run-notes').get_text(' ',strip=True)
            self.assertIn('2026-01-01 06:00 NZDT',text)
            self.assertIn('Feed — timeout',text)
            pages=[root/'docs/index.html',*archive.glob('*.html')]
            first={p:p.read_bytes() for p in pages}
            render.refresh_design(root,config)
            self.assertEqual({p:p.read_bytes() for p in pages},first)
            self.assertEqual(editions.read_html(root/'docs/index.html'),editions.read_html(sorted(archive.glob('*.html'))[-1]))
            # Daily retention must also remove stale links in the earlier-story cards.
            with patch.object(render, 'ARCHIVE_KEEP', 2):
                render.render_html({'stories':[]},config,root,0,[],0)
            for path in [root/'docs/index.html',*archive.glob('*.html')]:
                soup=BeautifulSoup(path.read_text(encoding='utf-8'),'html.parser')
                self.assertTrue(all((path.parent/a['href']).exists() for a in soup.select('.earlier-card a, .edition-nav a')))


if __name__ == '__main__':
    unittest.main()
