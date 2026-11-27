/**
 * Unit tests for Bellhop's pure DOM rendering & accessibility helpers (`render.js`).
 */
import assert from "node:assert/strict";
import test from "node:test";

import { FakeDocument, FakeElement } from "./_dom_stub.mjs";
import {
  getVisibleFocusable,
  readTimelineStickyColWidthPx,
  renderCardHtml,
  renderCardsSectionsHtml,
  renderCategoriesHtml,
  renderLogoHtml,
  renderTimelineViewDom,
  restoreFocus,
  trapFocusInContainer,
  updateLiveProgressInPlace,
} from "../../src/bellhop/web/static/render.js";

function withGlobals(doc, extraWindow, fn) {
  const prevDoc = globalThis.document;
  const prevWin = globalThis.window;
  globalThis.document = doc;
  globalThis.window = extraWindow;
  try {
    return fn();
  } finally {
    globalThis.document = prevDoc;
    globalThis.window = prevWin;
  }
}

function makeChannel(overrides = {}) {
  return {
    chno: 1108,
    sd_chno: 108,
    name: "RDS",
    callsign: "RDS",
    category_id: "2",
    category_name: "Sports",
    logo: "https://example.com/rds.png",
    quality_tier: "fhd",
    epg_id: "rds.ca",
    stream_url: "/api/stream/1108.ts",
    hls_url: "/api/hls/1108/index.m3u8",
    now_playing: null,
    up_next: null,
    schedule: [],
    ...overrides,
  };
}

test("getVisibleFocusable, trapFocusInContainer, and restoreFocus enforce modal focus", () => {
  const doc = new FakeDocument();
  const modal = doc.createElement("div");
  const hiddenWrap = doc.createElement("div");
  hiddenWrap.hidden = true;

  const btnFirst = doc.createElement("button");
  btnFirst.id = "btn-first";
  const btnHidden = doc.createElement("button");
  btnHidden.hidden = true;
  const btnNestedHidden = doc.createElement("button");
  hiddenWrap.appendChild(btnNestedHidden);
  const btnDisabled = doc.createElement("button");
  btnDisabled.disabled = true;
  const btnUnfocusable = doc.createElement("button");
  btnUnfocusable.setAttribute("tabindex", "-1");
  const inputHiddenTab = doc.createElement("input");
  inputHiddenTab.setAttribute("tabindex", "-1");
  const inputVisible = doc.createElement("input");
  inputVisible.id = "inp-visible";
  const btnLast = doc.createElement("button");
  btnLast.id = "btn-last";

  for (const child of [
    btnFirst,
    btnHidden,
    hiddenWrap,
    btnDisabled,
    btnUnfocusable,
    inputHiddenTab,
    inputVisible,
    btnLast,
  ]) {
    modal.appendChild(child);
  }
  doc.body.appendChild(modal);

  withGlobals(doc, {}, () => {
    const visible = getVisibleFocusable(modal);
    assert.deepEqual(
      visible.map((el) => el.id),
      ["btn-first", "inp-visible", "btn-last"],
    );

    // Non-Tab key is ignored.
    let prevented = false;
    trapFocusInContainer(modal, {
      key: "Enter",
      shiftKey: false,
      preventDefault() {
        prevented = true;
      },
    });
    assert.equal(prevented, false);

    // Shift+Tab on first focusable wraps to last.
    doc.activeElement = btnFirst;
    trapFocusInContainer(modal, {
      key: "Tab",
      shiftKey: true,
      preventDefault() {
        prevented = true;
      },
    });
    assert.equal(prevented, true);
    assert.equal(doc.activeElement, btnLast);

    // Forward Tab on last focusable wraps to first.
    prevented = false;
    doc.activeElement = btnLast;
    trapFocusInContainer(modal, {
      key: "Tab",
      shiftKey: false,
      preventDefault() {
        prevented = true;
      },
    });
    assert.equal(prevented, true);
    assert.equal(doc.activeElement, btnFirst);

    // Forward Tab on middle focusable is not intercepted.
    prevented = false;
    doc.activeElement = inputVisible;
    trapFocusInContainer(modal, {
      key: "Tab",
      shiftKey: false,
      preventDefault() {
        prevented = true;
      },
    });
    assert.equal(prevented, false);

    // Empty focusable container prevents Tab escape.
    const emptyBox = doc.createElement("div");
    doc.body.appendChild(emptyBox);
    trapFocusInContainer(emptyBox, {
      key: "Tab",
      shiftKey: false,
      preventDefault() {
        prevented = true;
      },
    });
    assert.equal(prevented, true);

    // restoreFocus prefers connected & visible saved target, else fallback.
    restoreFocus(inputVisible, btnFirst);
    assert.equal(doc.activeElement, inputVisible);
    restoreFocus(btnNestedHidden, btnFirst);
    assert.equal(doc.activeElement, btnFirst);
    const detached = new FakeElement("button", doc);
    restoreFocus(detached, btnHidden);
    assert.equal(doc.activeElement, btnFirst);
  });
});

