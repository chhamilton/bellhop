"""Streamlined CLI runner (`bellhop run`) driven declaratively by `config.yaml`."""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import logging
import signal
import sys
from pathlib import Path

from rich.console import Console
from rich.panel import Panel
from rich.progress import (
  BarColumn,
  MofNCompleteColumn,
  Progress,
  SpinnerColumn,
  TaskID,
  TextColumn,
  TimeElapsedColumn,
)
from rich.table import Table

from bellhop.catalog_fetcher import CatalogFetchResult
from bellhop.config import AppConfig, load_config
from bellhop.lineup_loader import CableLineup
from bellhop.lineup_retriever import LineupRetrievalResult, LineupRetrieverError
from bellhop.mapping_state import MappingDocument
from bellhop.models import MappedChannel
from bellhop.runner import (
  PipelineConfigError,
  PipelineRunner,
  PipelineRunOptions,
  PipelineRunResult,
  parse_phase_tokens,
  parse_refresh_targets,
  resolve_active_phases,
)
from bellhop.serve import UnifiedServeServer
from bellhop.xtream import XtreamError
from bellhop.xtream_exporter import XtreamExportResult
from bellhop.xtream_server import build_access_urls

console = Console()


def _print_lineup_panel(
  res: LineupRetrievalResult, cfg: AppConfig, *, dry_run: bool
) -> None:
  lu = res.lineup
  src = (
    f"monthly cache (age: {res.age_days:.1f}d / {cfg.lineup.refresh_days:.0f}d TTL)"
    if res.from_cache
    else "fresh retrieval"
  )
  snap_note = (
    f"{res.snapshot_path} (dry-run, not written)"
    if (dry_run and not res.from_cache)
    else str(res.snapshot_path)
  )
  diff = res.diff
  console.print(
    Panel.fit(
      f"[bold]Lineup ID:[/bold]           {res.lineup_id.raw_id} "
      f"(backend=[cyan]{res.backend}[/cyan], country={res.lineup_id.country}, headend={res.lineup_id.headend})\n"
      f"[bold]Provider / Market:[/bold]   {lu.provider} — {lu.market} (Postal: {lu.postal_prefix})\n"
      f"[bold]Guide Slots:[/bold]         {lu.total_dial_positions} total dial numbers "
      f"({len(lu.channels)} unique networks; collapse_sd_dupes={cfg.lineup.collapse_sd_dupes})\n"
      f"[bold]Lineup Diff:[/bold]         +{len(diff.added_chnos)} added, "
      f"-{len(diff.removed_chnos)} removed, ~{len(diff.modified_chnos)} modified\n"
      f"[bold]Source:[/bold]              {src}\n"
      f"[bold]State Snapshot:[/bold]      {snap_note}",
      title="Phase (1) — Lineup Retrieval (`lineup`)",
      border_style="cyan",
    )
  )


def _print_catalog_panel(
  res: CatalogFetchResult, cfg: AppConfig, *, dry_run: bool
) -> None:
  acc = res.catalog.account
  src = (
    f"daily snapshot / seed (age: {res.age_hours:.1f}h / {cfg.xtream.cache_ttl_hours:.0f}h TTL)"
    if res.from_cache
    else "live player_api.php"
  )
  snap_note = (
    f"{res.snapshot_path} (dry-run, not written)"
    if (dry_run and not res.from_cache)
    else str(res.snapshot_path)
  )
  d = res.diff
  console.print(
    Panel.fit(
      f"[bold]Account:[/bold]          {acc.username}  |  [bold]Status:[/bold] {acc.status}\n"
      f"[bold]Connections:[/bold]      {acc.active_connections} active / {acc.max_connections} max\n"
      f"[bold]Categories:[/bold]       {len(res.catalog.categories)}  |  "
      f"[bold]Live Streams:[/bold] {len(res.catalog.streams):,}\n"
      f"[bold]Native Guide IDs:[/bold] {res.streams_with_native_epg:,} streams carry native epg_channel_id\n"
      f"[bold]Daily Diff:[/bold]       +{len(d.added_stream_ids):,} added, "
      f"-{len(d.removed_stream_ids):,} removed, ~{len(d.renamed_stream_ids):,} renamed\n"
      f"[bold]Source:[/bold]           {src}\n"
      f"[bold]State Snapshot:[/bold]   {snap_note}",
      title="Phase (2) — IPTV Catalog Fetch (`catalog`)",
      border_style="cyan",
    )
  )


