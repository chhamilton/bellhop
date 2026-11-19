/**
 * Single-tuner live video player overlay controller (`mpegts.js` & `hls.js`).
 */

import { escapeHtml, formatTime, qualityBadgeClass } from "./guide_logic.js";
import { renderLogoHtml, restoreFocus } from "./render.js";

export interface PlayerStateSlice {
  channels: PublicChannel[];
  filteredChannels: PublicChannel[];
  activeChannel: PublicChannel | null;
  streamMode: StreamMode;
  timezone: string;
}

export interface PlayerControllerDeps {
  pageClientId: string;
  state: PlayerStateSlice;
  elOverlay: HTMLElement;
  elBackdrop: HTMLElement;
  elPlayerStage: HTMLElement;
  elVideo: WebkitHTMLVideoElement;
  elUnmuteBtn: HTMLButtonElement;
  elClickPlayBox: HTMLElement;
  elClickPlayBtn: HTMLButtonElement;
  elSpinner: HTMLElement;
  elStatusText: HTMLElement;
  elErrorBox: HTMLElement;
  elErrorText: HTMLElement;
  elPlayerLogoWrap: HTMLElement;
  elPlayerChno: HTMLElement;
  elPlayerName: HTMLElement;
  elPlayerQuality: HTMLElement;
  elPlayerLivePill: HTMLElement;
  elPlayerCategory: HTMLElement;
  elPlayerNowTitle: HTMLElement;
  elPlayerNowTime: HTMLElement;
  elPlayerProgressWrap: HTMLElement;
  elPlayerProgressBar: HTMLElement;
  elPlayerNowDesc: HTMLElement;
  elPlayerUpcomingList: HTMLElement;
  elSettingsOverlay: HTMLElement | null;
  requireEl: <T extends HTMLElement>(id: string) => T;
  syncUrlFromState: (opts?: { replace?: boolean }) => void;
  syncModeButtons: () => void;
}

export interface PlayerController {
  openChannelOverlay(
    chno: number | string | undefined,
    opts?: { updateUrl?: boolean; replaceUrl?: boolean; fromDeepLink?: boolean },
  ): void;
  closeOverlay(opts?: {
    updateUrl?: boolean;
    replaceUrl?: boolean;
  }): Promise<void>;
  toggleFullscreen(): Promise<void>;
  stepChannel(delta: number): void;
  destroyActivePlayers(notifyServerRelease?: boolean): void;
}

const errorMessage = (err: unknown): string =>
  err instanceof Error ? err.message : String(err);

