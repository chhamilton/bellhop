/**
 * DOM rendering, focus-trap utilities, and in-place 30s live progress updates
 * for the Bellhop Web Guide (Cards & 2D Timeline).
 */
import { TIMELINE_SLOT_COUNT, TIMELINE_SLOT_SEC, TIMELINE_STICKY_COL_DEFAULT_PX, computeTimelineGeometry, escapeHtml, formatTime, makeInitials, qualityBadgeClass, } from "./guide_logic.js";
const FOCUSABLE_SELECTOR = [
    "button:not([disabled]):not([tabindex='-1'])",
    "[href]",
    "input:not([disabled]):not([type='hidden']):not([tabindex='-1'])",
    "select:not([disabled])",
    "textarea:not([disabled])",
    "video[controls]",
    "[tabindex]:not([tabindex='-1'])",
].join(",");
export const getVisibleFocusable = (container) => Array.from(container.querySelectorAll(FOCUSABLE_SELECTOR)).filter((el) => !el.hidden && !el.closest("[hidden]"));
export const trapFocusInContainer = (container, e) => {
    if (e.key !== "Tab")
        return;
    const focusable = getVisibleFocusable(container);
    if (focusable.length === 0) {
        e.preventDefault();
        return;
    }
    const first = focusable[0];
    const last = focusable[focusable.length - 1];
    if (!first || !last)
        return;
    const active = document.activeElement;
    if (e.shiftKey) {
        if (!active || active === first || !container.contains(active)) {
            e.preventDefault();
            last.focus();
        }
    }
    else if (!active || active === last || !container.contains(active)) {
        e.preventDefault();
        first.focus();
    }
};
export const restoreFocus = (saved, fallback) => {
    const candidate = saved && saved.isConnected && !saved.closest("[hidden]")
        ? saved
        : fallback && fallback.isConnected && !fallback.closest("[hidden]")
            ? fallback
            : null;
    candidate?.focus();
};
export const readTimelineStickyColWidthPx = () => {
    try {
        const raw = window
            .getComputedStyle(document.documentElement)
            .getPropertyValue("--tl-col-width")
            .trim();
        const parsed = parseFloat(raw);
        return Number.isFinite(parsed) && parsed > 0
            ? parsed
            : TIMELINE_STICKY_COL_DEFAULT_PX;
    }
    catch {
        return TIMELINE_STICKY_COL_DEFAULT_PX;
    }
};
export const renderLogoHtml = (ch) => {
    const fallback = escapeHtml(makeInitials(ch.callsign || ch.name));
    if (!ch.logo) {
        return `<span class="logo-fallback">${fallback}</span>`;
    }
    return `<img src="${escapeHtml(ch.logo)}" alt="${escapeHtml(ch.name)}" loading="lazy" data-logo-fallback="${fallback}" />`;
};
export const renderCategoriesHtml = (summaries) => summaries
    .map((item) => `
      <button class="cat-item ${item.isActive ? "active" : ""}${item.isEmpty ? " is-empty" : ""}" data-cat="${escapeHtml(item.category_id)}">
        <span>${escapeHtml(item.category_name)}</span>
        <span class="${item.countClass}">${item.badge}</span>
      </button>
    `)
    .join("");
