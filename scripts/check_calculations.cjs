// Exercise the actual browser calculator against the published export.
// Built-in Node modules only; no browser or network is needed.
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const assert = require('node:assert/strict');
const root = path.resolve(__dirname, '..');
const data = JSON.parse(fs.readFileSync(path.join(root, 'docs/data/latest.json'), 'utf8'));
const html = fs.readFileSync(path.join(root, 'docs/index.html'), 'utf8');
const state = {data, amount: 20, ccy: 'EUR'};
const calculator = html.slice(html.indexOf('function monthlyFeeIsWaived(o)'), html.indexOf('function sourceLabel(o)'));
const display = html.slice(html.indexOf('function effectiveRate(o)'), html.indexOf('function published(o)'));
const fns = vm.runInNewContext(calculator + display + '\n({calc,effectiveRate,monthlyFeeIsWaived})', {
  S: state, fmtPct: n => `${n} %`,
});
for (const offer of data.offers) {
  const calculated = fns.calc(offer);
  for (const [browser, exported] of [['emin', 'effective_min_pct'], ['emax', 'effective_max_pct']]) {
    const expected = offer.calculation_reference[exported];
    const actual = calculated[browser];
    assert.equal(actual == null, expected == null, `${offer.id}: missing ${browser}`);
    if (actual != null) assert.ok(Math.abs(actual - expected) < 0.000051, `${offer.id}: ${actual} != ${expected}`);
  }
}
const promo = data.offers.find(o => o.id === 'CZ-payu-ecommerce-card');
assert.ok(fns.effectiveRate({...promo, _c: fns.calc(promo)}).startsWith('akční '), 'Nonzero promo must be labelled');
const synthetic = {
  variable_pct_min: 1, variable_pct_max: 1, fixed_fee_min: 0, fixed_fee_max: 0,
  fee_currency: 'CZK', monthly_fee: 100, monthly_currency: 'CZK',
  monthly_fee_waiver: {turnover_threshold: 100000, currency: 'CZK', operator: 'gt'},
};
const originalRate = data.fx.rates.EUR.czk_per_unit;
for (const [rate, expected] of [[25, 1], [20, 1.1], [10, 1.2]]) {
  data.fx.rates.EUR.czk_per_unit = rate;
  assert.ok(Math.abs(fns.calc(synthetic).emin - expected) < 1e-10, 'Waiver threshold and FX boundary');
}
data.fx.rates.EUR.czk_per_unit = originalRate;
console.log(`Verified ${data.offers.length} browser/export calculations, waiver boundaries and nonzero promo labels.`);
