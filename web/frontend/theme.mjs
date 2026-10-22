// The remote UI always wears the active Omarchy theme: /api/theme serves the
// host's colors.toml and font, mapped here onto the CSS tokens in style.css.
// style.css carries Tokyo Night (Omarchy's default) for the first paint.
const POLL_MS = 30000;

// colors.toml key → CSS token. Pure.
export function tokens(colors) {
  const c = key => colors[key];
  return {
    '--bg': c('background'),
    '--bg-2': c('lighter_background'),
    '--bg-3': c('dark_background'),
    '--raise': c('selection'),
    '--line': c('selection'),
    '--muted': c('muted'),
    '--fg': c('bright_foreground') || c('foreground'),
    '--fg-2': c('foreground'),
    // dark_foreground is often too dim for 11px labels; lift it toward the text color.
    '--fg-3': `color-mix(in srgb, ${c('dark_foreground')} 72%, ${c('foreground')})`,
    '--accent': c('accent'),
    '--on-accent': c('background'),
    '--ok': c('green'),
    '--warn': c('yellow'),
    '--bad': c('red'),
    '--cyan': c('cyan'),
    '--magenta': c('magenta'),
  };
}

export function installTheme({onChange = () => {}} = {}) {
  let version = '';
  let fontLoaded = '';

  async function loadFont(theme) {
    if (!theme.fonts?.regular || fontLoaded === theme.font) return;
    fontLoaded = theme.font;
    try {
      const faces = [new FontFace('Omarchy', 'url(/theme/font-regular)', {weight: '400', display: 'swap'})];
      if (theme.fonts.bold) faces.push(new FontFace('Omarchy', 'url(/theme/font-bold)', {weight: '700', display: 'swap'}));
      for (const face of faces) document.fonts.add(await face.load());
    } catch {
      // The monospace fallback stack stays in place.
    }
  }

  async function refresh() {
    if (document.hidden) return;
    try {
      const response = await fetch('/api/theme', {signal: AbortSignal.timeout(4000)});
      if (!response.ok) return;
      const theme = await response.json();
      if (theme.version === version) return;
      version = theme.version;
      const root = document.documentElement;
      for (const [name, value] of Object.entries(tokens(theme.colors))) if (value) root.style.setProperty(name, value);
      root.style.colorScheme = theme.mode === 'light' ? 'light' : 'dark';
      root.dataset.themeMode = theme.mode;
      document.querySelector('meta[name=theme-color]')?.setAttribute('content', theme.colors.background);
      loadFont(theme);
      onChange(theme);
    } catch {
      // Keep the current look; the next poll retries.
    }
  }

  refresh();
  setInterval(refresh, POLL_MS);
  document.addEventListener('visibilitychange', refresh);
  return {refresh};
}
