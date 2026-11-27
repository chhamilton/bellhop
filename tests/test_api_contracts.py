"""End-to-end Python <-> TypeScript HTTP API schema contract tests."""

from __future__ import annotations

import json
import time
from pathlib import Path

import pytest

from bellhop.common.api_contracts import (
  API_CONTRACT_TYPES,
  AccountInfoResponseDict,
  AuthStatusResponseDict,
  ChannelEpgResponseDict,
  HealthApiResponseDict,
  LineupApiResponseDict,
  PasswordChangeResponseDict,
  PlayerUrlsDict,
  build_api_json_schemas,
  validate_typed_dict_payload,
)
from bellhop.config import load_config
from bellhop.serve import UnifiedServeServer
from bellhop.web.store import ProgramItem
from tests.conftest import TEST_PASSWORD_HASH, TEST_WEB_PASSWORD
from tests.http_mock import TestHttpClient


def test_json_schema_generation_and_validator_negative_cases() -> None:
  """Verify Draft 2020-12 JSON Schema generation and negative runtime checks."""
  bundle = build_api_json_schemas()
  assert bundle["$schema"] == "https://json-schema.org/draft/2020-12/schema"
  defs = bundle["$defs"]
  assert set(defs.keys()) == {ts_name for _, ts_name in API_CONTRACT_TYPES}
  assert "callsign" in defs["PublicChannel"]["properties"]
  assert defs["PublicChannel"]["properties"]["callsign"] == {
    "anyOf": [{"type": "string"}, {"type": "null"}]
  }

  # Valid payload passes
  validate_typed_dict_payload(
    {
      "auth_required": True,
      "authenticated": False,
      "account_id": None,
      "email": None,
      "expires_at": None,
    },
    AuthStatusResponseDict,
  )
  validate_typed_dict_payload(
    {
      "xtream_server_url": "http://127.0.0.1:8080",
      "m3u_url": "http://127.0.0.1:8080/get.php?u=a&p=b",
      "m3u_hls_url": "http://127.0.0.1:8080/get.php?u=a&p=b&output=hls",
      "xmltv_url": "http://127.0.0.1:8080/xmltv.php?u=a&p=b",
    },
    PlayerUrlsDict,
  )

  # Bool rejected where int/float expected
  with pytest.raises(AssertionError, match="expires_at"):
    validate_typed_dict_payload(
      {
        "auth_required": True,
        "authenticated": True,
        "account_id": "admin",
        "email": "admin@example.com",
        "expires_at": True,
      },
      AuthStatusResponseDict,
    )

  # Unexpected extra key rejected
  with pytest.raises(AssertionError, match="unexpected keys"):
    validate_typed_dict_payload(
      {
        "auth_required": False,
        "authenticated": True,
        "account_id": None,
        "email": None,
        "expires_at": None,
        "surprise": 1,
      },
      AuthStatusResponseDict,
    )


@pytest.mark.asyncio
async def test_live_http_endpoints_conform_to_generated_ts_contracts(
  tmp_path: Path,
) -> None:
  """Verify real WebGuideApp responses satisfy TypedDict contracts."""
  now_ts = int(time.time())
  xt_dir = tmp_path / "output" / "xtream"
  xt_dir.mkdir(parents=True)
  (xt_dir / "bellhop_xtream_playlist.json").write_text(
    json.dumps(
      {
        "lineup_info": {
          "lineup_id": "CAN-BELLFIBE-MTL-X",
          "provider": "Bell Fibe TV",
          "market": "Montréal, QC",
          "postal_prefix": "H3B",
        },
        "server_info": {"timezone": "UTC"},
        "live_categories": [
          {"category_id": "2", "category_name": "Sports", "parent_id": 0}
        ],
        "live_streams": [
          {
            "num": 1108,
            "name": "RDS",
            "stream_id": 410108,
            "stream_icon": "https://img.example/rds.png",
            "epg_channel_id": "RDS.ca",
            "category_id": "2",
            "category_name": "Sports",
            "direct_source": "http://upstream.example/live/u/p/410108.ts",
            "guide": {
              "sd_chno": 108,
              "callsign": None,
              "language": "fr",
              "quality_tier": "fhd",
              "has_native_epg": True,
            },
          }
        ],
      }
    ),
    encoding="utf-8",
  )

  creds_path = tmp_path / "credentials.yaml"
  creds_path.write_text(
    f"""\
accounts:
  contract_admin:
    email: "contract@example.com"
    web_password_hash: "{TEST_PASSWORD_HASH}"
    xtream_username: "xtuser01"
    xtream_password: "xtpass01"
    upstream_username: "up_user"
    upstream_password: "up_pass"
""",
    encoding="utf-8",
  )

  cfg_path = tmp_path / "config.yaml"
  cfg_path.write_text(
    f"""\
credentials_file: "{creds_path}"
paths:
  cache_dir: "{tmp_path / "cache"}"
  xtream_output_dir: "{xt_dir}"
web_server:
  enabled: true
  host: "127.0.0.1"
  port: 0
upstream_xtream:
  server_url: "http://upstream.example"
  stream_format: "ts"
xtream_server:
  enabled: true
""",
    encoding="utf-8",
  )

  cfg = load_config(cfg_path)
  srv = UnifiedServeServer.from_config(cfg)
  assert srv.web_app is not None
  assert srv.web_app.epg_refresher is not None
  snap = srv.web_app.store.get_snapshot()
  srv.web_app.epg_refresher.db.replace_guide_schedules(
    snap=snap,
    programs_by_id={
      "RDS.ca": [
        ProgramItem(
          id="p1",
          channel_id="RDS.ca",
          title="Hockey Canadien",
          sub_title="LNH",
          description="En direct.",
          category="Sports",
          start_timestamp=now_ts - 600,
          stop_timestamp=now_ts + 1800,
        )
      ]
    },
    fetched_at=float(now_ts),
  )
  await srv.start()
  try:
    async with TestHttpClient(
      base_url=f"http://127.0.0.1:{srv.bound_port}", timeout=5.0
    ) as client:
      auth_r = await client.get("/api/auth/status")
      assert auth_r.status_code == 200
      validate_typed_dict_payload(auth_r.json(), AuthStatusResponseDict)

      login_r = await client.post(
        "/api/auth/login",
        json={"email": "contract@example.com", "password": TEST_WEB_PASSWORD},
      )
      assert login_r.status_code == 200

      health_r = await client.get("/api/health")
      assert health_r.status_code == 200
      validate_typed_dict_payload(health_r.json(), HealthApiResponseDict)

      lineup_r = await client.get("/api/lineup")
      assert lineup_r.status_code == 200
      validate_typed_dict_payload(lineup_r.json(), LineupApiResponseDict)

      epg_r = await client.get("/api/epg/1108")
      assert epg_r.status_code == 200
      validate_typed_dict_payload(epg_r.json(), ChannelEpgResponseDict)

      acct_r = await client.get("/api/account")
      assert acct_r.status_code == 200
      validate_typed_dict_payload(acct_r.json(), AccountInfoResponseDict)

      regen_r = await client.post("/api/account/stream-credentials/regenerate")
      assert regen_r.status_code == 200
      validate_typed_dict_payload(regen_r.json(), AccountInfoResponseDict)

      pw_r = await client.post(
        "/api/account/password",
        json={
          "current_password": TEST_WEB_PASSWORD,
          "new_password": "new_contract_password",
        },
      )
      assert pw_r.status_code == 200
      validate_typed_dict_payload(pw_r.json(), PasswordChangeResponseDict)
  finally:
    await srv.stop()
