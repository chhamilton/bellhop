"""End-to-end tests for Bellhop's Alpine Docker packaging, entrypoint, and runtime.

Covers:
  1. Static contract validation of `Dockerfile`, `docker/entrypoint.sh`,
     `docker/config.docker.yaml`, `docker-compose.example.yml`, and `.env.example`.
  2. Live subprocess end-to-end execution of `docker/entrypoint.sh` driving both
     `bellhop run --skip validate` and `bellhop serve` against `FakeXtreamServer`
     with surfaced login credentials, `TZ`, isolated mount dirs, and `SIGTERM`.
  3. Live `docker build` + `docker run` container end-to-end test (executed
     automatically whenever a Docker/Podman daemon is available; skipped cleanly
     in environments without a container runtime).
"""

from __future__ import annotations

import asyncio
import os
import shutil
import signal
import socket
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

import pytest
import yaml

from bellhop.config import load_config
from bellhop_web.http_utils import UPSTREAM_ERRORS
from tests.fake_xtream_server import (
  FakeProgram,
  FakeStreamDef,
  FakeXtreamServer,
)
from tests.http_mock import TestHttpClient

REPO_ROOT = Path(__file__).resolve().parents[1]


def _pick_free_port() -> int:
  with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
    sock.bind(("127.0.0.1", 0))
    return int(sock.getsockname()[1])


def _detect_container_cli() -> str | None:
  for cli in ("docker", "podman"):
    binary = shutil.which(cli)
    if not binary:
      continue
    probe = subprocess.run(
      [binary, "info"],
      capture_output=True,
      text=True,
      check=False,
      timeout=10,
    )
    if probe.returncode == 0:
      return binary
  return None


