(() => {
  document.querySelectorAll('.thumbnail-source').forEach(img => {
    const label = img.closest('.thumbnail-cover').querySelector('.thumbnail-label');
    function fallback() { img.remove(); if (label) label.textContent = ''; }
    function failed() {
      if (!img.dataset.retried && img.src.includes('maxresdefault')) {
        img.dataset.retried = '1'; img.src = img.src.replace('maxresdefault','hqdefault');
      } else fallback();
    }
    function check() { if (!img.naturalWidth || img.naturalWidth < 260) failed(); }
    img.addEventListener('error', failed);
    img.addEventListener('load', check);
    if (img.complete) check();
  });
})();