def _print_mapping_panel(
  doc: MappingDocument, mapping_path: Path, *, saved: bool
) -> None:
  s = doc.last_summary
  reduction = (
    (1.0 - s.unique_candidate_streams / s.raw_catalog_streams) * 100.0
    if s.raw_catalog_streams > 0
    else 0.0
  )
  run_mode = (
    "Initial seed (started from empty mapping)"
    if s.was_empty_initial_run
    else "Incremental reconciliation"
  )
  persisted_label = (
    str(mapping_path) if saved else f"{mapping_path} (dry-run, not written)"
  )

  console.print(
    Panel.fit(
      f"[bold]Mode:[/bold]                {run_mode}\n"
      f"[bold]Lineup ID:[/bold]           {doc.lineup_id} ({doc.provider} — {doc.market})\n"
      f"[bold]Matched Slots:[/bold]       [green]{s.matched_slots}[/green] / {s.total_lineup_slots} "
      f"([cyan]{s.slots_with_native_epg}[/cyan] with native provider EPG)\n"
      f"[bold]Slot Changes:[/bold]        +{s.new_slots} new, ={s.unchanged_slots} unchanged, "
      f"↑{s.upgraded_slots} upgraded, ↻{s.replaced_missing_slots + s.replaced_invalid_slots} replaced, "
      f"~{s.candidates_updated_slots} alts updated, -{len(s.removed_lineup_chnos)} pruned\n"
      f"[bold]Candidate Pool:[/bold]      [bold yellow]{s.unique_candidate_streams:,}[/bold yellow] unique streams "
      f"selected from {s.raw_catalog_streams:,} raw channels ([green]{reduction:.1f}% reduction[/green])\n"
      f"[bold]Mapping State File:[/bold]  {persisted_label}",
      title="Phase (3) — Rough Channel Matcher (`match`)",
      border_style="cyan",
    )
  )


def _print_validation_panel(
  doc: MappingDocument,
  probed_unique: int,
  would_probe_slots: int,
  *,
  dry_run: bool,
  only_changed: bool,
  skipped_reason: str | None = None,
) -> None:
  valid_slots = sum(
    1
    for sl in doc.slots.values()
    if sl.preferred_source in {"validated", "pinned"}
  )
  if skipped_reason:
    console.print(
      Panel.fit(
        f"[bold yellow]Auto-Skipped:[/bold yellow]       {skipped_reason} (implicit `--skip validate`)\n"
        f"[bold]Slots Deferred:[/bold]     {would_probe_slots} slots preserved unprobed (only_changed={only_changed})\n"
        f"[bold]Currently Verified:[/bold] {valid_slots} / {len(doc.slots)} slots",
        title="Phase (4) — Candidate Stream Validation (`validate`)",
        border_style="yellow",
      )
    )
  elif dry_run:
    console.print(
      Panel.fit(
        f"[bold yellow]Dry-Run Mode:[/bold yellow]       No live stream connections opened\n"
        f"[bold]Slots Queued:[/bold]       {would_probe_slots} slots eligible for probing (only_changed={only_changed})\n"
        f"[bold]Currently Verified:[/bold] {valid_slots} / {len(doc.slots)} slots",
        title="Phase (4) — Candidate Stream Validation (`validate`)",
        border_style="yellow",
      )
    )
  else:
    console.print(
      Panel.fit(
        f"[bold]Slots Checked:[/bold]                   {would_probe_slots} (only_changed={only_changed})\n"
        f"[bold]Unique Candidate Streams Probed:[/bold] {probed_unique}\n"
        f"[bold green]Validated Active Lineup Slots:[/bold green]   {valid_slots} / {len(doc.slots)}",
        title="Phase (4) — Candidate Stream Validation (`validate`)",
        border_style="green",
      )
    )


