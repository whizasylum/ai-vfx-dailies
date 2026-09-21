const {chromium}=require('playwright');
const fs=require('fs');
const base=process.env.DAILIES_TEST_URL || 'http://127.0.0.1:8938/docs/';
const assert=(ok,message)=>{if(!ok)throw Error(message)};
(async()=>{
 const browser=await chromium.launch({channel:'chrome',headless:true});
 const page=await browser.newPage({viewport:{width:1440,height:1100}});
 const errors=[];page.on('pageerror',e=>errors.push(e.message));
 await page.goto(base);await page.waitForSelector('article.story');await page.evaluate(()=>document.fonts.ready);
 assert(await page.locator('body').getAttribute('data-design')==='screening-rust','Wrong live design');
 assert(await page.locator('.review-bar,.palette-switch').count()===0,'Preview UI leaked onto live page');
 assert(await page.locator('body').evaluate(el=>getComputedStyle(el).backgroundColor)==='rgb(27, 19, 16)','Wrong rust background');
 const latest=await page.locator('.edition-nav').getAttribute('data-edition');
 const dates=await page.locator('.edition-nav time').evaluateAll(nodes=>nodes.map(n=>n.dateTime));
 const all=await page.locator('article.story').count();
 const firstId=await page.locator('article.story').first().getAttribute('id');
 await page.getByRole('button',{name:'Research',exact:true}).click();
 const research=await page.locator('article.story[data-channel="g"]').count();
 assert(await page.locator('article.story:visible').count()===research,'Category filter mismatch');
 if(!research){assert(await page.locator('.filter-empty').isVisible(),'Missing empty filter state');await page.locator('.reset-filter').click();}
 else await page.getByRole('button',{name:'All stories',exact:true}).click();
 assert(await page.locator('article.story:visible').count()===all,'Filter reset lost stories');
 // Exercise the actual UI/POST payload, with a mocked endpoint so no personal ratings are sent.
 const endpoint=await page.locator('body').getAttribute('data-rating-endpoint');
 const requests=[];
 await page.route(endpoint,route=>{requests.push(route.request().postDataJSON());return route.fulfill({status:requests.length===1?500:204,body:''});});
 const up=page.locator('article.story').first().locator('.up');
 await up.click();await page.locator('article.story').first().getByText('Could not save your rating. Please try again.').waitFor();
 assert(await up.isEnabled(),'Failed rating cannot be retried');
 await up.click();await page.locator('article.story').first().getByText('Rating saved. It will help shape future editions.').waitFor();
 assert(requests.length===2 && requests.every(r=>r.sid===firstId&&r.vote==='+1'),'Incorrect rating payload');
 assert(await up.isDisabled(),'Successful rating remains clickable');
 await page.evaluate(()=>Object.defineProperty(navigator,'clipboard',{value:{writeText:async v=>window.copied=v}}));
 await page.locator('.copy').first().click();
 const copied=await page.evaluate(()=>window.copied);
 assert(copied.endsWith('/archive/'+latest+'.html#'+firstId),'Shared link not pinned to edition');
 // Fresh page for the visual record, without test ratings.
 await page.goto(base);await page.waitForSelector('article.story');await page.evaluate(()=>document.fonts.ready);
 await page.locator('.thumbnail-art').evaluateAll(imgs=>imgs.forEach(i=>i.loading='eager'));
 await page.waitForFunction(()=>[...document.querySelectorAll('.thumbnail-art')].every(i=>i.complete&&i.naturalWidth===1200));
 fs.mkdirSync('design-previews/screenshots',{recursive:true});
 await page.screenshot({path:'design-previews/screenshots/rust-live-desktop.png',fullPage:true});
 assert(await page.locator('a.day-step.next').count()===0,'Newest edition has next link');
 await page.locator('a.day-step.prev').click();await page.waitForURL('**/archive/'+dates.at(-2)+'.html');
 await page.keyboard.press('ArrowRight');await page.waitForURL('**/archive/'+latest+'.html');
 await page.keyboard.press('ArrowRight');assert(page.url().endsWith('/archive/'+latest+'.html'),'Timeline loops at newest');
 await page.goto(new URL('archive/'+dates[0]+'.html',base).href);
 await page.keyboard.press('ArrowLeft');assert(page.url().endsWith('/archive/'+dates[0]+'.html'),'Timeline loops at oldest');
 await page.locator('.brand').click();await page.waitForURL('**/index.html');
 await page.setViewportSize({width:390,height:844});
 await page.goto(base);await page.waitForSelector('article.story');await page.evaluate(()=>document.fonts.ready);
 await page.locator('.thumbnail-art').evaluateAll(imgs=>imgs.forEach(i=>i.loading='eager'));
 await page.waitForFunction(()=>[...document.querySelectorAll('.thumbnail-art')].every(i=>i.complete&&i.naturalWidth===1200));
 assert(!await page.evaluate(()=>document.documentElement.scrollWidth>innerWidth),'Mobile overflow');
 const centered=await page.locator('.edition-nav').evaluate(nav=>{const track=nav.querySelector('.day-track').getBoundingClientRect(),active=nav.querySelector('.current').getBoundingClientRect();return active.left>=track.left && active.right<=track.right+1;});
 assert(centered,'Selected date hidden on mobile');
 await page.screenshot({path:'design-previews/screenshots/rust-live-mobile.png',fullPage:true});
 // Break all remote image loads and check the artwork remains usable on every edition.
 await page.route('**/*',route=>route.request().resourceType()==='image'&&!route.request().url().startsWith(base)?route.abort():route.continue());
 for(const day of dates){
  await page.goto(new URL('archive/'+day+'.html',base).href);await page.waitForSelector('article.story');
  await page.locator('.thumbnail-art,.thumbnail-source').evaluateAll(imgs=>imgs.forEach(i=>i.loading='eager'));
  await page.waitForFunction(()=>[...document.querySelectorAll('.thumbnail-art')].every(i=>i.complete&&i.naturalWidth===1200));
  await page.waitForFunction(()=>document.querySelectorAll('.thumbnail-source').length===0);
  assert(!await page.evaluate(()=>document.documentElement.scrollWidth>innerWidth),'Archive mobile overflow: '+day);
 }
 assert(errors.length===0,errors.join('\n'));
 console.log('Live rust design passed: desktop/mobile, all '+dates.length+' archives, category filters, rating success/retry payloads, pinned sharing, both timeline stops, home link, offline artwork and no JS errors.');
 await browser.close();
})().catch(error=>{console.error(error);process.exitCode=1;});
