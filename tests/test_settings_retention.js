const { test } = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

function managerFor(saved = {}) {
    let factory;
    let payload;
    const api = {
        get: async () => saved,
        put: async (endpoint, settings) => {
            assert.equal(endpoint, '/settings');
            payload = settings;
        }
    };
    const context = {
        document: { addEventListener: (_event, callback) => callback() },
        Alpine: {
            data: (_name, create) => { factory = create; },
            store: name => name === 'api' ? api : { success: () => {} }
        },
        console,
        window: { location: { hash: '' } }
    };
    const filename = path.join(__dirname, '../static/js/alpine/components/settings.js');
    vm.runInNewContext(fs.readFileSync(filename, 'utf8'), context);
    return { manager: factory(), payload: () => payload };
}

test('retention defaults to 14 and supports older settings responses', async () => {
    const { manager } = managerFor();
    assert.equal(manager.feed_channel_retention_days, 14);
    await manager.loadSettings();
    assert.equal(manager.feed_channel_retention_days, 14);
});

test('retention loads and saves through the existing settings API', async () => {
    for (const days of [1, 14, 30]) {
        const fixture = managerFor({ feed_channel_retention_days: days, feed_video_max_age: 90 });
        await fixture.manager.loadSettings();
        assert.equal(fixture.manager.feed_channel_retention_days, days);
        await fixture.manager.saveSettings();
        assert.equal(fixture.payload().feed_channel_retention_days, days);
        assert.equal(fixture.payload().feed_video_max_age, 90);
    }
});

test('invalid input is not silently replaced by the default before server validation', async () => {
    for (const value of [0, 31, -1, 'invalid', '']) {
        const fixture = managerFor();
        fixture.manager.feed_channel_retention_days = value;
        await fixture.manager.saveSettings();
        assert.equal(fixture.payload().feed_channel_retention_days, value);
    }
});

test('numeric control reuses Feed Fetcher markup and existing input styles', () => {
    const html = fs.readFileSync(path.join(__dirname, '../static/index.html'), 'utf8');
    const feed = html.slice(html.indexOf('<!-- Feed Section -->'), html.indexOf('<!-- Network Section -->'));
    const input = feed.match(/<input[^>]*id="feed-channel-retention-days"[^>]*>/)[0];
    for (const attribute of ['type="number"', 'min="1"', 'max="30"', 'step="1"',
        'class="input-base"', 'x-model.number="feed_channel_retention_days"']) {
        assert.ok(input.includes(attribute), attribute);
    }
    assert.ok(feed.includes('Deleted Channel Retention (days)'));
    assert.ok(feed.includes('Number of days to retain deleted channels before permanent cleanup (1–30 days).'));
});