def _print_m3u8_summary(
  lineup: CableLineup,
  mappings: list[MappedChannel],
  playlist_path: Path | None,
  report_path: Path | None,
  cfg: AppConfig,
  *,
  dry_run: bool,
) -> None:
  groups: dict[str, list[MappedChannel]] = {}
  for m in mappings:
    groups.setdefault(m.channel.group, []).append(m)

  table = Table(
    title=f"{lineup.provider} ({lineup.market}) [{lineup.lineup_id}] — Lineup Coverage"
  )
  table.add_column("Channel Group", style="bold")
  table.add_column("Active", style="green", justify="right")
  table.add_column("Native EPG", style="cyan", justify="right")
  table.add_column("Unmatched", style="red", justify="right")
  table.add_column("Total", justify="right")

  for grp_name, items in groups.items():
    active = sum(1 for m in items if m.selected is not None)
    epg = sum(
      1 for m in items if m.selected is not None and m.selected.has_native_epg
    )
    unmatched = sum(1 for m in items if m.selected is None)
    table.add_row(
      grp_name, str(active), str(epg), str(unmatched), str(len(items))
    )

  total_slots = len(mappings)
  total_active = sum(1 for m in mappings if m.selected is not None)
  total_epg = sum(
    1 for m in mappings if m.selected is not None and m.selected.has_native_epg
  )
  total_unmatched = sum(1 for m in mappings if m.selected is None)
  pct = (total_active / total_slots * 100.0) if total_slots else 0.0
  epg_pct = (total_epg / total_active * 100.0) if total_active else 0.0

  table.add_section()
  table.add_row(
    f"[bold]TOTAL ({pct:.1f}%)[/bold]",
    f"[bold green]{total_active}[/bold green]",
    f"[bold cyan]{total_epg} ({epg_pct:.0f}%)[/bold cyan]",
    f"[bold red]{total_unmatched}[/bold red]",
    f"[bold]{total_slots}[/bold]",
  )
  console.print(table)

  if dry_run:
    return

  if cfg.lineup.collapse_sd_dupes or cfg.lineup.numbering_mode == "hd":
    dial_count, dial_total = total_active, total_slots
    sd_note = "SD dupes collapsed"
  elif cfg.lineup.numbering_mode == "sd":
    dial_count, dial_total = total_active, total_slots
    sd_note = "SD numbering"
  else:
    dial_count = sum(
      (
        2
        if (
          m.channel.sd_chno is not None and m.channel.sd_chno != m.channel.chno
        )
        else 1
      )
      for m in mappings
      if m.selected is not None
    )
    dial_total = lineup.total_dial_positions
    sd_note = "full SD + HD dial"

  console.print(
    Panel.fit(
      f"[bold green]M3U8 Playlist:[/bold green]   {playlist_path}\n"
      f"[bold cyan]JSON Report:[/bold cyan]     {report_path}\n"
      f"[bold]Unique Networks:[/bold] {total_active}/{total_slots} ({pct:.1f}%), "
      f"{total_epg} with native provider EPG\n"
      f"[bold]M3U8 Dial Slots:[/bold] {dial_count}/{dial_total} channel numbers emitted ({sd_note})",
      title="Phase (5) — M3U8 & Diagnostics Generation (`m3u8`)",
      border_style="green",
    )
  )


