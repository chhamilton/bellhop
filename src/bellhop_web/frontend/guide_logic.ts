/**
 * Pure, DOM-free channel filtering, EPG progress, URL state, and config helpers.
 *
 * Imported directly by both the browser UI (`app.ts`, `render.ts`, `settings.ts`)
 * and Node unit tests (`tests/frontend/guide.test.mjs`).
 */

export const TIMELINE_STICKY_COL_DEFAULT_PX = 255;
export const TIMELINE_SLOT_COUNT = 8;
export const TIMELINE_SLOT_WIDTH_PX = 240;
export const TIMELINE_SLOT_SEC = 1800;

export interface ChannelCategoryGroup {
  category_id: string;
  category_name: string;
  channels: PublicChannel[];
}

export interface CategorySummaryItem {
  category_id: string;
  category_name: string;
  total: number;
  matched: number;
  badge: string;
  countClass: string;
  isActive: boolean;
  isEmpty: boolean;
}

export interface ParsedUrlGuideState {
  selectedCategory: string;
  hideEmptyGuide: boolean;
  searchRaw: string;
  searchQuery: string;
  viewMode: ViewMode;
  timelineOffsetSec: number;
  streamMode: StreamMode;
  pendingChannel: number | null;
}

export interface TimelineGeometry {
  windowStart: number;
  windowEnd: number;
  trackWidthPx: number;
  pxPerSec: number;
  showNowLine: boolean;
  nowOffsetPx: number;
}

export const escapeHtml = (
  str: string | number | null | undefined,
): string => {
  if (str === null || str === undefined || str === "") return "";
  return String(str)
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;")
    .replace(/'/g, "&#39;");
};

export const makeInitials = (name: string | null | undefined): string => {
  const words = String(name || "TV")
    .replace(/\([^)]*\)/g, "")
    .trim()
    .split(/\s+/)
    .filter(Boolean);
  return words
    .slice(0, 3)
    .map((w) => w.charAt(0))
    .join("")
    .toUpperCase()
    .slice(0, 4);
};

export const qualityBadgeClass = (tier: string | null | undefined): string => {
  const t = String(tier || "").toLowerCase();
  if (t === "4k") return "q-4k";
  if (t === "fhd") return "q-fhd";
  if (t === "hd") return "q-hd";
  return "";
};

export const formatTime = (
  ts: number | null | undefined,
  timezone: string,
): string => {
  if (!ts) return "";
  try {
    return new Intl.DateTimeFormat("en-CA", {
      hour: "2-digit",
      minute: "2-digit",
      hour12: false,
      timeZone: timezone,
    }).format(new Date(ts * 1000));
  } catch {
    const d = new Date(ts * 1000);
    return d.toTimeString().slice(0, 5);
  }
};

export const computeTimelineGeometry = (
  nowSec: number,
  timelineOffsetSec: number,
): TimelineGeometry => {
  const baseHalfHour =
    Math.floor(nowSec / TIMELINE_SLOT_SEC) * TIMELINE_SLOT_SEC;
  const windowStart = baseHalfHour + timelineOffsetSec;
  const trackWidthPx = TIMELINE_SLOT_COUNT * TIMELINE_SLOT_WIDTH_PX;
  const windowDuration = TIMELINE_SLOT_COUNT * TIMELINE_SLOT_SEC;
  const windowEnd = windowStart + windowDuration;
  const pxPerSec = trackWidthPx / windowDuration;
  const showNowLine = nowSec >= windowStart && nowSec <= windowEnd;
  const nowOffsetPx = (nowSec - windowStart) * pxPerSec;
  return {
    windowStart,
    windowEnd,
    trackWidthPx,
    pxPerSec,
    showNowLine,
    nowOffsetPx,
  };
};

/**
 * Recompute `now_playing` (`progress_pct`) and `up_next` from `ch.schedule`.
 * Returns `true` if any channel crossed a program boundary (requiring a full
 * card/timeline re-render rather than an in-place progress bar width update).
 */
export const refreshScheduleWindows = (
  channels: PublicChannel[],
  nowSec: number,
): boolean => {
  let boundaryCrossed = false;
  for (const ch of channels) {
    const sched = ch.schedule || [];
    if (sched.length === 0) continue;

    const prevKey = ch.now_playing
      ? `${ch.now_playing.start_timestamp}:${ch.now_playing.title}`
      : "";
    let nowP: ProgramItem | null = null;
    let nextP: ProgramItem | null = null;
    for (const p of sched) {
      if (p.stop_timestamp && p.stop_timestamp <= nowSec) continue;
      if (!nowP && p.start_timestamp <= nowSec && nowSec < p.stop_timestamp) {
        const pct =
          ((nowSec - p.start_timestamp) /
            Math.max(1, p.stop_timestamp - p.start_timestamp)) *
          100;
        nowP = { ...p, progress_pct: Math.min(100, Math.max(0, pct)) };
      } else if (!nextP && p.start_timestamp >= nowSec) {
        nextP = p;
      }
    }
    const nextKey = nowP ? `${nowP.start_timestamp}:${nowP.title}` : "";
    if (prevKey !== nextKey) {
      boundaryCrossed = true;
    }
    ch.now_playing = nowP;
    ch.up_next = nextP;
  }
  return boundaryCrossed;
};

