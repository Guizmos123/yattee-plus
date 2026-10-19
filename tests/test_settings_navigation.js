const { test } = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

function settingsFixture() {
    let create;
    let saved = {};
    const calls = [];
    const focused = [];
    vm.runInNewContext(fs.readFileSync(path.join(__dirname,
        '../static/js/alpine/components/settings.js'), 'utf8'), {
        document: {
            addEventListener: (_event, callback) => callback(),
            getElementById: id => ({ focus: () => focused.push(id) })
        },
        Alpine: {
            data: (_name, factory) => { create = factory; },
            store: name => name === 'api' ? {
                get: async endpoint => { calls.push(['get', endpoint]); return saved; },
                put: async (endpoint, body) => { calls.push(['put', endpoint]); saved = body; }
            } : { success: () => {} }
        },
        console,
        window: { location: { hash: '' } }
    });
    const manager = create();
    manager.$nextTick = callback => callback();
    return { manager, calls, focused, saved: () => saved };
}

test('Settings starts at Security and exposes the nine sections in order', () => {
    const { manager } = settingsFixture();
    assert.equal(manager.activeSection, 'security');
    assert.deepEqual(Array.from(manager.sections, section => section.label), [
        'Security', 'InnerTube', 'Invidious Proxy', 'Feed Fetcher', 'Network',
        'Other Settings', 'yt-dlp Configuration', 'Rate Limiting', 'Cache TTL (seconds)'
    ]);
});

test('keyboard navigation wraps and keeps focus on the active tab', () => {
    const { manager, calls, focused } = settingsFixture();
    for (const [key, section] of [
        ['ArrowDown', 'innertube'], ['End', 'cache'], ['ArrowDown', 'security'],
        ['ArrowUp', 'cache'], ['Home', 'security']
    ]) {
        let prevented = false;
        manager.navigateSections({ key, preventDefault: () => { prevented = true; } });
        assert.equal(prevented, true);
        assert.equal(manager.activeSection, section);
        assert.equal(focused.at(-1), `settings-tab-${section}`);
    }
    manager.navigateSections({ key: 'Tab', preventDefault: () => assert.fail('Tab must keep native focus behavior') });
    assert.deepEqual(calls, []);
});

test('section changes retain drafts and the existing global save/load payload', async () => {
    const { manager, calls, saved } = settingsFixture();
    await manager.loadSettings();
    manager.allow_all_sites_for_extraction = true;
    manager.feed_channel_retention_days = 23;
    manager.activeSection = 'network';
    manager.yt_ip_family = 'ipv6';
    manager.activeSection = 'cache';
    manager.cache_video_ttl = 7200;
    manager.activeSection = 'security';
    assert.equal(manager.feed_channel_retention_days, 23);
    assert.deepEqual(calls, [['get', '/settings']]);
    await manager.saveSettings();
    assert.equal(saved().feed_channel_retention_days, 23);
    assert.equal(saved().yt_ip_family, 'ipv6');
    assert.equal(saved().cache_video_ttl, 7200);
    assert.equal(saved().allow_all_sites_for_extraction, true);
    assert.ok(!('activeSection' in saved()) && !('sections' in saved()));
    manager.cache_video_ttl = 60;
    await manager.loadSettings();
    assert.equal(manager.cache_video_ttl, 7200);
});

test('metadata settings retain defaults, false values, drafts, and saved values', async () => {
    const { manager, saved } = settingsFixture();
    assert.equal(manager.metadata_sync_enabled, true);
    assert.equal(manager.metadata_sync_interval_hours, 24);
    assert.equal(manager.metadata_sync_max_http_per_hour, 150);
    await manager.loadSettings();
    manager.metadata_sync_enabled = false;
    manager.metadata_sync_interval_hours = 72;
    manager.metadata_sync_max_http_per_hour = 200;
    manager.activeSection = 'network';
    manager.activeSection = 'feed';
    assert.equal(manager.metadata_sync_interval_hours, 72);
    await manager.saveSettings();
    assert.equal(saved().metadata_sync_enabled, false);
    assert.equal(saved().metadata_sync_interval_hours, 72);
    assert.equal(saved().metadata_sync_max_http_per_hour, 200);
    manager.metadata_sync_enabled = true;
    await manager.loadSettings();
    assert.equal(manager.metadata_sync_enabled, false);
    assert.equal(manager.metadata_sync_interval_hours, 72);
    for (const invalid of [0, 73, 1.5, 'invalid', '']) {
        manager.metadata_sync_interval_hours = invalid;
        await manager.saveSettings();
        assert.equal(saved().metadata_sync_interval_hours, invalid);
    }
});

test('Video metadata stays inside Feed Fetcher with labels and status refresh', () => {
    const html = fs.readFileSync(path.join(__dirname, '../static/index.html'), 'utf8');
    const feed = html.slice(html.indexOf('<!-- Feed Section -->'), html.indexOf('<!-- Network Section -->'));
    assert.ok(feed.includes('<legend class="font-medium mb-2">Video metadata</legend>'));
    for (const name of ['enabled', 'interval', 'budget']) {
        assert.ok(feed.includes(`id="metadata-sync-${name}"`));
        assert.ok(feed.includes(`for="metadata-sync-${name}"`));
    }
    assert.ok(feed.includes('loadMetadataSyncStatus()'));
    assert.ok(feed.includes('shared HTTP budget limits actual capacity'));
    assert.equal(settingsFixture().manager.sections.length, 9);
});
