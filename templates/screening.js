(() => {
  const cards = [...document.querySelectorAll('article.story')];
  const filters = document.querySelector('.filters');
  const count = document.getElementById('story-count');
  const empty = document.querySelector('.filter-empty');
  function filter(category) {
    let visible = 0;
    cards.forEach(card => { card.hidden = category !== 'all' && card.dataset.channel !== category; if (!card.hidden) visible++; });
    filters.querySelectorAll('button').forEach(button => {
      const selected = button.dataset.filter === category;
      button.classList.toggle('active', selected); button.setAttribute('aria-pressed', String(selected));
    });
    count.textContent = `${visible} ${visible === 1 ? 'STORY' : 'STORIES'} / ${count.dataset.date}`;
    empty.hidden = category === 'all' || visible > 0;
  }
  filters.hidden = cards.length === 0;
  filters.querySelectorAll('button').forEach(button => button.addEventListener('click', () => filter(button.dataset.filter)));
  document.querySelector('.reset-filter').addEventListener('click', () => { filter('all'); filters.querySelector('button').focus(); });
  // A shared story link must stay visible after changing the category filter.
  addEventListener('hashchange', () => {
    const target = document.getElementById(location.hash.slice(1));
    if (target?.matches('article.story') && target.hidden) { filter('all'); target.scrollIntoView(); }
  });
  const endpoint = document.body.dataset.ratingEndpoint;
  document.querySelectorAll('.actions .up, .actions .down').forEach(button => {
    button.addEventListener('click', async () => {
      const card = button.closest('.story');
      const siblings = card.querySelectorAll('.up, .down');
      const status = card.querySelector('.rating-status');
      siblings.forEach(b => b.disabled = true);
      status.textContent = 'Saving rating…';
      try {
        const response = await fetch(endpoint, {method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({sid:button.dataset.sid,vote:button.dataset.vote})});
        if (!response.ok) throw Error('rating failed');
        button.classList.add('done');
        button.textContent = button.dataset.vote === '+1' ? 'Marked useful' : 'Marked not useful';
        status.textContent = 'Rating saved. It will help shape future editions.';
      } catch (_) {
        siblings.forEach(b => b.disabled = false);
        status.textContent = 'Could not save your rating. Please try again.';
      }
    });
  });
})();
