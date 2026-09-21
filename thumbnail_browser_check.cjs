const { chromium }=require('playwright');
const fs=require('fs');
(async()=>{
 const browser=await chromium.launch({channel:'chrome',headless:true});
 const page=await browser.newPage({viewport:{width:1440,height:1100}});
 const errors=[];page.on('pageerror',e=>errors.push(e.message));
 const base='http://127.0.0.1:8938/';
 await page.goto(base+'design-previews/screening-rust.html?day=2026-09-18');
 await page.waitForSelector('.story');await page.evaluate(()=>document.fonts.ready);
 await page.screenshot({path:'design-previews/screenshots/screening-rust-sep18.png',fullPage:true});
 if(await page.locator('body').evaluate(el=>getComputedStyle(el).backgroundColor)!=='rgb(27, 19, 16)')throw Error('Rust background mismatch');
 await page.locator('[data-palette="screening"]').click();
 await page.waitForSelector('.story');
 if(!page.url().endsWith('screening.html?day=2026-09-18'))throw Error('Palette lost selected date');
 await page.locator('[data-palette="screening-rust"]').click();await page.waitForSelector('.story');
 // Intercept only image requests to simulate unavailable upstream thumbnails.
 await page.route('**/*',route=>{
  const req=route.request();
  if(req.resourceType()==='image' && !req.url().startsWith(base))return route.abort();
  return route.continue();
 });
 for(const url of ['design-previews/screening-rust.html?day=2026-09-20','docs/archive/2026-09-20.html']){
  await page.goto(base+url);await page.waitForSelector('article.story');
  await page.locator('article.story').last().scrollIntoViewIfNeeded();
  await page.waitForFunction(()=>document.querySelectorAll('.thumbnail-source').length===0);
  await page.waitForFunction(()=>[...document.querySelectorAll('.thumbnail-art')].every(i=>i.complete && i.naturalWidth===1200));
  if(await page.locator('.thumbnail-label:not(:empty),.image-credit').count())throw Error('Misleading AI label after fallback');
  await page.screenshot({path:'design-previews/screenshots/'+(url.startsWith('docs')?'live':'rust')+'-fallback.png',fullPage:true});
  await page.setViewportSize({width:390,height:844});
  if(await page.evaluate(()=>document.documentElement.scrollWidth>innerWidth))throw Error('fallback mobile overflow');
  await page.screenshot({path:'design-previews/screenshots/'+(url.startsWith('docs')?'live':'rust')+'-fallback-mobile.png',fullPage:true});
  await page.setViewportSize({width:1440,height:1100});
 }
 if(errors.length)throw Error(errors.join('\n'));
 console.log('Rust palette/day preservation and forced image-failure fallback passed in preview and production HTML. No JS errors or mobile overflow.');
 await browser.close();
})();