def _print_xtream_panel(res: XtreamExportResult) -> None:
  ver = res.epg_verification
  if ver is None or ver.skipped:
    epg_status = "[yellow]Skipped[/yellow]"
  elif ver.verified:
    epg_status = (
      f"[bold green]Verified[/bold green] "
      f"(HTTP {ver.status_code or 'OK'}, {ver.format_detected}, {ver.latency_ms:.0f}ms)"
    )
  else:
    epg_status = (
      f"[bold red]Unverified[/bold red] ({ver.error or 'check failed'})"
    )

  epg_url_line = res.xmltv_url_redacted or "(not configured)"
  console.print(
    Panel.fit(
      f"[bold green]Unified Xtream Bundle:[/bold green]   {res.bundle_path}\n"
      f"[bold cyan]player_api Live Streams:[/bold cyan] {res.live_streams_path} "
      f"([bold]{res.emitted_streams}[/bold] dial entries)\n"
      f"[bold cyan]player_api Categories:[/bold cyan]   {res.live_categories_path} "
      f"([bold]{res.emitted_categories}[/bold] groups)\n"
      f"[bold cyan]EPG Passthrough:[/bold cyan]         {epg_status} "
      f"({res.channels_with_guide_id} guide-bound slots)\n"
      f"[bold]Upstream XMLTV URL:[/bold]      {epg_url_line}\n"
      f"[bold]Official Network Logos:[/bold]  {res.channels_with_official_logo} networks enriched with official logos",
      title="Phase (6) — Official Xtream JSON & Passthrough EPG Export (`xtream`)",
      border_style="green",
    )
  )


def _add_run_execution_args(parser: argparse.ArgumentParser) -> None:
  parser.add_argument(
    "-c",
    "--config",
    type=Path,
    default=None,
    help="Path to declarative YAML config file (defaults to ./config.yaml).",
  )
  parser.add_argument(
    "-p",
    "--phase",
    action="append",
    dest="phases",
    default=None,
    metavar="PHASES",
    help=(
      "Comma-separated or repeatable phase(s) to run: "
      "lineup, catalog, match, validate, m3u8, xtream (or shorthand `export` for m3u8,xtream). "
      "Defaults to all enabled phases in config.yaml."
    ),
  )
  parser.add_argument(
    "-s",
    "--skip",
    action="append",
    dest="skip_phases",
    default=None,
    metavar="PHASES",
    help="Comma-separated or repeatable phase(s) to skip on this run (e.g. `--skip validate`).",
  )
  parser.add_argument(
    "-n",
    "--dry-run",
    action="store_true",
    help="Preview lineup, catalog, and mapping changes without writing state/output or probing streams.",
  )
  parser.add_argument(
    "--force-refresh",
    nargs="*",
    default=None,
    metavar="TARGET",
    help="Bypass cache TTLs on this run (`all` if omitted, or `lineup`, `catalog`, `epg`).",
  )
  parser.add_argument(
    "--reprobe-all",
    action="store_true",
    help="In `validate` phase, probe all matched slots instead of only changed/unvalidated slots.",
  )
  parser.add_argument(
    "--reset-mapping",
    action="store_true",
    help="In `match` phase, ignore existing channel_mapping.json and rebuild from scratch.",
  )
  verbosity = parser.add_mutually_exclusive_group()
  verbosity.add_argument(
    "-q",
    "--quiet",
    action="store_true",
    help="Quiet output (suppress summary panels; ideal for unattended scheduled runs).",
  )
  verbosity.add_argument(
    "-v",
    "--verbose",
    action="store_true",
    help="Enable verbose debug logging.",
  )