export const hasGuideData = (ch: PublicChannel): boolean =>
  Boolean(
    ch.now_playing ||
      ch.up_next ||
      (Array.isArray(ch.schedule) && ch.schedule.length > 0),
  );

export const matchesCategory = (
  ch: PublicChannel,
  catFilter: string,
): boolean => catFilter === "all" || ch.category_id === catFilter;

export const matchesSecondaryFilters = (
  ch: PublicChannel,
  hideEmptyGuide: boolean,
  searchQuery: string,
): boolean => {
  if (hideEmptyGuide && !hasGuideData(ch)) {
    return false;
  }
  if (searchQuery) {
    const nowTitle = ch.now_playing ? ch.now_playing.title.toLowerCase() : "";
    const hay = `${ch.chno} ${ch.sd_chno ?? ""} ${ch.name.toLowerCase()} ${(ch.callsign || "").toLowerCase()} ${ch.category_name.toLowerCase()} ${nowTitle}`;
    if (!hay.includes(searchQuery)) return false;
  }
  return true;
};

export const computeCategorySummaries = (
  categories: readonly GuideCategory[],
  channels: readonly PublicChannel[],
  selectedCategory: string,
  hideEmptyGuide: boolean,
  searchQuery: string,
): CategorySummaryItem[] => {
  const filtering = Boolean(hideEmptyGuide || searchQuery);
  const countClassFor = (matched: number): string => {
    if (matched === 0) return "cat-count is-empty";
    if (filtering) return "cat-count is-matched";
    return "cat-count";
  };

  const allTotal = channels.length;
  const allMatched = filtering
    ? channels.filter((ch) =>
        matchesSecondaryFilters(ch, hideEmptyGuide, searchQuery),
      ).length
    : allTotal;

  const items: CategorySummaryItem[] = [
    {
      category_id: "all",
      category_name: "All Channels",
      total: allTotal,
      matched: allMatched,
      badge: filtering ? `${allMatched}/${allTotal}` : String(allTotal),
      countClass: countClassFor(allMatched),
      isActive: selectedCategory === "all",
      isEmpty: allMatched === 0,
    },
  ];

  for (const cat of categories) {
    const inCat = channels.filter((ch) =>
      matchesCategory(ch, cat.category_id),
    );
    const catTotal = inCat.length;
    const catMatched = filtering
      ? inCat.filter((ch) =>
          matchesSecondaryFilters(ch, hideEmptyGuide, searchQuery),
        ).length
      : catTotal;
    items.push({
      category_id: cat.category_id,
      category_name: cat.category_name,
      total: catTotal,
      matched: catMatched,
      badge: filtering ? `${catMatched}/${catTotal}` : String(catTotal),
      countClass: countClassFor(catMatched),
      isActive: selectedCategory === cat.category_id,
      isEmpty: catMatched === 0,
    });
  }
  return items;
};

export const groupChannelsByCategory = (
  channels: readonly PublicChannel[],
  categories: readonly GuideCategory[],
): ChannelCategoryGroup[] => {
  const byCat = new Map<string, ChannelCategoryGroup>();
  for (const ch of channels) {
    const cid = String(ch.category_id || "default");
    let group = byCat.get(cid);
    if (!group) {
      group = {
        category_id: cid,
        category_name: ch.category_name || "General",
        channels: [],
      };
      byCat.set(cid, group);
    }
    group.channels.push(ch);
  }

  const ordered: ChannelCategoryGroup[] = [];
  const seen = new Set<string>();
  for (const cat of categories) {
    const cid = String(cat.category_id);
    const grp = byCat.get(cid);
    if (grp && grp.channels.length > 0) {
      ordered.push(grp);
      seen.add(cid);
    }
  }
  for (const [cid, grp] of byCat.entries()) {
    if (!seen.has(cid) && grp.channels.length > 0) {
      ordered.push(grp);
    }
  }
  return ordered;
};

export const resolveSelectedCategory = (
  rawCat: string,
  categories: readonly GuideCategory[],
): string => {
  if (!rawCat || rawCat === "all") return "all";
  if (categories.length === 0) return rawCat;
  const byId = categories.find((c) => String(c.category_id) === rawCat);
  if (byId) return String(byId.category_id);
  const byName = categories.find(
    (c) =>
      String(c.category_name || "").toLowerCase() === rawCat.toLowerCase(),
  );
  return byName ? String(byName.category_id) : "all";
};