test("readTimelineStickyColWidthPx reads CSS custom property with safe fallback", () => {
  const doc = new FakeDocument();
  withGlobals(
    doc,
    {
      getComputedStyle: () => ({
        getPropertyValue: (prop) => (prop === "--tl-col-width" ? "312px" : ""),
      }),
    },
    () => {
      assert.equal(readTimelineStickyColWidthPx(), 312);
    },
  );

  withGlobals(
    doc,
    {
      getComputedStyle: () => ({
        getPropertyValue: () => "invalid",
      }),
    },
    () => {
      assert.equal(readTimelineStickyColWidthPx(), 255);
    },
  );

  withGlobals(
    doc,
    {
      getComputedStyle: () => {
        throw new Error("no style");
      },
    },
    () => {
      assert.equal(readTimelineStickyColWidthPx(), 255);
    },
  );
});

test("renderLogoHtml, renderCategoriesHtml, renderCardHtml, and renderCardsSectionsHtml escape XSS cleanly", () => {
  const doc = new FakeDocument();
  const logoImg = renderLogoHtml(
    makeChannel({
      name: 'RDS <"Live">',
      callsign: "RDS",
      logo: 'https://img.example/rds.png?a=1&b="2"',
    }),
  );
  assert.ok(logoImg.includes("<img "));
  assert.ok(logoImg.includes("&quot;"));
  assert.ok(logoImg.includes('data-logo-fallback="R"'));

  const logoFallback = renderLogoHtml(
    makeChannel({ logo: "", callsign: "", name: "CTV News" }),
  );
  assert.equal(logoFallback, '<span class="logo-fallback">CN</span>');

  const catsHtml = renderCategoriesHtml([
    {
      category_id: "all",
      category_name: "All & <More>",
      badge: "2",
      countClass: "cat-count",
      isActive: true,
      isEmpty: false,
    },
    {
      category_id: "9",
      category_name: "Movies",
      badge: "0/3",
      countClass: "cat-count is-zero",
      isActive: false,
      isEmpty: true,
    },
  ]);
  assert.ok(catsHtml.includes("cat-item active"));
  assert.ok(catsHtml.includes("cat-item  is-empty"));
  assert.ok(catsHtml.includes("All &amp; &lt;More&gt;"));

  const nowTs = 1_764_000_000;
  const liveCard = renderCardHtml(
    makeChannel({
      name: "<script>XSS</script>",
      quality_tier: "4k",
      now_playing: {
        title: "Hockey & <Goals>",
        description: 'Live "Final" match',
        start_timestamp: nowTs - 600,
        stop_timestamp: nowTs + 1200,
        progress_pct: 33.3,
      },
      up_next: {
        title: "Postgame",
        start_timestamp: nowTs + 1200,
        stop_timestamp: nowTs + 3600,
      },
    }),
    "UTC",
  );
  assert.ok(!liveCard.includes("<script>XSS</script>"));
  assert.ok(liveCard.includes("&lt;script&gt;XSS&lt;/script&gt;"));
  assert.ok(liveCard.includes("q-4k"));
  assert.ok(liveCard.includes("width:33.3%"));
  assert.ok(liveCard.includes("Hockey &amp; &lt;Goals&gt;"));
  assert.ok(liveCard.includes("Live &quot;Final&quot; match"));
  assert.ok(liveCard.includes("Postgame"));

  const upcomingOnlyCard = renderCardHtml(
    makeChannel({
      callsign: "",
      quality_tier: "unknown",
      now_playing: null,
      up_next: {
        title: "Late Show",
        start_timestamp: nowTs + 600,
        stop_timestamp: nowTs + 2400,
      },
    }),
    "UTC",
  );
  assert.ok(upcomingOnlyCard.includes("Upcoming "));
  assert.ok(upcomingOnlyCard.includes("Late Show"));
  assert.ok(!upcomingOnlyCard.includes("quality-badge"));

  const emptyEpgCard = renderCardHtml(
    makeChannel({ now_playing: null, up_next: null }),
    "UTC",
  );
  assert.ok(emptyEpgCard.includes("Live stream ready"));

  const sectionsHtml = renderCardsSectionsHtml(
    [
      {
        category_id: "2",
        category_name: "Sports",
        channels: [makeChannel()],
      },
      {
        category_id: "3",
        category_name: "News",
        channels: [
          makeChannel({ chno: 1101, name: "RDI" }),
          makeChannel({ chno: 1102, name: "LCN" }),
        ],
      },
    ],
    "UTC",
  );
  assert.ok(sectionsHtml.includes("1 channel</span>"));
  assert.ok(sectionsHtml.includes("2 channels</span>"));

  // Silence unused import lint mentally if any
  assert.ok(doc instanceof FakeDocument);
});