def test_dockerfile_compose_and_env_contract(
  tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
  dockerfile = (REPO_ROOT / "Dockerfile").read_text(encoding="utf-8")
  assert "FROM node:22-alpine AS frontend-builder" in dockerfile
  assert "FROM python:3.13-alpine AS runtime" in dockerfile
  assert "COPY package.json package-lock.json tsconfig.json ./" in dockerfile
  assert "npm ci --no-audit --no-fund" in dockerfile
  assert "ARG INSTALL_FFMPEG=1" in dockerfile
  assert "pip install --no-cache-dir -r requirements.lock" in dockerfile
  assert "pip install --no-cache-dir --no-deps ." in dockerfile
  for pkg in ("tzdata", "su-exec", "ffmpeg", "ca-certificates"):
    assert pkg in dockerfile
  for mount in ("/config", "/data/state", "/data/cache", "/data/output"):
    assert mount in dockerfile
  assert "ENV TZ=UTC" in dockerfile
  assert "PUID=1000" in dockerfile
  assert "PGID=1000" in dockerfile
  assert "/api/auth/status" in dockerfile
  assert 'CMD ["bellhop", "serve"]' in dockerfile
  assert "bellhop-web" not in dockerfile

  # Validate POSIX shell syntax and executable bit of docker/entrypoint.sh & bellhop-shim.sh.
  for script_name in ("entrypoint.sh", "bellhop-shim.sh"):
    script_path = REPO_ROOT / "docker" / script_name
    assert script_path.is_file()
    assert os.access(script_path, os.X_OK), (
      f"docker/{script_name} must be executable"
    )
    sh_check = subprocess.run(
      ["sh", "-n", str(script_path)],
      capture_output=True,
      text=True,
      check=False,
    )
    assert sh_check.returncode == 0, (
      f"{script_name} syntax error: {sh_check.stderr}"
    )

  entrypoint_text = (REPO_ROOT / "docker" / "entrypoint.sh").read_text(
    encoding="utf-8"
  )
  assert "chown -R" not in entrypoint_text
  assert "find " in entrypoint_text

  # Validate docker-compose.example.yml structure & surfaced credentials/mounts.
  compose: dict[str, Any] = yaml.safe_load(
    (REPO_ROOT / "docker-compose.example.yml").read_text(encoding="utf-8")
  )
  svc = compose["services"]["bellhop"]
  assert ".env" in svc["env_file"]
  env_map = svc["environment"]
  for key in (
    "TZ",
    "PUID",
    "PGID",
    "BELLHOP_WEB_USERNAME",
    "BELLHOP_WEB_PASSWORD",
    "BELLHOP_SERVE_USERNAME",
    "BELLHOP_SERVE_PASSWORD",
    "XTREAM_SERVER_URL",
    "XTREAM_USERNAME",
    "XTREAM_PASSWORD",
  ):
    assert key in env_map, f"Missing {key} in docker-compose.example.yml"

  volumes = svc["volumes"]
  for target in (":/config", ":/data/state", ":/data/cache", ":/data/output"):
    assert any(target in str(v) for v in volumes), (
      f"Missing {target} mount in docker-compose.example.yml"
    )

  # Validate .env.example exposes all container + login credentials.
  env_example = (REPO_ROOT / ".env.example").read_text(encoding="utf-8")
  for key in (
    "TZ=",
    "PUID=",
    "PGID=",
    "BELLHOP_PORT=",
    "BELLHOP_WEB_USERNAME=",
    "BELLHOP_WEB_PASSWORD=",
    "BELLHOP_SERVE_USERNAME=",
    "BELLHOP_SERVE_PASSWORD=",
    "XTREAM_SERVER_URL=",
    "XTREAM_USERNAME=",
    "XTREAM_PASSWORD=",
  ):
    assert key in env_example, f"Missing {key} in .env.example"

  # Validate docker/config.docker.yaml loads cleanly with env credentials.
  monkeypatch.setenv("BELLHOP_WEB_USERNAME", "docker_admin")
  monkeypatch.setenv("BELLHOP_WEB_PASSWORD", "docker_web_pw")
  monkeypatch.setenv("BELLHOP_SERVE_USERNAME", "docker_xt")
  monkeypatch.setenv("BELLHOP_SERVE_PASSWORD", "docker_xt_pw")
  monkeypatch.setenv("XTREAM_SERVER_URL", "http://upstream.local:8080")
  monkeypatch.setenv("XTREAM_USERNAME", "up_u")
  monkeypatch.setenv("XTREAM_PASSWORD", "up_p")

  seeded_cfg = tmp_path / "config.yaml"
  seeded_cfg.write_text(
    (REPO_ROOT / "docker" / "config.docker.yaml").read_text(encoding="utf-8"),
    encoding="utf-8",
  )
  cfg = load_config(seeded_cfg)
  assert cfg.serve.web.username == "docker_admin"
  assert cfg.serve.web.password == "docker_web_pw"
  assert cfg.serve.xtream.username == "docker_xt"
  assert cfg.serve.xtream.password == "docker_xt_pw"
  assert cfg.xtream.server_url == "http://upstream.local:8080"
  assert str(cfg.state.state_dir) == "/data/state"
  assert str(cfg.xtream.cache_dir) == "/data/cache"
  assert str(cfg.output.xtream_dir) == "/data/output/xtream"


def test_requirements_lock_and_vendored_player_provenance() -> None:
  """Verify pinned `requirements.lock` and `static/vendor/VERSIONS.md` digests."""
  import hashlib
  import importlib.metadata as md
  import re

  lock_path = REPO_ROOT / "requirements.lock"
  assert lock_path.is_file()
  locked: dict[str, str] = {}
  for raw_line in lock_path.read_text(encoding="utf-8").splitlines():
    line = raw_line.strip()
    if not line or line.startswith("#"):
      continue
    spec_part = line.split(";", 1)[0].strip()
    assert "==" in spec_part, f"Unpinned entry in requirements.lock: {line}"
    pkg_name, pkg_ver = spec_part.split("==", 1)
    norm_name = re.sub(r"[-_.]+", "-", pkg_name.strip()).lower()
    locked[norm_name] = pkg_ver.strip()
    # If marker applies to the running interpreter, verify .venv matches lockfile.
    if ";" not in line or (sys.version_info < (3, 11)):
      installed_ver = md.version(pkg_name.strip())
      assert installed_ver == pkg_ver.strip(), (
        f"requirements.lock pins {pkg_name}=={pkg_ver}, "
        f"but .venv has {installed_ver}"
      )

  for required_pkg in (
    "aiohttp",
    "pyyaml",
    "python-dotenv",
    "rich",
    "ruamel-yaml",
  ):
    assert required_pkg in locked, (
      f"Missing top-level runtime dependency {required_pkg} in requirements.lock"
    )

  vendor_dir = REPO_ROOT / "src" / "bellhop_web" / "static" / "vendor"
  versions_md = (vendor_dir / "VERSIONS.md").read_text(encoding="utf-8")
  assert "Apache-2.0" in versions_md

  rows = re.findall(
    r"^\|\s*`([^`]+)`\s*\|\s*`([^`]+)`\s*\|\s*`([^`]+)`\s*\|\s*([^|]+?)\s*\|"
    r"\s*([^|]+?)\s*\|\s*`(\d+)`\s*\|\s*`([0-9a-f]{64})`\s*\|$",
    versions_md,
    re.MULTILINE,
  )
  by_file = {
    fname: (ver, int(size_s), sha)
    for fname, _lib, ver, _lic, _url, size_s, sha in rows
  }
  assert set(by_file) == {"mpegts.js", "hls.min.js"}

  for fname, (ver, expected_bytes, expected_sha) in by_file.items():
    raw_bytes = (vendor_dir / fname).read_bytes()
    actual_sha = hashlib.sha256(raw_bytes).hexdigest()
    assert len(raw_bytes) == expected_bytes, (
      f"{fname} byte size mismatch: expected {expected_bytes}, got {len(raw_bytes)}"
    )
    assert actual_sha == expected_sha, (
      f"{fname} SHA-256 mismatch: expected {expected_sha}, got {actual_sha}"
    )
    assert ver in raw_bytes.decode("utf-8", errors="replace")


@pytest.mark.asyncio
async def test_container_entrypoint_end_to_end_pipeline_and_serve(
  tmp_path: Path,
) -> None:
  """Execute `docker/entrypoint.sh` end-to-end with isolated mount directories."""
  now_ts = int(time.time())
  fake_xtream = FakeXtreamServer(
    username="up_user",
    password="up_secret",
    categories=[{"category_id": "10", "category_name": "CANADA FR"}],
    streams=[
      FakeStreamDef(
        stream_id=710101,
        name="CA-FR: ICI RADIO-CANADA MONTREAL HD",
        category_id="10",
        epg_channel_id="ICITeleMontreal.ca",
      )
    ],
    programs=[
      FakeProgram(
        channel_id="ICITeleMontreal.ca",
        title="Le Téléjournal Montréal",
        description="Bulletin de nouvelles.",
        start_ts=now_ts - 600,
        stop_ts=now_ts + 1800,
      )
    ],
  )
  await fake_xtream.start()

  serve_proc: subprocess.Popen[str] | None = None
  try:
    cfg_dir = tmp_path / "config"
    state_dir = tmp_path / "data" / "state"
    cache_dir = tmp_path / "data" / "cache"
    output_dir = tmp_path / "data" / "output"
    port = _pick_free_port()

    # Build a minimal container-layout default config template for entrypoint seeding.
    lineup_yaml = tmp_path / "mini_lineup.yaml"
    lineup_yaml.write_text(
      """\
provider: "Bell Fibe TV"
market: "Montréal, QC"
postal_prefix: "H2Y"
channels:
  - chno: 1101
    sd_chno: 101
    name: "ICI Radio-Canada Télé (Montréal)"
    callsign: "CBFT"
    group: "Francophone - Généralistes"
    language: "fr"
    tvg_id: "ICITeleMontreal.ca"
    aliases: ["ICI Radio-Canada Montreal", "Radio-Canada Montreal", "CBFT"]
""",
      encoding="utf-8",
    )

    default_tpl = tmp_path / "config.docker.test.yaml"
    tpl_text = (REPO_ROOT / "docker" / "config.docker.yaml").read_text(
      encoding="utf-8"
    )
    tpl_text = (
      tpl_text.replace(
        '"/config/credentials.yaml"', f'"{cfg_dir / "credentials.yaml"}"'
      )
      .replace('"/data/cache"', f'"{cache_dir}"')
      .replace(
        '"/data/state/channel_mapping.json"',
        f'"{state_dir / "channel_mapping.json"}"',
      )
      .replace('"/data/state"', f'"{state_dir}"')
      .replace(
        '"/app/data/lineups/bell_fibe_montreal.yaml"',
        f'"{lineup_yaml}"',
      )
      .replace(
        '"/data/output/bellhop.m3u8"', f'"{output_dir / "bellhop.m3u8"}"'
      )
      .replace(
        '"/data/output/lineup_report.json"',
        f'"{output_dir / "lineup_report.json"}"',
      )
      .replace('"/data/output/xtream"', f'"{output_dir / "xtream"}"')
      .replace(
        '  host: "0.0.0.0"\n  port: 8080',
        f'  host: "127.0.0.1"\n  port: {port}',
      )
      .replace('backend: "auto"', 'backend: "yaml"')
    )
    default_tpl.write_text(tpl_text, encoding="utf-8")

    venv_bin = str(Path(sys.executable).parent)
    env = os.environ.copy()
    env.update(
      {
        "PATH": f"{venv_bin}:{env.get('PATH', '')}",
        "TZ": "America/Montreal",
        "BELLHOP_CONFIG_DIR": str(cfg_dir),
        "BELLHOP_STATE_DIR": str(state_dir),
        "BELLHOP_CACHE_DIR": str(cache_dir),
        "BELLHOP_OUTPUT_DIR": str(output_dir),
        "BELLHOP_CONFIG": str(cfg_dir / "config.yaml"),
        "BELLHOP_DEFAULT_CONFIG": str(default_tpl),
        "BELLHOP_WEB_USERNAME": "e2e_admin",
        "BELLHOP_WEB_PASSWORD": "e2e_web_password",
        "BELLHOP_SERVE_USERNAME": "e2e_xt_user",
        "BELLHOP_SERVE_PASSWORD": "e2e_xt_password",
        "XTREAM_SERVER_URL": fake_xtream.base_url,
        "XTREAM_USERNAME": "up_user",
        "XTREAM_PASSWORD": "up_secret",
      }
    )

    entrypoint = str(REPO_ROOT / "docker" / "entrypoint.sh")

    # 1. First invocation seeds /config/config.yaml and runs pipeline via entrypoint.
    run_proc = await asyncio.to_thread(
      subprocess.run,
      [entrypoint, "bellhop", "run", "--skip", "validate", "--quiet"],
      cwd=REPO_ROOT,
      env=env,
      capture_output=True,
      text=True,
      check=False,
      timeout=30,
    )
    assert run_proc.returncode == 0, (
      f"entrypoint bellhop run failed:\n{run_proc.stdout}\n{run_proc.stderr}"
    )
    assert (cfg_dir / "config.yaml").is_file()
    assert (state_dir / "channel_mapping.json").is_file()
    assert (output_dir / "bellhop.m3u8").is_file()
    assert (output_dir / "xtream" / "bellhop_xtream_playlist.json").is_file()

    # 2. Launch `entrypoint.sh` with default CMD (`bellhop serve`) and verify HTTP E2E.
    serve_proc = subprocess.Popen(
      [entrypoint, "bellhop", "serve", "--quiet"],
      cwd=REPO_ROOT,
      env=env,
      stdout=subprocess.PIPE,
      stderr=subprocess.PIPE,
      text=True,
    )

    base_url = f"http://127.0.0.1:{port}"
    async with TestHttpClient(
      base_url=base_url, follow_redirects=False, timeout=5.0
    ) as client:
      # Poll the exact Docker HEALTHCHECK endpoint until ready.
      ready = False
      for _ in range(50):
        try:
          r = await client.get("/api/auth/status")
          if r.status_code == 200:
            data = r.json()
            assert data["auth_required"] is True
            assert data["authenticated"] is False
            ready = True
            break
        except UPSTREAM_ERRORS:
          await asyncio.sleep(0.1)
      assert ready, "bellhop serve did not become healthy in time"

      # Unauthenticated `/` redirects to `/login`, while logo/favicon/prefs are public.
      root_r = await client.get("/")
      assert root_r.status_code == 302
      assert root_r.headers["location"] == "/login"
      assert (await client.get("/static/bellhop-logo.svg")).status_code == 200
      assert (await client.get("/favicon.ico")).status_code == 200
      assert (await client.get("/static/prefs.js")).status_code == 200

      # Authenticate using the surfaced BELLHOP_WEB_USERNAME / PASSWORD env vars.
      login_r = await client.post(
        "/api/auth/login",
        json={"username": "e2e_admin", "password": "e2e_web_password"},
      )
      assert login_r.status_code == 200
      assert login_r.json()["authenticated"] is True

      lineup_r = await client.get("/api/lineup")
      assert lineup_r.status_code == 200
      channels = lineup_r.json()["channels"]
      assert len(channels) == 1
      assert channels[0]["chno"] == 1101

      # Stream live MPEG-TS through single-tuner relay.
      async with client.stream("GET", "/api/stream/1101.ts?client=e2e") as st:
        assert st.status_code == 200
        first_chunk = b""
        async for chunk in st.aiter_bytes():
          first_chunk += chunk
          if len(first_chunk) >= 188:
            break
        assert first_chunk[0] == 0x47

      # Passthrough Xtream API honors surfaced BELLHOP_SERVE_USERNAME / PASSWORD.
      xt_r = await client.get(
        "/player_api.php",
        params={"username": "e2e_xt_user", "password": "e2e_xt_password"},
      )
      assert xt_r.status_code == 200
      assert xt_r.json()["user_info"]["auth"] == 1

      epg_r = await client.get(
        "/xmltv.php",
        params={"username": "e2e_xt_user", "password": "e2e_xt_password"},
      )
      assert epg_r.status_code == 302
      assert fake_xtream.base_url in epg_r.headers["location"]

    # 3. Send SIGTERM (just like `docker stop`) and verify clean exit 0.
    serve_proc.send_signal(signal.SIGTERM)
    exit_code = await asyncio.to_thread(serve_proc.wait, 10)
    assert exit_code == 0, f"Expected clean SIGTERM exit 0, got {exit_code}"
    serve_proc = None
  finally:
    if serve_proc is not None and serve_proc.poll() is None:
      serve_proc.terminate()
      serve_proc.wait(timeout=5)
    await fake_xtream.stop()


@pytest.mark.asyncio
async def test_docker_image_build_and_container_e2e(tmp_path: Path) -> None:
  """Build the real Alpine Docker image and test it E2E against FakeXtreamServer."""
  container_cli = _detect_container_cli()
  if container_cli is None:
    pytest.skip("docker/podman daemon not available in this environment")

  image_tag = "bellhop:e2e-test"
  container_name = f"bellhop-e2e-{int(time.time())}"
  build = await asyncio.to_thread(
    subprocess.run,
    [container_cli, "build", "-t", image_tag, "."],
    cwd=REPO_ROOT,
    capture_output=True,
    text=True,
    check=False,
    timeout=300,
  )
  assert build.returncode == 0, f"Container build failed:\n{build.stderr}"

  # Start a live FakeXtreamServer on the host reachable from inside the container.
  now_ts = int(time.time())
  fake_xtream = FakeXtreamServer(
    host="0.0.0.0",
    public_host="host.docker.internal",
    username="up_user",
    password="up_secret",
    max_connections=1,
    enforce_max_connections=True,
    categories=[
      {"category_id": "10", "category_name": "CANADA FR"},
      {"category_id": "20", "category_name": "CANADA SPORTS"},
    ],
    streams=[
      FakeStreamDef(
        stream_id=710101,
        name="CA-FR: ICI RADIO-CANADA MONTREAL HD",
        category_id="10",
        epg_channel_id="ICITeleMontreal.ca",
      ),
      FakeStreamDef(
        stream_id=710401,
        name="CA-EN: TSN 1 FHD",
        category_id="20",
        epg_channel_id="TSN1.ca",
      ),
    ],
    programs=[
      FakeProgram(
        channel_id="ICITeleMontreal.ca",
        title="Le Téléjournal Montréal",
        description="Bulletin de nouvelles national.",
        start_ts=now_ts - 600,
        stop_ts=now_ts + 1800,
      ),
      FakeProgram(
        channel_id="TSN1.ca",
        title="SportsCentre",
        description="Top sports highlights.",
        start_ts=now_ts - 600,
        stop_ts=now_ts + 1800,
      ),
    ],
  )
  await fake_xtream.start()

  cfg_dir = tmp_path / "config"
  state_dir = tmp_path / "state"
  cache_dir = tmp_path / "cache"
  output_dir = tmp_path / "output"
  for d in (cfg_dir, state_dir, cache_dir, output_dir):
    d.mkdir(parents=True, exist_ok=True)

  host_port = _pick_free_port()
  uid = os.getuid()
  gid = os.getgid()

  try:
    start = await asyncio.to_thread(
      subprocess.run,
      [
        container_cli,
        "run",
        "-d",
        "--name",
        container_name,
        "--add-host=host.docker.internal:host-gateway",
        "-p",
        f"127.0.0.1:{host_port}:8080",
        "-e",
        "TZ=America/Montreal",
        "-e",
        f"PUID={uid}",
        "-e",
        f"PGID={gid}",
        "-e",
        "BELLHOP_WEB_USERNAME=container_admin",
        "-e",
        "BELLHOP_WEB_PASSWORD=container_pw",
        "-e",
        "BELLHOP_SERVE_USERNAME=container_xt",
        "-e",
        "BELLHOP_SERVE_PASSWORD=container_xt_pw",
        "-e",
        f"XTREAM_SERVER_URL={fake_xtream.base_url}",
        "-e",
        "XTREAM_USERNAME=up_user",
        "-e",
        "XTREAM_PASSWORD=up_secret",
        "-v",
        f"{cfg_dir}:/config",
        "-v",
        f"{state_dir}:/data/state",
        "-v",
        f"{cache_dir}:/data/cache",
        "-v",
        f"{output_dir}:/data/output",
        image_tag,
      ],
      capture_output=True,
      text=True,
      check=False,
      timeout=30,
    )
    assert start.returncode == 0, f"Container start failed:\n{start.stderr}"

    async with TestHttpClient(
      base_url=f"http://127.0.0.1:{host_port}",
      follow_redirects=False,
      timeout=10.0,
    ) as client:
      # 1. Wait for Docker HEALTHCHECK endpoint (/api/auth/status).
      ready = False
      for _ in range(60):
        try:
          r = await client.get("/api/auth/status")
          if r.status_code == 200 and r.json()["auth_required"] is True:
            ready = True
            break
        except UPSTREAM_ERRORS:
          await asyncio.sleep(0.25)
      if not ready:
        logs = subprocess.run(
          [container_cli, "logs", container_name],
          capture_output=True,
          text=True,
          check=False,
        )
        pytest.fail(
          f"Bellhop container did not respond on /api/auth/status.\n"
          f"STDOUT:\n{logs.stdout}\nSTDERR:\n{logs.stderr}"
        )

      # 2. Verify auto-seeded config, PUID/PGID ownership, non-root process, and TZ.
      seeded = cfg_dir / "config.yaml"
      assert seeded.is_file()
      st = seeded.stat()
      assert st.st_uid == uid
      assert st.st_gid == gid

      tz_check = await asyncio.to_thread(
        subprocess.run,
        [container_cli, "exec", container_name, "date", "+%Z"],
        capture_output=True,
        text=True,
        check=False,
        timeout=10,
      )
      assert tz_check.returncode == 0
      assert tz_check.stdout.strip() in {"EST", "EDT"}

      # 3. Run the 6-phase pipeline via plain `docker exec` (NO `-u` flag!):
      #    `docker exec` starts as uid 0, and `/usr/local/bin/bellhop` must auto-drop
      #    privileges to PUID:PGID so volume files are never created as root.
      raw_exec_id = await asyncio.to_thread(
        subprocess.run,
        [container_cli, "exec", container_name, "id", "-u"],
        capture_output=True,
        text=True,
        check=False,
        timeout=10,
      )
      assert raw_exec_id.stdout.strip() == "0"

      run_pipe = await asyncio.to_thread(
        subprocess.run,
        [
          container_cli,
          "exec",
          container_name,
          "bellhop",
          "run",
          "--skip",
          "validate",
          "--quiet",
        ],
        capture_output=True,
        text=True,
        check=False,
        timeout=60,
      )
      assert run_pipe.returncode == 0, (
        f"In-container `bellhop run` failed:\n{run_pipe.stdout}\n{run_pipe.stderr}"
      )

      # Verify mounted state & output artifacts exist on host with PUID:PGID ownership.
      mapping_file = state_dir / "channel_mapping.json"
      m3u8_file = output_dir / "bellhop.m3u8"
      xt_bundle = output_dir / "xtream" / "bellhop_xtream_playlist.json"
      for artifact in (mapping_file, m3u8_file, xt_bundle):
        assert artifact.is_file(), f"Missing expected artifact {artifact}"
        assert artifact.stat().st_uid == uid
        assert artifact.stat().st_gid == gid

      # 4. Verify unauthenticated redirect, public brand/favicon/prefs assets, and login.
      root_r = await client.get("/")
      assert root_r.status_code == 302
      assert root_r.headers["location"] == "/login"
      assert (await client.get("/login")).status_code == 200
      assert (await client.get("/static/bellhop-logo.svg")).status_code == 200
      assert (await client.get("/favicon.ico")).status_code == 200
      assert (await client.get("/favicon.svg")).status_code == 200
      assert (await client.get("/static/prefs.js")).status_code == 200
      assert (await client.get("/static/app.js")).status_code == 401

      login_r = await client.post(
        "/api/auth/login",
        json={"username": "container_admin", "password": "container_pw"},
      )
      assert login_r.status_code == 200
      assert login_r.json()["authenticated"] is True

      index_r = await client.get("/")
      assert index_r.status_code == 200
      assert "/static/bellhop-logo.svg" in index_r.text
      assert "/static/prefs.js" in index_r.text
      assert (await client.get("/static/guide_logic.js")).status_code == 200

      # 5. Verify hot-reloaded lineup + dynamic passthrough EPG from FakeXtreamServer.
      refresh_r = await client.post("/api/epg/refresh")
      assert refresh_r.status_code == 200

      lineup_r = await client.get("/api/lineup")
      assert lineup_r.status_code == 200
      channels_by_chno = {ch["chno"]: ch for ch in lineup_r.json()["channels"]}
      assert 1101 in channels_by_chno
      assert 1401 in channels_by_chno
      assert (
        channels_by_chno[1101]["now_playing"]["title"]
        == "Le Téléjournal Montréal"
      )
      assert channels_by_chno[1401]["now_playing"]["title"] == "SportsCentre"
      assert (cache_dir / "bellhop_web" / "epg_cache.json").is_file()

      # Verify /api/config exposes operational config with zero secrets/paths.
      cfg_r = await client.get("/api/config")
      assert cfg_r.status_code == 200
      cfg_payload = cfg_r.json()
      assert cfg_payload["available"] is True
      assert "web_server" not in cfg_payload["config"]
      assert "paths" not in cfg_payload["config"]
      assert "container_pw" not in cfg_r.text
      assert "up_secret" not in cfg_r.text

      # 6. Verify live MPEG-TS streaming + single-tuner HLS preemption through container.
      async with client.stream("GET", "/api/stream/1101.ts?client=c1") as st_r:
        assert st_r.status_code == 200
        ts_buf = b""
        async for chunk in st_r.aiter_bytes():
          ts_buf += chunk
          if len(ts_buf) >= 188:
            break
        assert ts_buf[0] == 0x47

      hls_r = await client.get("/api/hls/1401/index.m3u8?client=c1")
      assert hls_r.status_code == 200
      assert "#EXTM3U" in hls_r.text
      seg_lines = [
        line.strip()
        for line in hls_r.text.splitlines()
        if line.strip() and not line.startswith("#")
      ]
      assert len(seg_lines) >= 1
      seg_r = await client.get(seg_lines[0])
      assert seg_r.status_code == 200
      assert seg_r.content[0] == 0x47
      assert fake_xtream.connection_limit_violations == 0

      # 7. Verify Passthrough Xtream Codes API with surfaced BELLHOP_SERVE credentials.
      xt_auth = await client.get(
        "/player_api.php",
        params={"username": "container_xt", "password": "container_xt_pw"},
      )
      assert xt_auth.status_code == 200
      assert xt_auth.json()["user_info"]["auth"] == 1

      xt_streams = await client.get(
        "/player_api.php",
        params={
          "username": "container_xt",
          "password": "container_xt_pw",
          "action": "get_live_streams",
        },
      )
      assert xt_streams.status_code == 200
      stream_ids = {int(s["stream_id"]) for s in xt_streams.json()}
      assert 710101 in stream_ids
      assert 710401 in stream_ids

      xt_epg = await client.get(
        "/xmltv.php",
        params={"username": "container_xt", "password": "container_xt_pw"},
      )
      assert xt_epg.status_code == 302
      assert fake_xtream.base_url in xt_epg.headers["location"]

      xt_live = await client.get("/live/container_xt/container_xt_pw/710101.ts")
      assert xt_live.status_code == 302
      assert (
        xt_live.headers["location"]
        == f"{fake_xtream.base_url}/live/up_user/up_secret/710101.ts"
      )

    # 8. Stop container cleanly via SIGTERM and verify exit code 0 (no SIGKILL).
    stop_res = await asyncio.to_thread(
      subprocess.run,
      [container_cli, "stop", "-t", "5", container_name],
      capture_output=True,
      text=True,
      check=False,
      timeout=15,
    )
    assert stop_res.returncode == 0
    inspect_code = await asyncio.to_thread(
      subprocess.run,
      [container_cli, "inspect", "-f", "{{.State.ExitCode}}", container_name],
      capture_output=True,
      text=True,
      check=False,
      timeout=10,
    )
    assert inspect_code.stdout.strip() == "0", (
      f"Expected clean SIGTERM exit 0, got {inspect_code.stdout.strip()}"
    )
  finally:
    await asyncio.to_thread(
      subprocess.run,
      [container_cli, "rm", "-f", container_name],
      capture_output=True,
      check=False,
    )
    await fake_xtream.stop()