export const parseUrlState = (
  search: string,
  hash: string,
  categories: readonly GuideCategory[],
): ParsedUrlGuideState => {
  const searchStr = search.startsWith("?") ? search.slice(1) : search;
  const hashStr = hash.replace(/^#\/?/, "");
  const params = new URLSearchParams(
    searchStr || (hashStr.includes("=") ? hashStr : ""),
  );

  const rawCat = (params.get("cat") || "").trim();
  const selectedCategory = rawCat
    ? resolveSelectedCategory(rawCat, categories)
    : "all";

  const rawEpg = (params.get("epg") || params.get("hide_empty") || "")
    .trim()
    .toLowerCase();
  const hideEmptyGuide =
    rawEpg === "1" || rawEpg === "true" || rawEpg === "only";

  const searchRaw = params.get("q") || "";
  const searchQuery = searchRaw.trim().toLowerCase();

  const rawView = (params.get("view") || "").trim().toLowerCase();
  const viewMode: ViewMode =
    rawView === "timeline" || rawView === "grid" ? "grid" : "cards";

  let timelineOffsetSec = 0;
  const rawOffset = params.get("offset");
  if (rawOffset !== null && rawOffset !== "") {
    const parsedMin = Number(rawOffset);
    if (Number.isFinite(parsedMin)) {
      timelineOffsetSec = Math.max(
        -7200,
        Math.min(8 * 3600, Math.round(parsedMin) * 60),
      );
    }
  }

  const rawMode = (params.get("mode") || "").trim().toLowerCase();
  const streamMode: StreamMode = rawMode === "hls" ? "hls" : "mpegts";

  const rawCh = (params.get("ch") || "").trim();
  const parsedCh = rawCh ? Number(rawCh) : NaN;
  const pendingChannel =
    Number.isInteger(parsedCh) && parsedCh > 0 ? parsedCh : null;

  return {
    selectedCategory,
    hideEmptyGuide,
    searchRaw,
    searchQuery,
    viewMode,
    timelineOffsetSec,
    streamMode,
    pendingChannel,
  };
};

export const buildUrlQueryString = (opts: {
  selectedCategory: string;
  hideEmptyGuide: boolean;
  searchRaw: string;
  viewMode: ViewMode;
  timelineOffsetSec: number;
  streamMode: StreamMode;
  activeChno: number | null;
}): string => {
  const params = new URLSearchParams();
  if (opts.selectedCategory && opts.selectedCategory !== "all") {
    params.set("cat", opts.selectedCategory);
  }
  if (opts.hideEmptyGuide) {
    params.set("epg", "1");
  }
  if (opts.searchRaw.trim()) {
    params.set("q", opts.searchRaw.trim());
  }
  if (opts.viewMode === "grid") {
    params.set("view", "timeline");
    if (opts.timelineOffsetSec !== 0) {
      params.set("offset", String(Math.round(opts.timelineOffsetSec / 60)));
    }
  }
  if (opts.streamMode === "hls") {
    params.set("mode", "hls");
  }
  if (opts.activeChno !== null) {
    params.set("ch", String(opts.activeChno));
  }
  return params.toString();
};

export const parsePinnedStreamsText = (
  rawPins: string,
): Record<string, number> => {
  const trimmed = rawPins.trim();
  const pinnedStreams: Record<string, number> = {};
  if (!trimmed) return pinnedStreams;

  if (trimmed.startsWith("{")) {
    const parsed = JSON.parse(trimmed) as Record<string, unknown>;
    for (const [k, v] of Object.entries(parsed)) {
      pinnedStreams[String(k).trim()] = Number(v);
    }
    return pinnedStreams;
  }

  for (const line of trimmed.split(/\r?\n/)) {
    const clean = line.trim();
    if (!clean || clean.startsWith("#")) continue;
    const sep = clean.includes("=") ? "=" : ":";
    const parts = clean.split(sep);
    const left = parts[0];
    const right = parts[1];
    if (parts.length !== 2 || left === undefined || right === undefined) {
      throw new Error(
        `Invalid pinned stream entry: "${clean}" (use chno=stream_id)`,
      );
    }
    pinnedStreams[left.trim()] = Number(right.trim());
  }
  return pinnedStreams;
};

export const parseExcludedChannelsText = (rawExcl: string): number[] => {
  const trimmed = rawExcl.trim();
  if (!trimmed) return [];
  return trimmed
    .split(/[,\s]+/)
    .map((t) => t.trim())
    .filter(Boolean)
    .map((t) => Number(t));
};