def build_parser() -> argparse.ArgumentParser:
  """Build the top-level `bellhop` (`run` / `serve`) CLI argument parser."""
  parser = argparse.ArgumentParser(
    prog="bellhop",
    description=(
      "Bellhop — Declarative 6-phase IPTV lineup & EPG pipeline and unified server. "
      "All provider, lineup, validation, and path settings live in `config.yaml`; "
      "CLI flags control execution mode and phase selection."
    ),
  )
  subparsers = parser.add_subparsers(dest="command", required=False)

  p_run = subparsers.add_parser(
    "run",
    help="Execute the configured pipeline (or selected phases).",
  )
  _add_run_execution_args(p_run)

  p_serve = subparsers.add_parser(
    "serve",
    help="Run the unified Web Guide and passthrough Xtream Codes API server.",
  )
  p_serve.add_argument(
    "-c",
    "--config",
    type=Path,
    default=None,
    help="Path to declarative YAML config file (defaults to ./config.yaml).",
  )
  serve_verbosity = p_serve.add_mutually_exclusive_group()
  serve_verbosity.add_argument(
    "-q",
    "--quiet",
    action="store_true",
    help="Quiet output (suppress startup banner).",
  )
  serve_verbosity.add_argument(
    "-v",
    "--verbose",
    action="store_true",
    help="Enable verbose debug logging.",
  )

  return parser


async def _execute_cli_serve(args: argparse.Namespace) -> int:
  log_level = (
    logging.DEBUG
    if args.verbose
    else (logging.ERROR if args.quiet else logging.WARNING)
  )
  logging.basicConfig(
    level=log_level, format="%(levelname)s %(name)s: %(message)s", force=True
  )
  if not args.quiet and not args.verbose:
    for log_name in (
      "bellhop_web.epg",
      "bellhop_web.store",
      "bellhop.serve",
    ):
      logging.getLogger(log_name).setLevel(logging.INFO)

  cfg = load_config(args.config)
  server = UnifiedServeServer.from_config(cfg)

  # Validate pipeline outputs & hydrate persistent disk EPG cache before banner
  if server.web_enabled and server.web_app.epg_refresher is not None:
    server.web_app.epg_refresher.hydrate_from_cache()
  web_snap = server.web_app.store.get_snapshot() if server.web_enabled else None
  xt_snap = server.xtream_app.store.get_snapshot()

  if not args.quiet:
    url_lines = ""
    if server.shared_listener:
      urls = build_access_urls(
        server.web_bind_host,
        server.web_port,
        protocol=server.server_protocol,
      )
      url_lines += f"[bold green]Server URL:[/bold green]         [underline]{urls[0]}[/underline]\n"
      for extra_url in urls[1:]:
        url_lines += f"[bold]Local URL:[/bold]          [underline]{extra_url}[/underline]\n"
      url_lines += (
        f"[bold]Bind Address:[/bold]       {server.web_bind_host}:{server.web_port} "
        f"(shared Web Guide + Xtream API)\n"
      )
    else:
      if server.web_enabled:
        web_urls = build_access_urls(
          server.web_bind_host,
          server.web_port,
          protocol=server.server_protocol,
        )
        url_lines += (
          f"[bold green]Web Guide URL:[/bold green]      [underline]{web_urls[0]}[/underline] "
          f"(bind {server.web_bind_host}:{server.web_port})\n"
        )
      if server.xtream_enabled:
        xt_urls = build_access_urls(
          server.xtream_bind_host,
          server.xtream_port,
          protocol=server.server_protocol,
        )
        url_lines += (
          f"[bold green]Xtream API URL:[/bold green]     [underline]{xt_urls[0]}[/underline] "
          f"(bind {server.xtream_bind_host}:{server.xtream_port})\n"
        )

    cfg_label = str(cfg.config_file) if cfg.config_file else "(defaults)"

    if not server.web_enabled:
      web_label = "[yellow]Disabled[/yellow] (`web_server.enabled: false`)"
    elif server.web_auth_enabled:
      web_label = (
        f"[bold green]Enabled[/bold green] "
        f"(login user: [cyan]{cfg.serve.web.username}[/cyan], "
        f"session TTL={cfg.serve.web.session_ttl_hours:g}h; "
        f"settle={cfg.serve.web.settle_ms}ms)"
      )
    else:
      web_label = (
        f"[bold green]Enabled[/bold green] "
        f"(open access; single-tuner relay settle={cfg.serve.web.settle_ms}ms)"
      )

    if not server.xtream_enabled:
      xtream_label = (
        "[yellow]Disabled[/yellow] (`xtream_server.enabled: false`)"
      )
    elif server.xtream_auth_configured:
      xtream_label = (
        f"[bold green]Enabled[/bold green] (user: [cyan]{cfg.serve.xtream.username}[/cyan], "
        f"/player_api.php, /get.php, /xmltv.php, /live/*)"
      )
    else:
      xtream_label = (
        "[yellow]Auth Disabled[/yellow] "
        "(set `xtream_server.username` & `password` in config.yaml or credentials.yaml)"
      )

    epg_lines = ""
    if web_snap is not None:
      total_progs, active_progs, _ = web_snap.epg_coverage_stats()
      epg_ref = server.web_app.epg_refresher
      if epg_ref is not None and epg_ref.is_configured:
        will_refresh, reason = epg_ref.needs_refresh()
        epg_label = (
          f"Dynamic passthrough (TTL {cfg.epg.refresh_hours:g}h; fetching: {reason})"
          if will_refresh
          else f"Dynamic passthrough (TTL {cfg.epg.refresh_hours:g}h; cache fresh)"
        )
        epg_cache_label = str(epg_ref.config.cache_file)
      else:
        epg_label = "Not configured"
        epg_cache_label = "(none)"
      epg_lines = (
        f"[bold]EPG Guide Items:[/bold]    [cyan]{active_progs:,}[/cyan] active / "
        f"[cyan]{total_progs:,}[/cyan] cached ({epg_label})\n"
        f"[bold]EPG Disk Cache:[/bold]     {epg_cache_label}\n"
      )

    console.print(
      Panel.fit(
        f"{url_lines}"
        f"[bold]Config File:[/bold]        {cfg_label}\n"
        f"[bold]Lineup:[/bold]             {xt_snap.provider} — {xt_snap.market} [{xt_snap.lineup_id}]\n"
        f"[bold]Channels Loaded:[/bold]    [cyan]{len(xt_snap.streams)}[/cyan] channels across "
        f"[cyan]{len(xt_snap.categories)}[/cyan] lineup categories\n"
        f"[bold]Web Guide UI:[/bold]       {web_label}\n"
        f"[bold]Xtream Codes API:[/bold]   {xtream_label}\n"
        f"{epg_lines}"
        f"[bold]Playlist Source:[/bold]    {server.xtream_app.store.xtream_dir} (auto hot-reloads)",
        title="Bellhop — Unified Web Guide & Xtream API Server (`bellhop serve`)",
        border_style="cyan",
      )
    )

  # Handle SIGTERM gracefully (e.g., `docker stop`) so active tuner slots close cleanly.
  loop = asyncio.get_running_loop()
  serve_task = asyncio.create_task(server.serve_forever())

  def _on_sigterm() -> None:
    if not serve_task.done():
      serve_task.cancel()

  with contextlib.suppress(NotImplementedError):
    loop.add_signal_handler(signal.SIGTERM, _on_sigterm)

  try:
    await serve_task
  except asyncio.CancelledError:
    pass
  finally:
    with contextlib.suppress(NotImplementedError):
      loop.remove_signal_handler(signal.SIGTERM)
  return 0