test("renderTimelineViewDom and updateLiveProgressInPlace render grid & update positions in place", () => {
  const doc = new FakeDocument();
  const elTimelineView = doc.createElement("div");
  elTimelineView.scrollLeft = 42;
  elTimelineView.scrollTop = 18;
  const elTimelineHeader = doc.createElement("div");
  const elTimelineBody = doc.createElement("div");
  const elCardsView = doc.createElement("div");
  doc.body.appendChild(elTimelineView);
  elTimelineView.appendChild(elTimelineHeader);
  elTimelineView.appendChild(elTimelineBody);
  doc.body.appendChild(elCardsView);

  const nowSec = 1_764_001_200;
  const chWithSchedule = makeChannel({
    chno: 1108,
    name: "RDS",
    now_playing: {
      title: "Hockey",
      start_timestamp: nowSec - 600,
      stop_timestamp: nowSec + 1200,
      progress_pct: 40.0,
    },
    schedule: [
      {
        title: "Old Show",
        start_timestamp: nowSec - 7200,
        stop_timestamp: nowSec - 3600,
      },
      {
        title: "Hockey",
        description: "Period 2",
        start_timestamp: nowSec - 600,
        stop_timestamp: nowSec + 1200,
      },
      {
        title: "Sports 30",
        start_timestamp: nowSec + 1200,
        stop_timestamp: nowSec + 7200,
      },
    ],
  });
  const chEmpty = makeChannel({
    chno: 1109,
    name: "RDS Info",
    callsign: "",
    quality_tier: "unknown",
    schedule: [],
  });

  withGlobals(
    doc,
    {
      getComputedStyle: () => ({
        getPropertyValue: () => "260px",
      }),
    },
    () => {
      renderTimelineViewDom({
        elTimelineView,
        elTimelineHeader,
        elTimelineBody,
        groups: [
          {
            category_id: "2",
            category_name: "Sports",
            channels: [chWithSchedule, chEmpty],
          },
        ],
        timezone: "UTC",
        timelineOffsetSec: 0,
        nowSec,
      });

      assert.equal(elTimelineView.scrollLeft, 42);
      assert.equal(elTimelineView.scrollTop, 18);
      assert.ok(elTimelineHeader.innerHTML.includes("tl-now-badge"));
      assert.ok(elTimelineBody.innerHTML.includes("tl-empty-hint"));
      assert.ok(elTimelineBody.innerHTML.includes("LIVE</div>"));
      assert.ok(elTimelineBody.innerHTML.includes("Period 2"));
      assert.ok(!elTimelineBody.innerHTML.includes("Old Show"));

      // Advance in place in grid mode -> moves NOW indicators without rebuilding DOM.
      const beforeBadgeLeft = elTimelineHeader.querySelector(".tl-now-badge").style.left;
      updateLiveProgressInPlace({
        viewMode: "grid",
        elCardsView,
        elTimelineHeader,
        elTimelineBody,
        channels: [chWithSchedule, chEmpty],
        timelineOffsetSec: 0,
        nowSec: nowSec + 300,
      });
      const afterBadgeLeft = elTimelineHeader.querySelector(".tl-now-badge").style.left;
      assert.notEqual(afterBadgeLeft, beforeBadgeLeft);
      assert.ok(
        parseFloat(elTimelineBody.querySelector(":scope > .tl-now-line").style.left) >
          260,
      );

      // Update in-place in cards mode -> updates .progress-fill width.
      elCardsView.innerHTML = renderCardsSectionsHtml(
        [{ category_id: "2", category_name: "Sports", channels: [chWithSchedule] }],
        "UTC",
      );
      chWithSchedule.now_playing.progress_pct = 75.5;
      updateLiveProgressInPlace({
        viewMode: "cards",
        elCardsView,
        elTimelineHeader,
        elTimelineBody,
        channels: [chWithSchedule],
        timelineOffsetSec: 0,
        nowSec: nowSec + 600,
      });
      assert.equal(
        elCardsView.querySelector('.channel-card[data-chno="1108"] .progress-fill').style.width,
        "75.5%",
      );

      // Large positive shift hides NOW line cleanly.
      renderTimelineViewDom({
        elTimelineView,
        elTimelineHeader,
        elTimelineBody,
        groups: [{ category_id: "2", category_name: "Sports", channels: [chEmpty] }],
        timezone: "UTC",
        timelineOffsetSec: 8 * 3600,
        nowSec,
      });
      assert.equal(elTimelineHeader.querySelector(".tl-now-badge"), null);
    },
  );
});
