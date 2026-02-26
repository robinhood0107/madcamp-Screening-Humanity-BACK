"""
[역할]
향후 C gRPC 데이터 서비스 호출을 Python 라우터/서비스에서 공통으로 쓰기 위한 클라이언트 추상화 스텁.

[현재 상태]
- gRPC 경유 호출(`grpcurl` 기반) + 리허설용 CLI bridge(c-data-service one-shot JSON) 경로를 단계적으로 붙이는 중이다.
- 실제 운영용 gRPC client(`grpcio`/generated stub)로 고정되기 전까지는 rehearsal fallback과 함께 사용된다.

[주의]
- 실제 CRUD/조회 호출 구현 전에는 `NotImplementedError`를 발생시킨다.
- 라우터에서 직접 사용하기 시작하는 시점에 단계적으로 메서드를 추가한다.
"""

from __future__ import annotations

from dataclasses import dataclass
import json
import logging
import os
from pathlib import Path
import shutil
import subprocess
from typing import Any, Dict, Optional

from app.core.config import settings

logger = logging.getLogger(__name__)


@dataclass
class DataServiceConfig:
    enabled: bool
    grpc_addr: str
    timeout_ms: int


def get_data_service_config() -> DataServiceConfig:
    return DataServiceConfig(
        enabled=bool(settings.USE_C_DATA_SERVICE),
        grpc_addr=settings.data_service_grpc_addr_value,
        timeout_ms=int(settings.DATA_SERVICE_TIMEOUT_MS),
    )