export const renderCardHtml = (ch, timezone) => {
    const qTier = ch.quality_tier && ch.quality_tier !== "unknown"
        ? ch.quality_tier.toUpperCase()
        : "";
    const qBadge = qTier
        ? `<span class="quality-badge ${qualityBadgeClass(ch.quality_tier)}">${escapeHtml(qTier)}</span>`
        : "";
    const csText = ch.callsign ? `${escapeHtml(ch.callsign)} • ` : "";
    let epgHtml = `<div class="epg-empty">Live stream ready — no schedule listing at this hour</div>`;
    if (ch.now_playing) {
        const np = ch.now_playing;
        const pct = typeof np.progress_pct === "number" ? np.progress_pct : 0;
        const nextRow = ch.up_next
            ? `<div class="epg-next-row"><strong>Next ${formatTime(ch.up_next.start_timestamp, timezone)}:</strong> ${escapeHtml(ch.up_next.title)}</div>`
            : "";
        const descRow = np.description
            ? `<div class="epg-desc-snippet">${escapeHtml(np.description)}</div>`
            : "";
        epgHtml = `
      <div class="epg-now-header">
        <div class="epg-now-title" title="${escapeHtml(np.title)}">${escapeHtml(np.title)}</div>
        <div class="epg-now-time">${formatTime(np.start_timestamp, timezone)}–${formatTime(np.stop_timestamp, timezone)}</div>
      </div>
      <div class="progress-track"><div class="progress-fill" style="width:${pct.toFixed(1)}%"></div></div>
      ${descRow}
      ${nextRow}
    `;
    }
    else if (ch.up_next) {
        epgHtml = `
      <div class="epg-next-row" style="border-top:none;padding-top:0">
        <strong>Upcoming ${formatTime(ch.up_next.start_timestamp, timezone)}:</strong> ${escapeHtml(ch.up_next.title)}
      </div>
    `;
    }
    return `
    <article class="channel-card" data-chno="${ch.chno}" tabindex="0" role="button" aria-label="Watch ${escapeHtml(ch.name)}">
      <div class="card-top">
        <div class="logo-tile">${renderLogoHtml(ch)}</div>
        <div class="card-ident">
          <div class="card-title-row">
            <span class="chno-pill">${ch.chno}</span>
            <span class="ch-name" title="${escapeHtml(ch.name)}">${escapeHtml(ch.name)}</span>
            ${qBadge}
          </div>
          <div class="card-sub-row">${csText}${escapeHtml(ch.category_name)}</div>
        </div>
        <div class="play-hint" title="Watch Live">&#9654;</div>
      </div>
      <div class="card-epg">${epgHtml}</div>
    </article>
  `;
};
export const renderCardsSectionsHtml = (groups, timezone) => {
    let html = "";
    for (const grp of groups) {
        const cardsHtml = grp.channels
            .map((ch) => renderCardHtml(ch, timezone))
            .join("");
        html += `
      <div class="category-section">
        <div class="category-section-header">
          <span class="category-section-title">${escapeHtml(grp.category_name)}</span>
          <span class="category-section-count">${grp.channels.length} ${grp.channels.length === 1 ? "channel" : "channels"}</span>
        </div>
        <div class="cards-grid">${cardsHtml}</div>
      </div>
    `;
    }
    return html;
};
export const renderTimelineViewDom = (opts) => {
    const { elTimelineView, elTimelineHeader, elTimelineBody, groups, timezone, timelineOffsetSec, nowSec, } = opts;
    const prevScrollLeft = elTimelineView.scrollLeft;
    const prevScrollTop = elTimelineView.scrollTop;
    const stickyWidthPx = readTimelineStickyColWidthPx();
    const geom = computeTimelineGeometry(nowSec, timelineOffsetSec);
    let ticksHtml = "";
    for (let i = 0; i < TIMELINE_SLOT_COUNT; i += 1) {
        const slotTs = geom.windowStart + i * TIMELINE_SLOT_SEC;
        ticksHtml += `<div class="tl-tick">${formatTime(slotTs, timezone)}</div>`;
    }
    const headerNowHtml = geom.showNowLine
        ? `<div class="tl-now-badge" style="left:${geom.nowOffsetPx.toFixed(1)}px">NOW</div>
       <div class="tl-now-line" style="left:${geom.nowOffsetPx.toFixed(1)}px"></div>`
        : "";
    elTimelineHeader.innerHTML = `
    <div class="tl-corner">
      <span>Channel</span>
      <div class="tl-nav-btns">
        <button type="button" class="tl-nav-btn" data-tl-shift="-3600" title="Back 1 hour">&#9664;</button>
        <button type="button" class="tl-nav-btn" data-tl-shift="0" title="Jump to Current Time">Now</button>
        <button type="button" class="tl-nav-btn" data-tl-shift="3600" title="Forward 1 hour">&#9654;</button>
      </div>
    </div>
    <div class="tl-slots-ruler" style="width:${geom.trackWidthPx}px;min-width:${geom.trackWidthPx}px">
      ${ticksHtml}
      ${headerNowHtml}
    </div>
  `;
    const totalRowWidth = stickyWidthPx + geom.trackWidthPx;
    let rowsHtml = "";
    for (const grp of groups) {
        rowsHtml += `
      <div class="tl-category-row" style="width:${totalRowWidth}px;min-width:${totalRowWidth}px">
        <div class="tl-cat-sticky">
          <span>${escapeHtml(grp.category_name)}</span>
          <span class="tl-cat-count">${grp.channels.length}</span>
        </div>
      </div>
    `;
        for (const ch of grp.channels) {
            let blocksHtml = "";
            const sched = ch.schedule || [];
            let renderedAny = false;
            for (const p of sched) {
                if (!p.start_timestamp || !p.stop_timestamp)
                    continue;
                if (p.stop_timestamp <= geom.windowStart ||
                    p.start_timestamp >= geom.windowEnd) {
                    continue;
                }
                renderedAny = true;
                const clampedStart = Math.max(geom.windowStart, p.start_timestamp);
                const clampedStop = Math.min(geom.windowEnd, p.stop_timestamp);
                const leftPx = (clampedStart - geom.windowStart) * geom.pxPerSec;
                const widthPx = Math.max(28, (clampedStop - clampedStart) * geom.pxPerSec - 3);
                const isLive = p.start_timestamp <= nowSec && nowSec < p.stop_timestamp;
                const tip = `${p.title} (${formatTime(p.start_timestamp, timezone)}–${formatTime(p.stop_timestamp, timezone)})${p.description ? `\n${p.description}` : ""}`;
                blocksHtml += `
          <div class="tl-prog ${isLive ? "is-live" : ""}" style="left:${leftPx.toFixed(1)}px;width:${widthPx.toFixed(1)}px" title="${escapeHtml(tip)}" data-chno="${ch.chno}" tabindex="0" role="button" aria-label="${escapeHtml(`${p.title} on ${ch.name}`)}">
            <div class="tl-prog-title">${escapeHtml(p.title)}</div>
            <div class="tl-prog-time">${formatTime(p.start_timestamp, timezone)}–${formatTime(p.stop_timestamp, timezone)}</div>
          </div>
        `;
            }
            if (!renderedAny) {
                blocksHtml += `<div class="tl-empty-hint" data-chno="${ch.chno}" tabindex="0" role="button" aria-label="Watch ${escapeHtml(ch.name)}">Live broadcast — click to watch ${escapeHtml(ch.name)}</div>`;
            }
            const subLabel = ch.callsign
                ? escapeHtml(ch.callsign)
                : escapeHtml(ch.quality_tier && ch.quality_tier !== "unknown"
                    ? ch.quality_tier.toUpperCase()
                    : "LIVE");
            rowsHtml += `
        <div class="tl-row">
          <div class="tl-ch-cell" data-chno="${ch.chno}" tabindex="0" role="button" aria-label="Watch ${escapeHtml(ch.name)} (channel ${ch.chno})" title="${escapeHtml(`${ch.chno} ${ch.name}`)}">
            <span class="tl-ch-num">${ch.chno}</span>
            <div class="tl-logo">${renderLogoHtml(ch)}</div>
            <div class="tl-ch-meta">
              <div class="tl-ch-name">${escapeHtml(ch.name)}</div>
              <div class="tl-ch-sub">${subLabel}</div>
            </div>
          </div>
          <div class="tl-track" style="width:${geom.trackWidthPx}px;min-width:${geom.trackWidthPx}px" data-chno="${ch.chno}">${blocksHtml}</div>
        </div>
      `;
        }
    }
    const bodyNowHtml = geom.showNowLine
        ? `<div class="tl-now-line" style="left:${(stickyWidthPx + geom.nowOffsetPx).toFixed(1)}px"></div>`
        : "";
    elTimelineBody.innerHTML = `${rowsHtml}${bodyNowHtml}`;
    elTimelineView.scrollLeft = prevScrollLeft;
    elTimelineView.scrollTop = prevScrollTop;
};
/**
 * Update live progress bars and timeline NOW markers in place on the 30s tick
 * without replacing `innerHTML` (preserving keyboard focus and hover state).
 */
