(() => {
  const button = document.getElementById('auth-theme');
  const root = document.documentElement;
  const key = 'ziqva_theme';

  function applyTheme(theme) {
    root.dataset.theme = theme;
    const light = theme === 'light';
    button.textContent = light ? 'Mode gelap' : 'Mode terang';
  }

  applyTheme(localStorage.getItem(key) === 'light' ? 'light' : 'dark');
  button.addEventListener('click', () => {
    const theme = root.dataset.theme === 'light' ? 'dark' : 'light';
    localStorage.setItem(key, theme);
    applyTheme(theme);
  });
})();