class DataServiceClient:
    """
    [역할]
    C gRPC data-service 호출 인터페이스의 Python 측 래퍼 스텁.

    [향후 계획]
    - grpcio 채널 초기화
    - auth/conversation/media/character catalog 서비스별 스텁 분리
    - 공통 timeout/retry/에러 매핑 도입
    """

    def __init__(self) -> None:
        self.cfg = get_data_service_config()
        self._scaffold_warned = False
        self._grpcurl_warned = False
        self._cli_bridge_warned = False

    def is_enabled(self) -> bool:
        return self.cfg.enabled

    def _repo_root(self) -> Optional[Path]:
        # .../madcamp-Screening-Humanity-BACK/app/services/data_service_client.py -> repo root
        try:
            return Path(__file__).resolve().parents[3]
        except Exception:
            return None

    def _proto_dir(self) -> Optional[Path]:
        root = self._repo_root()
        if not root:
            return None
        proto_dir = root / "madcamp-Screening-Humanity-DATA-SERVICE" / "proto"
        return proto_dir if proto_dir.exists() else None

    def _grpcurl_path(self) -> Optional[str]:
        return shutil.which("grpcurl")

    def _cli_bridge_enabled(self) -> bool:
        return bool(settings.DATA_SERVICE_CLI_BRIDGE_ENABLED)

    def _c_data_service_bin_path(self) -> Optional[str]:
        configured = settings.data_service_cli_bin_value
        if configured:
            resolved = shutil.which(configured)
            if resolved:
                return resolved
            # 절대/상대 경로로 직접 지정된 경우
            if Path(configured).exists():
                return configured
        root = self._repo_root()
        if not root:
            return None
        candidates = [
            root / "madcamp-Screening-Humanity-DATA-SERVICE" / "c-data-service",
            root / "madcamp-Screening-Humanity-DATA-SERVICE" / "bin" / "c-data-service",
            root / "madcamp-Screening-Humanity-DATA-SERVICE" / "build" / "c-data-service",
        ]
        for c in candidates:
            if c.exists():
                return str(c)
        return None

    def _grpcurl_cmd_base(self) -> Optional[list[str]]:
        grpcurl = self._grpcurl_path()
        proto_dir = self._proto_dir()
        if not grpcurl or not proto_dir:
            return None
        timeout_sec = max(1, int(self.cfg.timeout_ms / 1000))
        return [
            grpcurl,
            "-plaintext",
            "-max-time",
            str(timeout_sec),
            "-import-path",
            str(proto_dir),
            "-proto",
            "madcamp_data.proto",
        ]

    def _warn_grpcurl_unavailable_once(self) -> None:
        if self._grpcurl_warned:
            return
        self._grpcurl_warned = True
        logger.warning(
            "USE_C_DATA_SERVICE=true but grpcurl/proto path is unavailable (grpcurl=%s proto_dir=%s addr=%s)",
            bool(self._grpcurl_path()),
            str(self._proto_dir()) if self._proto_dir() else None,
            self.cfg.grpc_addr,
        )

    def _warn_cli_bridge_unavailable_once(self) -> None:
        if self._cli_bridge_warned:
            return
        self._cli_bridge_warned = True
        logger.warning(
            "DATA_SERVICE_CLI_BRIDGE is unavailable/enabled=%s bin=%s addr=%s",
            self._cli_bridge_enabled(),
            self._c_data_service_bin_path(),
            self.cfg.grpc_addr,
        )

    def _cli_bridge_call(self, args: list[str]) -> Dict[str, Any]:
        if not self._cli_bridge_enabled():
            raise NotImplementedError("DATA_SERVICE_CLI_BRIDGE_ENABLED=false")
        bin_path = self._c_data_service_bin_path()
        if not bin_path:
            self._warn_cli_bridge_unavailable_once()
            raise NotImplementedError("c-data-service binary is unavailable")
        cmd = [bin_path, *args]
        env = os.environ.copy()
        # c-data-service one-shot commands read PG* env directly.
        try:
            proc = subprocess.run(
                cmd,
                check=True,
                capture_output=True,
                text=True,
                env=env,
            )
        except FileNotFoundError as exc:
            self._warn_cli_bridge_unavailable_once()
            raise NotImplementedError("c-data-service CLI bridge binary not found") from exc
        except subprocess.CalledProcessError as exc:
            stderr = (exc.stderr or "").strip()
            stdout = (exc.stdout or "").strip()
            detail = stderr or stdout or str(exc)
            raise RuntimeError(f"c-data-service CLI bridge call failed: {detail}") from exc

        raw = (proc.stdout or "").strip()
        if not raw:
            return {}
        try:
            parsed = json.loads(raw)
            return parsed if isinstance(parsed, dict) else {"value": parsed}
        except json.JSONDecodeError as exc:
            raise RuntimeError(f"c-data-service CLI bridge returned non-JSON: {raw[:200]}") from exc

    def _grpcurl_call(self, full_method: str, *, payload: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        base = self._grpcurl_cmd_base()
        if not base:
            self._warn_grpcurl_unavailable_once()
            raise NotImplementedError("grpcurl transport is unavailable in current environment")

        cmd = [*base, "-d", json.dumps(payload or {}), self.cfg.grpc_addr, full_method]
        try:
            proc = subprocess.run(
                cmd,
                check=True,
                capture_output=True,
                text=True,
            )
        except FileNotFoundError as exc:
            self._warn_grpcurl_unavailable_once()
            raise NotImplementedError("grpcurl binary not found") from exc
        except subprocess.CalledProcessError as exc:
            stderr = (exc.stderr or "").strip()
            stdout = (exc.stdout or "").strip()
            detail = stderr or stdout or str(exc)
            lowered = detail.lower()
            if "unimplemented" in lowered or "not found" in lowered or "server does not support the reflection" in lowered:
                raise NotImplementedError(f"{full_method} not implemented on data-service: {detail}") from exc
            raise RuntimeError(f"grpcurl call failed for {full_method}: {detail}") from exc

        raw = (proc.stdout or "").strip()
        if not raw:
            return {}
        try:
            parsed = json.loads(raw)
            return parsed if isinstance(parsed, dict) else {"value": parsed}
        except json.JSONDecodeError as exc:
            raise RuntimeError(f"grpcurl returned non-JSON output for {full_method}: {raw[:200]}") from exc

    @staticmethod
    def _owner_selector_from_payload(payload: Dict[str, Any]) -> Dict[str, Any]:
        if payload.get("owner_user_id"):
            return {"userId": str(payload["owner_user_id"])}
        if payload.get("owner_guest_session_id"):
            return {"guestSessionId": str(payload["owner_guest_session_id"])}
        raise ValueError("owner_user_id or owner_guest_session_id is required")

    @staticmethod
    def _normalize_guest_session_result(res: Dict[str, Any]) -> Dict[str, Any]:
        found = bool(res.get("found", True))
        if not found:
            return {"found": False, "guest_session": None}
        guest_row = {
            "id": res.get("id"),
            "display_name": res.get("displayName") or res.get("display_name"),
            "status": res.get("status"),
            "merged_to_user_id": res.get("mergedToUserId") or res.get("merged_to_user_id"),
            "created_at": res.get("createdAt") or res.get("created_at"),
            "expires_at": res.get("expiresAt") or res.get("expires_at"),
        }
        return {"found": True, "guest_session": guest_row}

    @staticmethod
    def _normalize_user_result(res: Dict[str, Any]) -> Dict[str, Any]:
        found = bool(res.get("found", True))
        if not found:
            return {"found": False, "user": None}
        user_row = {
            "id": res.get("id"),
            "email": res.get("email"),
            "username": res.get("username"),
            "picture": res.get("picture"),
            "is_active": bool(res.get("isActive") if "isActive" in res else res.get("is_active", True)),
            "is_superuser": bool(
                res.get("isSuperuser") if "isSuperuser" in res else res.get("is_superuser", False)
            ),
            "provider": res.get("provider"),
            "created_at": res.get("createdAt") or res.get("created_at"),
        }
        return {"found": True, "user": user_row}

    def health_check(self) -> Dict[str, Any]:
        if not self.cfg.enabled:
            return {
                "enabled": False,
                "status": "disabled",
                "grpc_addr": self.cfg.grpc_addr,
                "detail": "USE_C_DATA_SERVICE=false",
            }
        try:
            grpc_res = self._grpcurl_call("madcamp.data.v1.HealthService/Check", payload={})
            return {
                "enabled": True,
                "status": "grpc_ok",
                "grpc_addr": self.cfg.grpc_addr,
                "service": grpc_res.get("service"),
                "detail": grpc_res.get("detail") or grpc_res.get("status") or "gRPC health responded",
            }
        except NotImplementedError as exc:
            try:
                cli_res = self._cli_bridge_call(["--healthcheck"])
                return {
                    "enabled": True,
                    "status": "cli_bridge_ok",
                    "grpc_addr": self.cfg.grpc_addr,
                    "detail": cli_res.get("status") or "c-data-service CLI bridge health responded",
                    "service": cli_res.get("service"),
                }
            except Exception as cli_exc:
                if not self._scaffold_warned:
                    logger.warning(
                        "USE_C_DATA_SERVICE=true but gRPC server/wiring is not ready yet (addr=%s detail=%s cli=%s)",
                        self.cfg.grpc_addr,
                        exc,
                        cli_exc,
                    )
                    self._scaffold_warned = True
                return {
                    "enabled": True,
                    "status": "scaffold_only",
                    "grpc_addr": self.cfg.grpc_addr,
                    "detail": str(exc),
                }
        except Exception as exc:
            logger.warning("DataServiceClient health_check failed addr=%s err=%s", self.cfg.grpc_addr, exc)
            return {
                "enabled": True,
                "status": "grpc_unavailable",
                "grpc_addr": self.cfg.grpc_addr,
                "detail": str(exc),
            }

    def history_list_conversations(self, *, payload: Dict[str, Any]) -> Dict[str, Any]:
        unsupported_filters = any(
            payload.get(k) is not None for k in ("status_filter", "mode", "has_audio", "cursor", "search")
        )
        req: Dict[str, Any] = {
            "owner": self._owner_selector_from_payload(payload),
            "limit": int(payload.get("limit") or 20),
        }
        if payload.get("status_filter"):
            req["status"] = payload["status_filter"]
        if payload.get("mode"):
            req["mode"] = payload["mode"]
        if payload.get("has_audio") is not None:
            req["hasAudio"] = bool(payload["has_audio"])
        if payload.get("cursor"):
            req["cursor"] = str(payload["cursor"])
        if payload.get("search"):
            req["search"] = str(payload["search"])

        try:
            res = self._grpcurl_call("madcamp.data.v1.ConversationDataService/ListConversationsForOwner", payload=req)
        except NotImplementedError:
            if unsupported_filters:
                raise
            owner_user_id = payload.get("owner_user_id")
            owner_guest_session_id = payload.get("owner_guest_session_id")
            limit = int(payload.get("limit") or 20)
            if owner_user_id:
                res = self._cli_bridge_call(["--json-history-list-user", str(owner_user_id), str(limit)])
            elif owner_guest_session_id:
                res = self._cli_bridge_call(["--json-history-list-guest", str(owner_guest_session_id), str(limit)])
            else:
                raise ValueError("owner_user_id or owner_guest_session_id is required")
        items_in = res.get("items") or []
        items_out = []
        for item in items_in:
            if not isinstance(item, dict):
                continue
            items_out.append(
                {
                    "id": item.get("conversationId") or item.get("conversation_id"),
                    "session_id": item.get("legacySessionId") or item.get("legacy_session_id"),
                    "title": item.get("title") or "",
                    "status": item.get("status") or "active",
                    "mode": item.get("gameMode") or item.get("game_mode"),
                    "turn_count": None,
                    "audio_count": int(item.get("audioCount") or item.get("audio_count") or 0),
                    "updated_at": item.get("updatedAt") or item.get("updated_at"),
                    "created_at": None,
                    "character_name": item.get("characterName") or item.get("character_name"),
                    "thumbnail_url": None,
                    "preview_text": item.get("latestMessagePreview") or item.get("latest_message_preview"),
                }
            )
        total_raw = res.get("total")
        total = int(total_raw) if total_raw is not None else len(items_out)
        return {"items": items_out, "next_cursor": res.get("nextCursor") or res.get("next_cursor"), "total": total}

    def history_get_detail(self, *, payload: Dict[str, Any]) -> Dict[str, Any]:
        req: Dict[str, Any] = {
            "owner": self._owner_selector_from_payload(payload),
            "conversationId": str(payload["conversation_id"]),
        }
        try:
            res = self._grpcurl_call("madcamp.data.v1.ConversationDataService/GetConversationDetail", payload=req)
        except NotImplementedError:
            owner_user_id = payload.get("owner_user_id")
            owner_guest_session_id = payload.get("owner_guest_session_id")
            conversation_id = str(payload["conversation_id"])
            if owner_user_id:
                res = self._cli_bridge_call(["--json-history-detail-user", str(owner_user_id), conversation_id])
            elif owner_guest_session_id:
                res = self._cli_bridge_call(["--json-history-detail-guest", str(owner_guest_session_id), conversation_id])
            else:
                raise ValueError("owner_user_id or owner_guest_session_id is required")

        scenario_snapshot = None
        scenario_snapshot_json = res.get("scenarioSnapshotJson") or res.get("scenario_snapshot_json")
        if isinstance(scenario_snapshot_json, str) and scenario_snapshot_json.strip():
            try:
                scenario_snapshot = json.loads(scenario_snapshot_json)
            except json.JSONDecodeError:
                # proto 1차 단계에서는 JSON 문자열이 아니더라도 raw를 유지
                scenario_snapshot = scenario_snapshot_json

        messages_in = res.get("messages") or []
        audio_in = res.get("audioAssets") or res.get("audio_assets") or []
        return {
            "conversation": {
                "id": res.get("conversationId") or res.get("conversation_id") or str(payload["conversation_id"]),
                "session_id": res.get("legacySessionId") or res.get("legacy_session_id"),
                "title": res.get("title") or "",
                "status": res.get("status") or "active",
                "mode": res.get("gameMode") or res.get("game_mode"),
                "character_name": None,
                "preview_text": None,
                "created_at": None,
                "updated_at": None,
                "scenario_snapshot": scenario_snapshot,
                "generated_script": res.get("generatedScript") or res.get("generated_script"),
            },
            "messages": [
                {
                    "id": item.get("id"),
                    "role": item.get("role"),
                    "content": item.get("content"),
                    "speaker_name": item.get("speakerName") or item.get("speaker_name"),
                    "created_at": item.get("createdAt") or item.get("created_at"),
                }
                for item in messages_in
                if isinstance(item, dict)
            ],
            "audio_assets": [
                {
                    "id": item.get("mediaAssetId") or item.get("media_asset_id"),
                    "message_id": item.get("messageId") or item.get("message_id"),
                    "voice_id": item.get("voiceId") or item.get("voice_id"),
                    "duration_sec": None,
                    # proto 1차 detail에는 직접 다운로드 URL이 없어서 None으로 둔다.
                    "download_url": None,
                    "play_url": None,
                    "mime_type": item.get("mimeType") or item.get("mime_type"),
                    "size_bytes": int(item.get("sizeBytes") or item.get("size_bytes") or 0),
                    "source_type": item.get("sourceType") or item.get("source_type"),
                    "created_at": item.get("createdAt") or item.get("created_at"),
                }
                for item in audio_in
                if isinstance(item, dict)
            ],
        }

    def history_soft_delete(self, *, payload: Dict[str, Any]) -> Dict[str, Any]:
        req: Dict[str, Any] = {
            "owner": self._owner_selector_from_payload(payload),
            "conversationId": str(payload["conversation_id"]),
            "hardDelete": bool(payload.get("hard_delete") or False),
        }
        try:
            res = self._grpcurl_call("madcamp.data.v1.ConversationDataService/DeleteConversationCascade", payload=req)
        except NotImplementedError:
            owner_user_id = payload.get("owner_user_id")
            owner_guest_session_id = payload.get("owner_guest_session_id")
            conversation_id = str(payload["conversation_id"])
            if owner_user_id:
                res = self._cli_bridge_call(["--json-history-delete-user", str(owner_user_id), conversation_id])
            elif owner_guest_session_id:
                res = self._cli_bridge_call(["--json-history-delete-guest", str(owner_guest_session_id), conversation_id])
            else:
                raise ValueError("owner_user_id or owner_guest_session_id is required")
        return {
            "message": "기록이 삭제되었습니다." if res.get("success", True) else "삭제 처리 실패",
            "conversation_id": str(payload["conversation_id"]),
            "deleted_audio_count": int(res.get("deletedAudioLinks") or res.get("deleted_audio_links") or 0),
        }

    def guest_merge_preview_counts(self, *, payload: Dict[str, Any]) -> Dict[str, Any]:
        req = {"guestSessionId": str(payload["guest_session_id"])}
        try:
            res = self._grpcurl_call("madcamp.data.v1.PrincipalDataService/GetGuestMergePreview", payload=req)
        except NotImplementedError:
            res = self._cli_bridge_call(["--json-guest-merge-preview", str(payload["guest_session_id"])])
        return {
            "conversation_count": int(res.get("conversationCount") or res.get("conversation_count") or 0),
            "audio_count": int(res.get("audioCount") or res.get("audio_count") or 0),
            "storage_bytes": int(res.get("storageBytes") or res.get("storage_bytes") or 0),
        }

    def guest_merge_move_to_user(self, *, payload: Dict[str, Any]) -> Dict[str, Any]:
        req = {
            "guestSessionId": str(payload["guest_session_id"]),
            "userId": str(payload["user_id"]),
            "deleteGuestBrowserCacheHint": True,
        }
        try:
            res = self._grpcurl_call("madcamp.data.v1.PrincipalDataService/MergeGuestToUser", payload=req)
        except NotImplementedError:
            res = self._cli_bridge_call(
                ["--json-guest-merge-move", str(payload["guest_session_id"]), str(payload["user_id"])]
            )
        return {
            "conversation_count": int(res.get("conversationsMoved") or res.get("conversations_moved") or 0),
            "audio_count": int(res.get("mediaAssetsMoved") or res.get("media_assets_moved") or 0),
        }

    def guest_session_get(self, *, payload: Dict[str, Any]) -> Dict[str, Any]:
        guest_session_id = str(payload["guest_session_id"])
        req = {"guestSessionId": guest_session_id}
        try:
            res = self._grpcurl_call("madcamp.data.v1.PrincipalDataService/GetGuestSession", payload=req)
        except NotImplementedError:
            res = self._cli_bridge_call(["--json-guest-session-get", guest_session_id])
        return self._normalize_guest_session_result(res)

    def user_get_by_id(self, *, payload: Dict[str, Any]) -> Dict[str, Any]:
        user_id = str(payload["user_id"])
        req = {"userId": user_id}
        try:
            res = self._grpcurl_call("madcamp.data.v1.PrincipalDataService/GetUserById", payload=req)
        except NotImplementedError:
            res = self._cli_bridge_call(["--json-user-get", user_id])
        return self._normalize_user_result(res)

    def guest_session_create(self, *, payload: Dict[str, Any]) -> Dict[str, Any]:
        guest_session_id = str(payload["guest_session_id"])
        display_name = str(payload.get("display_name") or "")
        expires_at = str(payload.get("expires_at") or "")
        client_ip = str(payload.get("client_ip") or "")
        user_agent = str(payload.get("user_agent") or "")
        req = {
            "guestSessionId": guest_session_id,
            "displayName": display_name,
            "expiresAt": expires_at,
            "clientIp": client_ip,
            "userAgent": user_agent,
        }
        try:
            res = self._grpcurl_call("madcamp.data.v1.PrincipalDataService/CreateGuestSession", payload=req)
        except NotImplementedError:
            res = self._cli_bridge_call(
                [
                    "--json-guest-session-create",
                    guest_session_id,
                    display_name,
                    expires_at,
                    client_ip,
                    user_agent,
                ]
            )
        return self._normalize_guest_session_result(res)

    def guest_session_update_status(self, *, payload: Dict[str, Any]) -> Dict[str, Any]:
        guest_session_id = str(payload["guest_session_id"])
        status = str(payload["status"])
        reason = payload.get("reason")
        req = {
            "guestSessionId": guest_session_id,
            "status": status,
            "reason": str(reason or ""),
        }
        try:
            res = self._grpcurl_call("madcamp.data.v1.PrincipalDataService/UpdateGuestSessionStatus", payload=req)
        except NotImplementedError:
            args = ["--json-guest-session-update-status", guest_session_id, status]
            if reason:
                args.append(str(reason))
            res = self._cli_bridge_call(args)

        return self._normalize_guest_session_result(res)

    def scenario_create_user(self, *, payload: Dict[str, Any]) -> Dict[str, Any]:
        user_id = str(payload["user_id"])
        user_name = str(payload.get("user_name") or "")
        character_name = str(payload.get("character_name") or "")
        situation = str(payload.get("situation") or "")
        summary = str(payload.get("summary") or "")
        background = str(payload.get("background") or "")

        req = {
            "userId": user_id,
            "userName": user_name,
            "characterName": character_name,
            "situation": situation,
            "summary": summary,
            "background": background,
        }
        try:
            res = self._grpcurl_call("madcamp.data.v1.ScenarioDataService/CreateScenario", payload=req)
        except NotImplementedError:
            res = self._cli_bridge_call(
                [
                    "--json-scenario-create-user",
                    user_id,
                    user_name,
                    character_name,
                    situation,
                    summary,
                    background,
                ]
            )

        return {
            "scenario_id": res.get("scenarioId") or res.get("scenario_id"),
            "summary": res.get("summary") or summary,
            "background": res.get("background") or background,
            "created_at": res.get("createdAt") or res.get("created_at"),
        }

    def auth_event_create(self, *, payload: Dict[str, Any]) -> Dict[str, Any]:
        actor_type = str(payload["actor_type"])
        actor_id = payload.get("actor_id")
        event_type = str(payload["event_type"])
        provider = str(payload["provider"])
        success = bool(payload.get("success", True))
        reason = payload.get("reason")
        client_ip = payload.get("client_ip")
        user_agent = payload.get("user_agent")

        req = {
            "actorType": actor_type,
            "actorId": str(actor_id or ""),
            "eventType": event_type,
            "provider": provider,
            "success": success,
            "reason": str(reason or ""),
            "clientIp": str(client_ip or ""),
            "userAgent": str(user_agent or ""),
        }
        try:
            res = self._grpcurl_call("madcamp.data.v1.PrincipalDataService/CreateAuthEvent", payload=req)
        except NotImplementedError:
            args = [
                "--json-auth-event-create",
                actor_type,
                str(actor_id or ""),
                event_type,
                provider,
                "true" if success else "false",
                str(reason or ""),
                str(client_ip or ""),
                str(user_agent or ""),
            ]
            res = self._cli_bridge_call(args)

        return {"event_id": res.get("eventId") or res.get("event_id")}

    def character_catalog_list_public_presets_legacy(self, *, payload: Dict[str, Any]) -> Dict[str, Any]:
        limit = int(payload.get("limit") or 500)
        req = {"limit": limit, "isPublic": True, "isPreset": True}
        try:
            res = self._grpcurl_call("madcamp.data.v1.CharacterCatalogService/ListCharacterCatalog", payload=req)
        except NotImplementedError:
            res = self._cli_bridge_call(["--json-character-presets-list", str(limit)])

        items_in = res.get("items") or []
        out_items = []
        for item in items_in:
            if not isinstance(item, dict):
                continue
            catalog_id = item.get("catalogId") or item.get("catalog_id") or item.get("id")
            slug = item.get("slug") or catalog_id
            image_asset_id = item.get("imageAssetId") or item.get("image_asset_id")
            image_url_external = item.get("imageUrlExternal") or item.get("image_url_external")
            tags = item.get("tags") or []
            if not isinstance(tags, list):
                tags = []
            canonical = f"/api/characters/catalog-image/{catalog_id}" if catalog_id and image_asset_id else None
            out_items.append(
                {
                    "id": slug,
                    "name": item.get("name"),
                    "description": item.get("description"),
                    "persona": item.get("persona"),
                    "voice_id": item.get("voiceId") or item.get("voice_id"),
                    "category": item.get("category"),
                    "tags": tags,
                    "image_url": canonical or image_url_external,
                    "image_url_canonical": canonical,
                    "is_preset": True,
                    "_source": "db_character_catalog_data_service",
                    "_catalog_id": catalog_id,
                    "_slug": slug,
                }
            )
        return {
            "items": out_items,
            "total": int(res.get("total") or len(out_items)),
        }

    def user_preferences_get(self, *, payload: Dict[str, Any]) -> Dict[str, Any]:
        user_id = str(payload["user_id"])
        req = {"userId": user_id}
        try:
            res = self._grpcurl_call("madcamp.data.v1.PreferenceDataService/GetUserPreferences", payload=req)
        except NotImplementedError:
            res = self._cli_bridge_call(["--json-user-pref-get", user_id])
        settings_value = res.get("settings") or {}
        if not isinstance(settings_value, dict):
            settings_value = {}
        return {
            "found": bool(res.get("found", True)),
            "settings": settings_value,
        }

    def user_preferences_put(self, *, payload: Dict[str, Any]) -> Dict[str, Any]:
        user_id = str(payload["user_id"])
        settings_patch = payload.get("settings_patch") or {}
        if not isinstance(settings_patch, dict):
            raise ValueError("settings_patch must be a dict")

        req = {"userId": user_id, "patchJson": json.dumps(settings_patch, ensure_ascii=False)}
        try:
            res = self._grpcurl_call("madcamp.data.v1.PreferenceDataService/PutUserPreferences", payload=req)
        except NotImplementedError:
            res = self._cli_bridge_call(
                ["--json-user-pref-put", user_id, json.dumps(settings_patch, ensure_ascii=False)]
            )
        merged = res.get("settings") or {}
        if not isinstance(merged, dict):
            merged = {}
        return {"settings": merged}

    def chat_summary_get_by_session(self, *, payload: Dict[str, Any]) -> Dict[str, Any]:
        session_id = str(payload["session_id"])
        req = {"sessionId": session_id}
        try:
            res = self._grpcurl_call("madcamp.data.v1.SummaryDataService/GetChatSummaryBySessionId", payload=req)
        except NotImplementedError:
            res = self._cli_bridge_call(["--json-chat-summary-get", session_id])

        return {
            "found": bool(res.get("found", True)),
            "session_id": res.get("sessionId") or res.get("session_id") or session_id,
            "summary": str(res.get("summary") or ""),
            "updated_at": res.get("updatedAt") or res.get("updated_at"),
        }

    def chat_summary_put_by_session(self, *, payload: Dict[str, Any]) -> Dict[str, Any]:
        session_id = str(payload["session_id"])
        summary = str(payload.get("summary") or "")
        req = {"sessionId": session_id, "summary": summary}
        try:
            res = self._grpcurl_call("madcamp.data.v1.SummaryDataService/PutChatSummaryBySessionId", payload=req)
        except NotImplementedError:
            res = self._cli_bridge_call(["--json-chat-summary-put", session_id, summary])

        return {
            "session_id": res.get("sessionId") or res.get("session_id") or session_id,
            "summary": str(res.get("summary") or summary),
            "updated_at": res.get("updatedAt") or res.get("updated_at"),
        }

    def evaluation_create_by_session(self, *, payload: Dict[str, Any]) -> Dict[str, Any]:
        legacy_session_id = str(payload["legacy_session_id"])
        score = payload.get("score")
        summary = str(payload.get("summary") or "")
        feedback = str(payload.get("feedback") or "")
        raw_payload = payload.get("raw_payload")
        raw_payload_json = (
            json.dumps(raw_payload, ensure_ascii=False)
            if isinstance(raw_payload, (dict, list))
            else (str(raw_payload) if raw_payload is not None else "")
        )

        score_value = int(score) if isinstance(score, (int, float)) else -1
        req = {
            "legacySessionId": legacy_session_id,
            "score": score_value,
            "summary": summary,
            "feedback": feedback,
            "rawPayloadJson": raw_payload_json,
        }
        try:
            res = self._grpcurl_call(
                "madcamp.data.v1.EvaluationDataService/CreateEvaluationByLegacySession",
                payload=req,
            )
        except NotImplementedError:
            args = [
                "--json-evaluation-create-by-session",
                legacy_session_id,
                str(score_value),
                summary,
                feedback,
            ]
            if raw_payload_json:
                args.append(raw_payload_json)
            res = self._cli_bridge_call(args)

        return {
            "created": bool(res.get("created", False)),
            "evaluation_id": res.get("evaluationId") or res.get("evaluation_id"),
            "conversation_id": res.get("conversationId") or res.get("conversation_id"),
            "created_at": res.get("createdAt") or res.get("created_at"),
            "reason": res.get("reason"),
        }

    def not_implemented(self, op_name: str, *, payload: Optional[Dict[str, Any]] = None) -> None:
        logger.error("DataServiceClient.%s requested before gRPC implementation payload_keys=%s",
                     op_name, sorted((payload or {}).keys()))
        raise NotImplementedError(
            f"DataServiceClient.{op_name} is not implemented yet. "
            f"Scaffold is present, but gRPC integration is pending."
        )


data_service_client = DataServiceClient()
