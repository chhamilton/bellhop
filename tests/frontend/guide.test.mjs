/**
 * Unit tests for Bellhop's pure ES module guide logic (`guide_logic.js`).
 *
 * Exercises HTML escaping, badge/initials helpers, live EPG schedule window
 * progression & boundary detection, category-relative filter counts, URL state
 * round-tripping, and Settings form parsers.
 */
import assert from "node:assert/strict";
import test from "node:test";

import {
  DEFAULT_UPDATE_CHECK_INTERVAL_MS,
  TIMELINE_STICKY_COL_DEFAULT_PX,
  buildUrlQueryString,
  computeCategorySummaries,
  computeTimelineGeometry,
  createLineupUpdatePoller,
  escapeHtml,
  groupChannelsByCategory,
  hasLineupUpdated,
  makeInitials,
  parseExcludedChannelsText,
  parsePinnedStreamsText,
  parseUrlState,
  qualityBadgeClass,
  refreshScheduleWindows,
} from "../../src/bellhop_web/static/guide_logic.js";

test("escapeHtml, makeInitials, and qualityBadgeClass format safely", () => {
  assert.equal(escapeHtml(null), "");
  assert.equal(escapeHtml(undefined), "");
  assert.equal(escapeHtml(""), "");
  assert.equal(
    escapeHtml(`O'Connor & <script>"alert"</script>`),
    "O&#39;Connor &amp; &lt;script&gt;&quot;alert&quot;&lt;/script&gt;",
  );

  assert.equal(makeInitials("RDS (Montréal)"), "R");
  assert.equal(makeInitials("ICI Radio Canada (Montréal)"), "IRC");
  assert.equal(makeInitials("CTV News Channel HD"), "CNC");
  assert.equal(makeInitials(""), "T");

  assert.equal(qualityBadgeClass("4K"), "q-4k");
  assert.equal(qualityBadgeClass("fhd"), "q-fhd");
  assert.equal(qualityBadgeClass("HD"), "q-hd");
  assert.equal(qualityBadgeClass("SD"), "");
  assert.equal(TIMELINE_STICKY_COL_DEFAULT_PX, 255);
});

test("refreshScheduleWindows updates progress_pct and detects boundary crossings", () => {
  const channels = [
    {
      chno: 1108,
      sd_chno: 108,
      name: "RDS",
      callsign: "RDS",
      category_id: "2",
      category_name: "Sports",
      logo: "",
      quality_tier: "FHD",
      epg_id: "rds.ca",
      stream_path: "/api/stream/1108.ts",
      hls_path: "/api/hls/1108/index.m3u8",
      now_playing: {
        title: "Hockey",
        start_timestamp: 1000,
        stop_timestamp: 2000,
        progress_pct: 10,
      },
      up_next: null,
      schedule: [
        {
          title: "Hockey",
          start_timestamp: 1000,
          stop_timestamp: 2000,
        },
        {
          title: "Sports 30",
          start_timestamp: 2000,
          stop_timestamp: 3800,
        },
      ],
    },
  ];

  // Advancing within the same program updates progress_pct in-place without boundary crossing.
  const crossedMidShow = refreshScheduleWindows(channels, 1500);
  assert.equal(crossedMidShow, false);
  assert.equal(channels[0].now_playing.title, "Hockey");
  assert.equal(channels[0].now_playing.progress_pct, 50);
  assert.equal(channels[0].up_next.title, "Sports 30");

  // Crossing stop_timestamp flips boundaryCrossed === true and advances now_playing.
  const crossedNextShow = refreshScheduleWindows(channels, 2450);
  assert.equal(crossedNextShow, true);
  assert.equal(channels[0].now_playing.title, "Sports 30");
  assert.equal(channels[0].now_playing.progress_pct, 25);
  assert.equal(channels[0].up_next, null);
});

test("computeCategorySummaries and groupChannelsByCategory preserve category-relative counts", () => {
  const categories = [
    { category_id: "1", category_name: "News" },
    { category_id: "2", category_name: "Sports" },
    { category_id: "3", category_name: "Movies" },
  ];
  const channels = [
    {
      chno: 1001,
      sd_chno: null,
      name: "CBC News",
      callsign: "CBCNN",
      category_id: "1",
      category_name: "News",
      now_playing: { title: "The National", start_timestamp: 0, stop_timestamp: 100 },
      up_next: null,
      schedule: [],
    },
    {
      chno: 1002,
      sd_chno: null,
      name: "CPAC",
      callsign: "CPAC",
      category_id: "1",
      category_name: "News",
      now_playing: null,
      up_next: null,
      schedule: [],
    },
    {
      chno: 1108,
      sd_chno: 108,
      name: "RDS",
      callsign: "RDS",
      category_id: "2",
      category_name: "Sports",
      now_playing: { title: "Canadiens", start_timestamp: 0, stop_timestamp: 100 },
      up_next: null,
      schedule: [],
    },
  ];

  // Unfiltered badges show plain totals, with Movies marked empty.
  const unfiltered = computeCategorySummaries(categories, channels, "all", false, "");
  assert.equal(unfiltered[0].badge, "3");
  assert.equal(unfiltered[0].countClass, "cat-count");
  assert.equal(unfiltered[1].badge, "2");
  assert.equal(unfiltered[3].badge, "0");
  assert.equal(unfiltered[3].isEmpty, true);
  assert.equal(unfiltered[3].countClass, "cat-count is-empty");

  // With EPG-only filter active AND Sports selected, News still reports 1/2 matched!
  const filtered = computeCategorySummaries(categories, channels, "2", true, "");
  assert.equal(filtered[0].badge, "2/3");
  assert.equal(filtered[0].countClass, "cat-count is-matched");
  assert.equal(filtered[1].badge, "1/2");
  assert.equal(filtered[2].badge, "1/1");
  assert.equal(filtered[2].isActive, true);
  assert.equal(filtered[3].badge, "0/0");
  assert.equal(filtered[3].countClass, "cat-count is-empty");

  const groups = groupChannelsByCategory(channels, categories);
  assert.deepEqual(
    groups.map((g) => [g.category_id, g.channels.length]),
    [
      ["1", 2],
      ["2", 1],
    ],
  );
});