export const updateLiveProgressInPlace = (opts) => {
    if (opts.viewMode !== "grid") {
        const pctByChno = new Map();
        for (const ch of opts.channels) {
            if (ch.now_playing && typeof ch.now_playing.progress_pct === "number") {
                pctByChno.set(ch.chno, ch.now_playing.progress_pct);
            }
        }
        opts.elCardsView
            .querySelectorAll(".channel-card[data-chno]")
            .forEach((card) => {
            const chno = Number(card.dataset["chno"]);
            const pct = pctByChno.get(chno);
            if (pct === undefined)
                return;
            const fill = card.querySelector(".progress-fill");
            if (fill) {
                fill.style.width = `${pct.toFixed(1)}%`;
            }
        });
        return;
    }
    const stickyWidthPx = readTimelineStickyColWidthPx();
    const geom = computeTimelineGeometry(opts.nowSec, opts.timelineOffsetSec);
    const headerBadge = opts.elTimelineHeader.querySelector(".tl-now-badge");
    const headerLine = opts.elTimelineHeader.querySelector(".tl-now-line");
    const bodyLine = opts.elTimelineBody.querySelector(":scope > .tl-now-line");
    if (geom.showNowLine) {
        if (headerBadge)
            headerBadge.style.left = `${geom.nowOffsetPx.toFixed(1)}px`;
        if (headerLine)
            headerLine.style.left = `${geom.nowOffsetPx.toFixed(1)}px`;
        if (bodyLine) {
            bodyLine.style.left = `${(stickyWidthPx + geom.nowOffsetPx).toFixed(1)}px`;
        }
    }
};
