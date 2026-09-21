(async () => {
  const editions = await (await fetch('editions.json')).json();
  const dates = Object.keys(editions).sort();
  const mode = document.body.className;
  document.querySelector('[data-mode="' + (mode === 'screening-rust' ? 'screening' : mode) + '"]').classList.add('selected');
  document.querySelector('[data-palette="' + mode + '"]')?.setAttribute('aria-current','page');
  const params = new URLSearchParams(location.search);
  let day = dates.includes(params.get('day')) ? params.get('day') : '2026-09-20';
  let filter = 'all';
  const labels = {a:'WORKFLOW',r:'MODELS & TOOLS',g:'RESEARCH',b:'INDUSTRY'};
  const escape = value => String(value || '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
  const safeUrl = value => /^https?:\/\//.test(value || '') ? escape(value) : '#';
  const imageUrl = value => /^(\.\.\/)?images\/generated\/[a-f0-9]+\.webp$/.test(value || "") ? "../docs/" + value.replace(/^\.\.\//, "") : safeUrl(value);
  const fmt = date => new Date(date + 'T12:00:00').toLocaleDateString('en-GB',{month:'short',day:'numeric'});
  function storyHTML(story,index) {
    const source = story.sources[0] || {};
    const tools = story.tools_and_platforms || [];
    return `<article class="story"><div class="visual"><img class="thumbnail-art" src="${escape(story.fallback_image)}" alt="" loading="lazy">${story.image ? `<img class="thumbnail-source" src="${imageUrl(story.image)}" alt="${story.image_kind === 'ai' ? 'AI illustration' : 'Thumbnail from ' + escape(source.name)}" loading="lazy">` : ''}<div class="image-caption"><span>${escape(source.name)}${story.image_kind === 'ai' ? '<small class="image-credit">AI illustration</small>' : ''}</span><a class="play" href="${safeUrl(source.url)}" target="_blank" rel="noopener">${source.video?'▶ Watch video':'↗ Read source'}</a></div></div><div class="story-content"><div class="story-meta"><span>${labels[story.channel] || 'STORY'} / ${String(index+1).padStart(2,'0')}</span><span>${story.watched?'VIDEO REVIEW':'IN THE DIGEST'}</span></div><h2><a href="${safeUrl(source.url)}" target="_blank" rel="noopener">${escape(story.headline)}</a></h2><p class="body">${escape(story.body)}</p>${story.why?`<div class="takeaway"><b>WHY IT MATTERS</b>${escape(story.why)}</div>`:''}${tools.length?`<ul class="tools">${tools.map(t=>`<li title="${escape(t.used_for)}">${escape(t.name)}</li>`).join('')}</ul>`:''}${story.whats_transferable?.length?`<details><summary>Inside the workflow · ${story.whats_transferable.length} takeaways</summary><ul>${story.whats_transferable.map(t=>`<li>${escape(t)}</li>`).join('')}</ul></details>`:''}<div class="story-actions"><a href="${safeUrl(source.url)}" target="_blank" rel="noopener">Open original ↗</a><button data-vote="useful" data-key="${escape(day+index)}">Useful</button><button data-vote="skip" data-key="${escape(day+index)}">Not for me</button></div></div></article>`;
  }
  function centerDate() {
    const track=document.getElementById('days'), active=track.querySelector('.active');
    if(active)track.scrollLeft=active.offsetLeft-track.offsetLeft-(track.clientWidth-active.clientWidth)/2;
  }
  new ResizeObserver(centerDate).observe(document.getElementById('days'));
  function show() {
    const i = dates.indexOf(day);
    const date = new Date(day + 'T12:00:00');
    document.getElementById('weekday').textContent = date.toLocaleDateString('en-GB',{weekday:'long'});
    document.getElementById('day-number').textContent = date.getDate();
    document.getElementById('month-year').textContent = date.toLocaleDateString('en-GB',{month:'long',year:'numeric'});
    document.getElementById('edition-label').textContent = `${day === dates.at(-1)?'LATEST EDITION':'FROM THE ARCHIVE'} / ${fmt(day)} ${date.getFullYear()}`;
    const titles = {screening:'A method worth<br>taking apart.',editorial:'Notes for the<br>next frame.',workstation:'The daily signal.'};
    document.getElementById('page-title').innerHTML = titles[mode === 'screening-rust' ? 'screening' : mode];
    document.getElementById('dek').textContent = 'AI tools, techniques and workflows. A short read, then back to making things.';
    document.getElementById('older').disabled = i === 0;
    document.getElementById('newer').disabled = i === dates.length-1;
    document.getElementById('latest').disabled = i === dates.length-1;
    document.getElementById('days').innerHTML = dates.map(d=>`<button data-day="${d}" ${d===day?'class="active" aria-current="date"':''}>${fmt(d)}</button>`).join('');
    document.querySelectorAll('[data-day]').forEach(button=>button.onclick=()=>{day=button.dataset.day;show();});
    centerDate();
    const stories = editions[day].stories.filter(s => filter==='all' || s.channel===filter);
    document.getElementById('story-count').textContent = `${stories.length} ${stories.length===1?'STORY':'STORIES'} / ${fmt(day).toUpperCase()}`;
    document.getElementById('stories').innerHTML = stories.length ? stories.map(storyHTML).join('') : '<p class="empty">No stories in this category for this edition.<br>Try All stories or another date.</p>';
    const earlier = dates.filter(d=>d<day).reverse().slice(0,3);
    document.getElementById('earlier-stories').innerHTML = earlier.map(d=>{const s=editions[d].stories[0];return s?`<article class="earlier-card"><small>${fmt(d).toUpperCase()} / ${labels[s.channel]}</small><h3><a href="?day=${d}">${escape(s.headline)}</a></h3><p>${escape(s.why)}</p></article>`:''}).join('');
    document.querySelector('.earlier').hidden = !earlier.length;
    document.querySelectorAll('.story-actions button').forEach(button=>{
      const key='dailies-preview-'+button.dataset.key;
      button.classList.toggle('chosen',sessionStorage.getItem(key)===button.dataset.vote);
      button.onclick=()=>{sessionStorage.setItem(key,button.dataset.vote);show();};
    });
    document.querySelectorAll('.thumbnail-source').forEach(img=>{
      const failed=()=>{if(!img.dataset.retried && img.src.includes('maxresdefault')){img.dataset.retried='1';img.src=img.src.replace('maxresdefault','hqdefault');}else{img.parentElement.querySelector('.image-credit')?.remove();img.remove();}};
      const check=()=>{if(!img.naturalWidth || img.naturalWidth<260)failed();};
      img.onerror=failed;img.onload=check;if(img.complete)check();
    });
    document.querySelectorAll('[data-mode],[data-palette]').forEach(link=>link.href=(link.dataset.palette || link.dataset.mode)+'.html?day='+day);
    history.replaceState(null,'','?day='+day);
  }
  document.getElementById('older').onclick=()=>{const i=dates.indexOf(day);if(i>0){day=dates[i-1];show();}};
  document.getElementById('newer').onclick=()=>{const i=dates.indexOf(day);if(i<dates.length-1){day=dates[i+1];show();}};
  document.getElementById('latest').onclick=()=>{day=dates.at(-1);show();};
  document.querySelectorAll('[data-filter]').forEach(button=>button.onclick=()=>{filter=button.dataset.filter;document.querySelectorAll('[data-filter]').forEach(b=>b.classList.toggle('active',b===button));show();});
  show();
})();