test("parseUrlState, buildUrlQueryString, and computeTimelineGeometry round-trip cleanly", () => {
  const categories = [
    { category_id: "1", category_name: "News" },
    { category_id: "2", category_name: "Sports" },
  ];

  const parsed = parseUrlState(
    "?cat=Sports&epg=1&q= RDS &view=timeline&offset=-60&mode=hls&ch=1108",
    "",
    categories,
  );
  assert.deepEqual(parsed, {
    selectedCategory: "2",
    hideEmptyGuide: true,
    searchRaw: " RDS ",
    searchQuery: "rds",
    viewMode: "grid",
    timelineOffsetSec: -3600,
    streamMode: "hls",
    pendingChannel: 1108,
  });

  const qs = buildUrlQueryString({
    selectedCategory: parsed.selectedCategory,
    hideEmptyGuide: parsed.hideEmptyGuide,
    searchRaw: parsed.searchRaw,
    viewMode: parsed.viewMode,
    timelineOffsetSec: parsed.timelineOffsetSec,
    streamMode: parsed.streamMode,
    activeChno: parsed.pendingChannel,
  });
  assert.equal(qs, "cat=2&epg=1&q=RDS&view=timeline&offset=-60&mode=hls&ch=1108");

  // Legacy hash fallback (#cat=1&epg=only) still parses when search string is empty.
  const fromHash = parseUrlState("", "#cat=1&epg=only", categories);
  assert.equal(fromHash.selectedCategory, "1");
  assert.equal(fromHash.hideEmptyGuide, true);

  const geom = computeTimelineGeometry(3700, 0);
  assert.equal(geom.windowStart, 3600);
  assert.equal(geom.windowEnd, 3600 + 8 * 1800);
  assert.equal(geom.trackWidthPx, 1920);
  assert.equal(geom.showNowLine, true);
});

test("parsePinnedStreamsText and parseExcludedChannelsText parse lines, JSON, and errors", () => {
  assert.deepEqual(
    parsePinnedStreamsText("# comment\n1108 = 410108\n1205:520105\n"),
    { "1108": 410108, "1205": 520105 },
  );
  assert.deepEqual(parsePinnedStreamsText('{"1108": 410108}'), {
    "1108": 410108,
  });
  assert.throws(
    () => parsePinnedStreamsText("1108_without_separator"),
    /Invalid pinned stream entry/,
  );

  assert.deepEqual(parseExcludedChannelsText("1001, 1002\n1305"), [
    1001,
    1002,
    1305,
  ]);
  assert.deepEqual(parseExcludedChannelsText("   "), []);
});

test("createLineupUpdatePoller checks lightweight timestamp and only reloads when changed", async () => {
  assert.equal(DEFAULT_UPDATE_CHECK_INTERVAL_MS, 60000);
  assert.equal(hasLineupUpdated(null, 1700000000.1), false);
  assert.equal(hasLineupUpdated(1700000000.1, 1700000000.1), false);
  assert.equal(hasLineupUpdated(1700000000.1, 1700000002.5), true);

  let clientKnownTs = 1700000000.123;
  let serverTs = 1700000000.123;
  let healthChecks = 0;
  let fullReloads = 0;
  let scheduledTick = null;
  let scheduledInterval = 0;
  let clearedTimerId = null;

  const poller = createLineupUpdatePoller({
    intervalMs: 60000,
    getKnownUpdatedAt: () => clientKnownTs,
    fetchHealth: async () => {
      healthChecks += 1;
      return {
        lineup_updated_at: serverTs,
        epg_status: { refreshing: false },
      };
    },
    onUpdateDetected: async (health) => {
      fullReloads += 1;
      clientKnownTs = health.lineup_updated_at;
    },
    setIntervalFn: (cb, ms) => {
      scheduledTick = cb;
      scheduledInterval = ms;
      return 42;
    },
    clearIntervalFn: (id) => {
      clearedTimerId = id;
    },
  });

  poller.start();
  assert.equal(scheduledInterval, 60000);
  assert.ok(typeof scheduledTick === "function");

  // Unchanged server timestamp -> lightweight check returns false, 0 full reloads!
  assert.equal(await poller.checkNow(), false);
  assert.equal(healthChecks, 1);
  assert.equal(fullReloads, 0);

  // Server updates catalog/lineup -> timestamp advances -> triggers exactly 1 full reload!
  serverTs = 1700000060.789;
  assert.equal(await poller.checkNow(), true);
  assert.equal(healthChecks, 2);
  assert.equal(fullReloads, 1);
  assert.equal(clientKnownTs, 1700000060.789);

  // Next minute tick with no further changes -> 0 additional full reloads!
  assert.equal(await poller.checkNow(), false);
  assert.equal(healthChecks, 3);
  assert.equal(fullReloads, 1);

  poller.stop();
  assert.equal(clearedTimerId, 42);
});