export const createPlayerController = (
  deps: PlayerControllerDeps,
): PlayerController => {
  const {
    pageClientId,
    state,
    elOverlay,
    elBackdrop,
    elPlayerStage,
    elVideo,
    elUnmuteBtn,
    elClickPlayBox,
    elClickPlayBtn,
    elSpinner,
    elStatusText,
    elErrorBox,
    elErrorText,
    elPlayerLogoWrap,
    elPlayerChno,
    elPlayerName,
    elPlayerQuality,
    elPlayerLivePill,
    elPlayerCategory,
    elPlayerNowTitle,
    elPlayerNowTime,
    elPlayerProgressWrap,
    elPlayerProgressBar,
    elPlayerNowDesc,
    elPlayerUpcomingList,
    elSettingsOverlay,
    requireEl,
    syncUrlFromState,
    syncModeButtons,
  } = deps;

  let mpegtsPlayer: MpegtsPlayer | null = null;
  let hlsPlayer: HlsInstance | null = null;
  let autoMuted = false;
  let lastFocusedBeforePlayer: HTMLElement | null = null;

  const destroyActivePlayers = (notifyServerRelease = false): void => {
    if (mpegtsPlayer) {
      try {
        mpegtsPlayer.pause();
        mpegtsPlayer.unload();
        mpegtsPlayer.detachMediaElement();
        mpegtsPlayer.destroy();
      } catch {
        // Ignore media teardown errors on rapid channel switches.
      }
      mpegtsPlayer = null;
    }
    if (hlsPlayer) {
      try {
        hlsPlayer.destroy();
      } catch {
        // Ignore HLS teardown errors.
      }
      hlsPlayer = null;
    }
    try {
      elVideo.pause();
      elVideo.removeAttribute("src");
      elVideo.load();
    } catch {
      // Ignore video reset errors.
    }

    if (notifyServerRelease) {
      fetch(
        `/api/tuner/release?client=${encodeURIComponent(pageClientId)}`,
        {
          method: "POST",
          keepalive: true,
        },
      ).catch(() => {});
    }
    elUnmuteBtn.hidden = true;
    elClickPlayBox.hidden = true;
  };

  const setPlayerStatus = (
    status: "connecting" | "live" | "error",
    message = "",
  ): void => {
    elPlayerLivePill.classList.remove("is-live", "is-error");
    if (status === "connecting") {
      elPlayerLivePill.textContent = "TUNING…";
      elStatusText.textContent = message || "Connecting to live stream…";
      elSpinner.hidden = false;
      elErrorBox.hidden = true;
      elClickPlayBox.hidden = true;
    } else if (status === "live") {
      elPlayerLivePill.textContent = `LIVE • ${state.streamMode === "mpegts" ? "TS" : "HLS"}`;
      elPlayerLivePill.classList.add("is-live");
      elSpinner.hidden = true;
      elErrorBox.hidden = true;
      elClickPlayBox.hidden = true;
    } else {
      elPlayerLivePill.textContent = "OFFLINE / ERROR";
      elPlayerLivePill.classList.add("is-error");
      elSpinner.hidden = true;
      elClickPlayBox.hidden = true;
      elUnmuteBtn.hidden = true;
      elErrorText.textContent =
        message || "Unable to play stream in current mode.";
      elErrorBox.hidden = false;
    }
  };

  const populateOverlayEpg = async (ch: PublicChannel): Promise<void> => {
    const renderProgramPanel = (programs: readonly ProgramItem[]): void => {
      const nowSec = Math.floor(Date.now() / 1000);
      const tz = state.timezone;
      const nowProg =
        programs.find(
          (p) => p.start_timestamp <= nowSec && nowSec < p.stop_timestamp,
        ) || ch.now_playing;
      const upcoming = programs
        .filter((p) => p.start_timestamp > nowSec)
        .slice(0, 6);

      if (nowProg) {
        elPlayerNowTitle.textContent = nowProg.title;
        elPlayerNowTime.textContent = `${formatTime(nowProg.start_timestamp, tz)} – ${formatTime(nowProg.stop_timestamp, tz)}`;
        const pct =
          ((nowSec - nowProg.start_timestamp) /
            Math.max(1, nowProg.stop_timestamp - nowProg.start_timestamp)) *
          100;
        elPlayerProgressWrap.hidden = false;
        elPlayerProgressBar.style.width = `${Math.min(100, Math.max(0, pct)).toFixed(1)}%`;
        elPlayerNowDesc.textContent =
          nowProg.description || "Live broadcast in progress.";
      } else {
        elPlayerNowTitle.textContent = ch.name;
        elPlayerNowTime.textContent = "Live Broadcast";
        elPlayerProgressWrap.hidden = true;
        elPlayerNowDesc.textContent =
          "No program description listed for this time slot.";
      }

      if (upcoming.length > 0) {
        elPlayerUpcomingList.innerHTML = upcoming
          .map(
            (p) => `
            <div class="upcoming-item">
              <span class="upcoming-time">${formatTime(p.start_timestamp, tz)}–${formatTime(p.stop_timestamp, tz)}</span>
              <span class="upcoming-title" title="${escapeHtml(p.title)}">${escapeHtml(p.title)}</span>
            </div>`,
          )
          .join("");
      } else {
        elPlayerUpcomingList.innerHTML = `<div class="epg-empty">No upcoming listings available.</div>`;
      }
    };

    renderProgramPanel(ch.schedule || []);
    try {
      const resp = await fetch(`/api/epg/${ch.chno}`);
      if (resp.ok) {
        const data = (await resp.json()) as ChannelEpgResponse;
        if (
          state.activeChannel &&
          state.activeChannel.chno === ch.chno &&
          Array.isArray(data.programs)
        ) {
          renderProgramPanel(data.programs);
        }
      }
    } catch {
      // Keep locally cached schedule if EPG detail fetch fails.
    }
  };

  const attemptPlayback = async (
    playFn: () => Promise<void> | void,
  ): Promise<void> => {
    try {
      const p = playFn();
      if (p && typeof p.then === "function") {
        await p;
      }
      elClickPlayBox.hidden = true;
      if (autoMuted && elVideo.muted) {
        elUnmuteBtn.hidden = false;
      }
    } catch (err: unknown) {
      if (!state.activeChannel) return;
      const errName = err instanceof Error ? err.name : "";
      const errMsg = errorMessage(err);
      const isAutoplayBlocked =
        errName === "NotAllowedError" ||
        /autoplay|user gesture|didn't interact|not allowed/i.test(errMsg);
      if (!isAutoplayBlocked) return;

      if (!elVideo.muted) {
        elVideo.muted = true;
        autoMuted = true;
        try {
          const p2 = playFn();
          if (p2 && typeof p2.then === "function") {
            await p2;
          }
          elClickPlayBox.hidden = true;
          elUnmuteBtn.hidden = false;
          return;
        } catch {
          // Fall through to explicit click-to-play button.
        }
      }
      elSpinner.hidden = true;
      elClickPlayBox.hidden = false;
    }
  };

  const startPlayback = (
    ch: PublicChannel,
    { preferMuted = false }: { preferMuted?: boolean } = {},
  ): void => {
    destroyActivePlayers(false);
    setPlayerStatus("connecting", `Tuning ${ch.chno} ${ch.name}…`);

    if (preferMuted) {
      elVideo.muted = true;
      autoMuted = true;
      elUnmuteBtn.hidden = false;
    } else if (autoMuted) {
      elVideo.muted = false;
      autoMuted = false;
      elUnmuteBtn.hidden = true;
    }

    const cacheBuster = Date.now();
    const clientParam = encodeURIComponent(pageClientId);

    if (state.streamMode === "mpegts") {
      if (window.mpegts && window.mpegts.isSupported()) {
        const absStreamUrl = new URL(
          `${ch.stream_url}?t=${cacheBuster}&client=${clientParam}`,
          window.location.origin,
        ).href;
        const player = window.mpegts.createPlayer(
          {
            type: "mpegts",
            isLive: true,
            url: absStreamUrl,
          },
          {
            enableWorker: true,
            enableStashBuffer: false,
            stashInitialSize: 128 * 1024,
            liveBufferLatencyChasing: true,
            liveBufferLatencyMaxLatency: 4.0,
            liveBufferLatencyMinRemain: 0.8,
          },
        );
        mpegtsPlayer = player;
        player.attachMediaElement(elVideo);
        player.on(window.mpegts.Events.ERROR, (_errType, errDetail) => {
          if (mpegtsPlayer !== player) return;
          setPlayerStatus(
            "error",
            `MPEG-TS playback error (${errDetail || "stream interrupted"}). Try switching to HLS mode.`,
          );
        });
        player.load();
        void attemptPlayback(() => player.play());
      } else {
        state.streamMode = "hls";
        syncModeButtons();
        syncUrlFromState({ replace: true });
        startPlayback(ch, { preferMuted });
      }
    } else {
      const hlsUrl = `${ch.hls_url}?t=${cacheBuster}&client=${clientParam}`;
      if (window.Hls && window.Hls.isSupported()) {
        const hls = new window.Hls({
          lowLatencyMode: true,
          maxBufferLength: 15,
        });
        hlsPlayer = hls;
        hls.loadSource(hlsUrl);
        hls.attachMedia(elVideo);
        hls.on(window.Hls.Events.MANIFEST_PARSED, () => {
          void attemptPlayback(() => elVideo.play());
        });
        hls.on(window.Hls.Events.ERROR, (_ev, data) => {
          if (data && data.fatal && hlsPlayer === hls) {
            setPlayerStatus(
              "error",
              `HLS error: ${data.details || "manifest/network failure"}. Try MPEG-TS mode.`,
            );
          }
        });
      } else if (elVideo.canPlayType("application/vnd.apple.mpegurl")) {
        elVideo.src = hlsUrl;
        void attemptPlayback(() => elVideo.play());
      } else {
        setPlayerStatus(
          "error",
          "HLS playback is not supported in this browser.",
        );
      }
    }
  };

  elVideo.addEventListener("canplay", () => {
    if (state.activeChannel && elVideo.paused && elErrorBox.hidden) {
      void attemptPlayback(() => elVideo.play());
    }
  });

  elVideo.addEventListener("playing", () => {
    if (state.activeChannel) {
      setPlayerStatus("live");
      if (autoMuted && elVideo.muted) {
        elUnmuteBtn.hidden = false;
      }
    }
  });

  elVideo.addEventListener("volumechange", () => {
    if (!elVideo.muted) {
      autoMuted = false;
      elUnmuteBtn.hidden = true;
    }
  });

  elVideo.addEventListener("waiting", () => {
    if (state.activeChannel && elErrorBox.hidden && elClickPlayBox.hidden) {
      elSpinner.hidden = false;
      elStatusText.textContent = "Buffering live stream…";
    }
  });

  const openChannelOverlay = (
    chno: number | string | undefined,
    {
      updateUrl = true,
      replaceUrl = false,
      fromDeepLink = false,
    }: { updateUrl?: boolean; replaceUrl?: boolean; fromDeepLink?: boolean } = {},
  ): void => {
    const ch = state.channels.find((c) => c.chno === Number(chno));
    if (!ch) return;
    const wasHidden = elOverlay.hidden;
    if (wasHidden && document.activeElement instanceof HTMLElement) {
      lastFocusedBeforePlayer = document.activeElement;
    }
    state.activeChannel = ch;

    elPlayerLogoWrap.innerHTML = renderLogoHtml(ch);
    elPlayerChno.textContent = String(ch.chno);
    elPlayerName.textContent = ch.name;
    elPlayerCategory.textContent = `${ch.callsign ? `${ch.callsign} • ` : ""}${ch.category_name}`;

    if (ch.quality_tier && ch.quality_tier !== "unknown") {
      elPlayerQuality.hidden = false;
      elPlayerQuality.className = `quality-badge ${qualityBadgeClass(ch.quality_tier)}`;
      elPlayerQuality.textContent = ch.quality_tier.toUpperCase();
    } else {
      elPlayerQuality.hidden = true;
    }

    elOverlay.hidden = false;
    document.body.style.overflow = "hidden";
    if (wasHidden) {
      requireEl<HTMLElement>("close-player-btn").focus();
    }

    if (updateUrl) {
      syncUrlFromState({ replace: replaceUrl });
    }

    void populateOverlayEpg(ch);
    startPlayback(ch, { preferMuted: fromDeepLink });
  };

  const closeOverlay = async ({
    updateUrl = true,
    replaceUrl = false,
  }: { updateUrl?: boolean; replaceUrl?: boolean } = {}): Promise<void> => {
    if (document.fullscreenElement) {
      try {
        await document.exitFullscreen();
      } catch {
        // Ignore fullscreen exit errors.
      }
    }
    state.activeChannel = null;
    elOverlay.hidden = true;
    if (!elSettingsOverlay || elSettingsOverlay.hidden) {
      document.body.style.overflow = "";
    }
    destroyActivePlayers(true);
    if (updateUrl) {
      syncUrlFromState({ replace: replaceUrl });
    }
    const toRestore = lastFocusedBeforePlayer;
    lastFocusedBeforePlayer = null;
    restoreFocus(toRestore);
  };

  const toggleFullscreen = async (): Promise<void> => {
    try {
      if (document.fullscreenElement) {
        await document.exitFullscreen();
      } else if (elPlayerStage.requestFullscreen) {
        await elPlayerStage.requestFullscreen();
      } else if (elVideo.webkitEnterFullscreen) {
        elVideo.webkitEnterFullscreen();
      }
    } catch {
      // Ignore fullscreen permission errors.
    }
  };

  const stepChannel = (delta: number): void => {
    if (!state.activeChannel) return;
    const activeChno = state.activeChannel.chno;
    const pool =
      state.filteredChannels.length > 0
        ? state.filteredChannels
        : state.channels;
    if (pool.length === 0) return;
    const idx = pool.findIndex((c) => c.chno === activeChno);
    const nextIdx = idx === -1 ? 0 : (idx + delta + pool.length) % pool.length;
    const target = pool[nextIdx];
    if (target) {
      openChannelOverlay(target.chno);
    }
  };

  // Overlay button bindings
  requireEl<HTMLElement>("close-player-btn").addEventListener("click", () => {
    void closeOverlay();
  });
  elBackdrop.addEventListener("click", () => {
    void closeOverlay();
  });
  elUnmuteBtn.addEventListener("click", () => {
    elVideo.muted = false;
    autoMuted = false;
    elUnmuteBtn.hidden = true;
    if (elVideo.paused) {
      elVideo.play().catch(() => {});
    }
  });
  elClickPlayBtn.addEventListener("click", () => {
    elVideo.muted = false;
    autoMuted = false;
    elUnmuteBtn.hidden = true;
    elClickPlayBox.hidden = true;
    elVideo.play().catch(() => {});
  });
  requireEl<HTMLElement>("fullscreen-btn").addEventListener("click", () => {
    void toggleFullscreen();
  });
  elPlayerStage.addEventListener("dblclick", () => {
    void toggleFullscreen();
  });
  requireEl<HTMLElement>("prev-ch-btn").addEventListener("click", () =>
    stepChannel(-1),
  );
  requireEl<HTMLElement>("next-ch-btn").addEventListener("click", () =>
    stepChannel(1),
  );
  requireEl<HTMLElement>("pip-btn").addEventListener("click", async () => {
    try {
      if (document.pictureInPictureElement) {
        await document.exitPictureInPicture();
      } else if (elVideo.requestPictureInPicture) {
        await elVideo.requestPictureInPicture();
      }
    } catch {
      // Ignore PiP rejection errors.
    }
  });
  document.querySelectorAll<HTMLElement>("[data-mode]").forEach((btn) => {
    btn.addEventListener("click", () => {
      state.streamMode = btn.dataset["mode"] === "hls" ? "hls" : "mpegts";
      syncModeButtons();
      syncUrlFromState({ replace: true });
      if (state.activeChannel) startPlayback(state.activeChannel);
    });
  });
  requireEl<HTMLElement>("retry-stream-btn").addEventListener("click", () => {
    if (state.activeChannel) startPlayback(state.activeChannel);
  });
  requireEl<HTMLElement>("switch-mode-btn").addEventListener("click", () => {
    state.streamMode = state.streamMode === "mpegts" ? "hls" : "mpegts";
    syncModeButtons();
    syncUrlFromState({ replace: true });
    if (state.activeChannel) startPlayback(state.activeChannel);
  });

  return {
    openChannelOverlay,
    closeOverlay,
    toggleFullscreen,
    stepChannel,
    destroyActivePlayers,
  };
};