async def _execute_cli_run(args: argparse.Namespace) -> int:
  log_level = (
    logging.DEBUG
    if args.verbose
    else (logging.ERROR if args.quiet else logging.WARNING)
  )
  logging.basicConfig(
    level=log_level, format="%(levelname)s %(name)s: %(message)s", force=True
  )

  cfg = load_config(args.config)
  phases = parse_phase_tokens(args.phases)
  skip_phases = parse_phase_tokens(args.skip_phases)
  refresh_targets = parse_refresh_targets(args.force_refresh)

  opts = PipelineRunOptions(
    phases=phases,
    skip_phases=skip_phases,
    dry_run=bool(args.dry_run),
    force_refresh=refresh_targets,
    reprobe_all=bool(args.reprobe_all),
    reset_mapping=bool(args.reset_mapping),
  )

  active_phases, _skipped = resolve_active_phases(
    cfg,
    include_phases=opts.phases or None,
    skip_phases=opts.skip_phases or None,
  )
  if not active_phases:
    if not args.quiet:
      console.print(
        "[yellow]No active phases selected after applying --phase / --skip.[/yellow]"
      )
    return 0

  runner = PipelineRunner(cfg)

  if not args.quiet and "validate" in active_phases and not opts.dry_run:
    with Progress(
      SpinnerColumn(),
      TextColumn("[progress.description]{task.description}"),
      BarColumn(),
      MofNCompleteColumn(),
      TimeElapsedColumn(),
      console=console,
    ) as progress:
      val_task_id: TaskID | None = None

      def _on_val_done(mapped: MappedChannel, _done: int, total: int) -> None:
        nonlocal val_task_id
        if val_task_id is None:
          val_task_id = progress.add_task(
            "Validating candidate slots", total=total
          )
        status_icon = (
          "[green]OK[/green]" if mapped.selected else "[red]FAIL[/red]"
        )
        progress.update(
          val_task_id,
          total=total,
          advance=1,
          description=f"[{mapped.channel.chno}] {mapped.channel.name[:28]} {status_icon}",
        )

      result: PipelineRunResult = await runner.run(
        opts, on_validation_progress=_on_val_done
      )
  else:
    result = await runner.run(opts)

  if args.quiet:
    return 0

  if result.lineup_result is not None and "lineup" in result.executed_phases:
    _print_lineup_panel(result.lineup_result, cfg, dry_run=result.dry_run)

  if result.catalog_result is not None and "catalog" in result.executed_phases:
    _print_catalog_panel(result.catalog_result, cfg, dry_run=result.dry_run)

  if (
    result.mapping_doc is not None
    and result.mapping_path is not None
    and "match" in result.executed_phases
  ):
    _print_mapping_panel(
      result.mapping_doc,
      result.mapping_path,
      saved=result.mapping_saved,
    )

  if result.mapping_doc is not None and (
    "validate" in result.executed_phases
    or result.validation_skipped_reason is not None
  ):
    only_changed = False if opts.reprobe_all else cfg.validation.only_changed
    _print_validation_panel(
      result.mapping_doc,
      result.validation_probed_streams,
      result.validation_would_probe_slots,
      dry_run=result.dry_run,
      only_changed=only_changed,
      skipped_reason=result.validation_skipped_reason,
    )

  if result.lineup is not None and (
    "m3u8" in result.executed_phases
    or (result.dry_run and "match" in result.executed_phases)
  ):
    _print_m3u8_summary(
      result.lineup,
      result.mappings,
      result.playlist_path,
      result.report_path,
      cfg,
      dry_run=result.dry_run,
    )

  if result.xtream_result is not None and "xtream" in result.executed_phases:
    _print_xtream_panel(result.xtream_result)

  return 0


def main(argv: list[str] | None = None) -> int:
  """Execute the `bellhop` CLI entry point and return an OS exit status code."""
  raw_argv = list(sys.argv[1:] if argv is None else argv)
  # Default to `run` subcommand when invoked as bare `bellhop` or `bellhop --skip validate`
  if not raw_argv or raw_argv[0] not in {"run", "serve", "-h", "--help"}:
    raw_argv = ["run", *raw_argv]

  parser = build_parser()
  args = parser.parse_args(raw_argv)

  try:
    if args.command == "serve":
      return asyncio.run(_execute_cli_serve(args))
    return asyncio.run(_execute_cli_run(args))
  except (XtreamError, LineupRetrieverError, PipelineConfigError) as exc:
    console.print(f"[bold red]Error:[/bold red] {exc}")
    return 2
  except FileNotFoundError as exc:
    console.print(f"[bold red]File Not Found:[/bold red] {exc}")
    return 2
  except KeyboardInterrupt:
    console.print("\n[yellow]Interrupted by user.[/yellow]")
    return 130


if __name__ == "__main__":
  sys.exit(main())
