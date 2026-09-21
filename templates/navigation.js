(() => {
  const nav = document.querySelector('.edition-nav');
  if (!nav) return;
  const track = nav.querySelector('.day-track');
  const current = nav.querySelector('[aria-current="page"]');
  function centerDate() {
    if (track && current) track.scrollLeft = current.offsetLeft - track.offsetLeft - (track.clientWidth - current.clientWidth) / 2;
  }
  centerDate();
  if (track && typeof ResizeObserver !== 'undefined') new ResizeObserver(centerDate).observe(track);
  document.addEventListener('keydown', event => {
    if (event.defaultPrevented || event.repeat || event.altKey || event.ctrlKey || event.metaKey || event.shiftKey) return;
    const focused = document.activeElement;
    if (focused && (focused.isContentEditable || focused.closest('input,textarea,select,button,a,[role="slider"],.day-track'))) return;
    const direction = event.key === 'ArrowLeft' ? 'prev' : event.key === 'ArrowRight' ? 'next' : null;
    const link = direction && nav.querySelector('a.day-step.' + direction);
    if (link) { event.preventDefault(); location.assign(link.href); }
  });
  document.querySelectorAll('.copy').forEach(button => {
    button.addEventListener('click', async () => {
      const path = location.pathname.includes('/archive/') ? location.pathname : new URL('archive/' + nav.dataset.edition + '.html', location.href).pathname;
      const url = new URL(path, location.origin);
      url.hash = button.dataset.anchor;
      try {
        await navigator.clipboard.writeText(url.href);
        button.textContent = 'Link copied';
      } catch (_) { button.textContent = 'Copy unavailable'; }
      setTimeout(() => { button.textContent = 'Copy link'; }, 1800);
    });
  });
})();
