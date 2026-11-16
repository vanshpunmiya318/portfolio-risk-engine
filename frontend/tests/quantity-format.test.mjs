import assert from 'node:assert/strict';
import { test } from 'node:test';
import { formatQuantity, quantityPresentation } from '../src/lib/format.ts';

test('binary artifacts are visibly rounded with the supplied number retained', () => {
  for (const sign of [1, -1]) {
    const value = sign * (0.1 + 0.2);
    assert.deepEqual(quantityPresentation(value), {
      display: sign === 1 ? '≈0.3' : '≈-0.3', exact: String(value), rounded: true,
    });
  }
});

test('legitimate fractional precision is retained or explicitly disclosed', () => {
  for (const [value, display] of [
    [1.23456789012345, '1.23456789012345'],
    [1.0000000001, '1.0000000001'],
    [0.12345678901234567, '≈0.123456789012346'],
    [-0.12345678901234567, '≈-0.123456789012346'],
    [1234567890123456, '1,234,567,890,123,456'],
  ]) {
    const data = Object.freeze({ quantity: value });
    const json = JSON.stringify(data);
    const result = quantityPresentation(data.quantity);
    assert.equal(result.display, display);
    assert.equal(result.exact, String(value));
    assert.equal(result.rounded, display.startsWith('≈'));
    assert.equal(JSON.stringify(data), json);
    assert.equal(data.quantity, value);
  }
});

test('zeros, missing values and tiny signed nonzero quantities stay distinct', () => {
  for (const value of [0, -0]) {
    assert.deepEqual(quantityPresentation(value), { display: '0', exact: '0', rounded: false });
  }
  assert.equal(formatQuantity(null), 'Unavailable');
  for (const value of [Number.MIN_VALUE, 1e-320, 1e-100, 1e-11]) {
    for (const sign of [1, -1]) {
      const result = quantityPresentation(sign * value);
      assert.match(result.display, /E-/);
      assert.notEqual(Number(result.display.replace('≈', '')), 0);
      assert.equal(Math.sign(Number(result.display.replace('≈', ''))), sign);
    }
  }
});

test('scientific notation uses the rounded magnitude at the 1e-8 boundary', () => {
  for (const [value, display] of [
    [9.99999999999999e-9, '9.99999999999999E-9'],
    [9.999999999999999e-9, '≈0.00000001'],
    [1e-8, '0.00000001'],
    [1.0000000000000002e-8, '≈0.00000001'],
    [1.00000000000001e-8, '0.0000000100000000000001'],
  ]) {
    assert.equal(formatQuantity(value), display);
    assert.equal(formatQuantity(-value), display.startsWith('≈') ? `≈-${display.slice(1)}` : `-${display}`);
  }
});

test('rounded quantity disclosure exposes full supplied precision accessibly', async () => {
  const { createServer } = await import('vite');
  const { default: react } = await import('@vitejs/plugin-react');
  const { createElement } = await import('react');
  const { renderToStaticMarkup } = await import('react-dom/server');
  const server = await createServer({ configFile: false, plugins: [react()],
    server: { middlewareMode: true, hmr: false, watch: null } });
  try {
    const { QuantityValue } = await server.ssrLoadModule('/src/components/QuantityValue.tsx');
    for (const value of [0.1 + 0.2, 0.12345678901234567]) {
      const html = renderToStaticMarkup(createElement(QuantityValue, { value }));
      assert.match(html, /<details/);
      assert.match(html, /<summary aria-label="Rounded quantity/);
      assert.ok(html.includes(`Supplied quantity: ${value}`));
      assert.match(html, /Display rounded to 15 significant digits/);
    }
    assert.equal(renderToStaticMarkup(createElement(QuantityValue, { value: -0 })), '<span>0</span>');
  } finally { await server.close(); }
});
