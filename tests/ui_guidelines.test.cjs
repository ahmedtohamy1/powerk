const assert = require('node:assert/strict');
const fs = require('node:fs');
const { test } = require('node:test');

const source = fs.readFileSync('powerk.py', 'utf8');
const css = source.match(/<style>([\s\S]*?)<\/style>/)[1];
const guidelines = fs.readFileSync('UI_GUIDELINES.md', 'utf8');
const rootPattern = /:root(?:\[data-theme=dark\])?\s*\{([^}]+)\}/g;
function declarations(body) {
  return Object.fromEntries(body.split(';').map(part => part.trim()).filter(Boolean).map(part => {
    const colon = part.indexOf(':');
    return [part.slice(0, colon).trim(), part.slice(colon + 1).trim()];
  }));
}
const roots = [...css.matchAll(rootPattern)].map(match => declarations(match[1]));
const themes = { light: roots[0], dark: {...roots[0], ...roots[1]} };
const rules = [...css.matchAll(/([^{}]+)\{([^{}]*)\}/g)]
  .map(match => ({selector:match[1].trim(), values:declarations(match[2])}));
function rgb(hex) {
  assert.match(hex, /^#[a-f\d]{6}$/i, 'Contrast checks require opaque six-digit color tokens');
  return [1, 3, 5].map(offset => parseInt(hex.slice(offset, offset + 2), 16));
}
function luminance(hex) {
  const channels = rgb(hex).map(value => {
    const channel = value / 255;
    return channel <= 0.04045 ? channel / 12.92 : ((channel + 0.055) / 1.055) ** 2.4;
  });
  return channels[0] * 0.2126 + channels[1] * 0.7152 + channels[2] * 0.0722;
}
function contrast(a, b) {
  const values = [luminance(a), luminance(b)].sort((a, b) => b - a);
  return (values[0] + 0.05) / (values[1] + 0.05);
}
function checkPair(theme, foreground, background, minimum) {
  const ratio = contrast(theme[foreground], theme[background]);
  assert.ok(ratio >= minimum, `${foreground} on ${background}: ${ratio.toFixed(2)}:1, expected >= ${minimum}:1`);
}

test('component colors use documented semantic tokens in both themes', () => {
  assert.equal(roots.length, 2);
  const componentCss = css.replace(rootPattern, '');
  assert.doesNotMatch(componentCss, /#[a-f\d]{3,8}\b|(?:rgba?|hsla?|oklch|color-mix)\(/i);
  assert.doesNotMatch(source, /style\s*=/i, 'Inline styles bypass the shared design rules');
  assert.doesNotMatch(css, /--(?:green|mint)\b/);
  const paletteKeys = Object.keys(roots[0]).filter(key => /^#[a-f\d]{6}$/i.test(roots[0][key]));
  assert.deepEqual(Object.keys(roots[1]).filter(key => roots[1][key].startsWith('#')).sort(), paletteKeys.sort());
  for (const [name, theme] of Object.entries(themes)) {
    for (const key of paletteKeys) {
      assert.ok(guidelines.includes('`' + key + '`'), `${key} must be documented`);
      assert.ok(guidelines.includes(theme[key]), `${name} ${key} value must match the guidelines`);
    }
  }
  for (const match of css.matchAll(/var\((--[\w-]+)/g)) {
    assert.ok(match[1] in themes.light, `Undefined design token: ${match[1]}`);
  }
});

for (const [name, theme] of Object.entries(themes)) {
  test(`${name} theme text colors achieve 4.5:1 contrast`, () => {
    for (const surface of ['--bg', '--surface', '--subtle']) {
      for (const text of ['--ink', '--muted', '--accent']) checkPair(theme, text, surface, 4.5);
    }
    checkPair(theme, '--accent-ink', '--accent-soft', 4.5);
    checkPair(theme, '--danger', '--danger-bg', 4.5);
  });

  test(`${name} theme controls and switch states achieve 3:1 contrast`, () => {
    for (const surface of ['--bg', '--surface', '--subtle']) {
      checkPair(theme, '--accent', surface, 3);
      checkPair(theme, '--control-border', surface, 3);
    }
    checkPair(theme, '--thumb', '--accent', 3);
    checkPair(theme, '--thumb', '--control-border', 3);
    checkPair(theme, '--danger-border', '--danger-bg', 3);
  });

  test(`${name} theme has neutral surfaces and a blue accent`, () => {
    for (const token of ['--bg', '--surface', '--subtle']) {
      const channels = rgb(theme[token]);
      assert.ok(Math.max(...channels) - Math.min(...channels) <= 20, `${token} must remain neutral`);
    }
    const [red, green, blue] = rgb(theme['--accent']);
    assert.ok(blue > red && blue > green, 'Accent must remain blue');
  });
}

test('spacing, typography and radii use the approved scales', () => {
  const scale = prefix => Object.entries(roots[0]).filter(([key]) => key.startsWith(prefix)).map(([, value]) => parseFloat(value));
  assert.deepEqual(scale('--space-'), [4, 8, 12, 16, 20, 24, 32, 40, 48, 64]);
  assert.deepEqual(scale('--text-'), [12, 14, 16, 20, 24, 28, 32, 40, 48]);
  assert.deepEqual(scale('--weight-'), [400, 500, 600, 700]);
  assert.deepEqual(scale('--radius-'), [8, 12, 16, 999]);
  assert.deepEqual(scale('--leading-'), [1.5, 1.2, 1.1]);
  for (const rule of rules) {
    if (rule.selector.startsWith(':root')) continue;
    for (const [property, value] of Object.entries(rule.values)) {
      if (property === 'line-height') {
        assert.ok(value.startsWith('var(--leading-') || (rule.selector === '.metric-icon' && value === '1'), `${rule.selector} line-height bypasses tokens`);
        continue;
      }
      if (!/^(?:padding|margin)(?:-(?:top|bottom|left|right))?$|^(?:gap|row-gap|column-gap|font-size|font-weight|letter-spacing|border-radius)$/.test(property)) continue;
      if (property === 'border-radius' && rule.selector.includes('.socket::') && value === '2px') continue;
      const literal = value.replace(/var\(--[\w-]+\)/g, '').trim();
      assert.ok(!literal || /^(?:0|auto|50%|inherit|\s)+$/.test(literal), `${rule.selector} ${property}: ${value} bypasses tokens`);
    }
  }
});

test('controls keep minimum hit areas, visible focus and reduced-motion support', () => {
  assert.equal(roots[0]['--control-size'], '44px');
  assert.equal(roots[0]['--control-wide'], '52px');
  assert.equal(roots[0]['--duration-fast'], '160ms');
  for (const selector of ['button', '.actions button', '.toggle', 'summary']) {
    const rule = rules.find(rule => rule.selector === selector);
    assert.equal(rule.values['min-height'], 'var(--control-size)', selector);
    for (const override of rules.filter(rule => rule.selector === selector && rule.values['min-height'])) {
      assert.equal(override.values['min-height'], 'var(--control-size)', `${selector} must not shrink in a media query`);
    }
  }
  assert.equal(rules.find(rule => rule.selector === 'button').values['min-width'], 'var(--control-size)');
  assert.match(css, /:focus-visible[^{}]*\{outline:3px solid var\(--accent\);outline-offset:4px\}/);
  assert.match(css, /@media\(prefers-reduced-motion:reduce\)/);
  assert.doesNotMatch(css, /outline\s*:\s*(?:0|none)|animation\s*:/);
});

test('long readings and metadata have explicit wrapping rules', () => {
  for (const selector of ['.metric-value>span', '.outlet-power', '.strip h3', '.device-detail-text', '.setup code']) {
    assert.equal(rules.find(rule => rule.selector === selector).values['overflow-wrap'], 'anywhere', selector);
  }
  assert.equal(rules.find(rule => rule.selector === '.grid').values['grid-template-columns'], 'repeat(4,minmax(0,1fr))');
  assert.ok(rules.some(rule => rule.selector === '.grid' && rule.values['grid-template-columns'] === 'repeat(2,minmax(0,1fr))'));
});

test('large panels and active outlets stay neutral', () => {
  for (const selector of ['.metric', '.metric.primary', '.outlet.is-on']) {
    const rule = rules.find(rule => rule.selector === selector);
    assert.equal(rule.values.background, 'var(--surface)', selector);
  }
  assert.doesNotMatch(css, /\.primary::after|gradient\(/);
  assert.equal(rules.find(rule => rule.selector === '#error[data-kind=success]').values.background, 'var(--subtle)');
});
